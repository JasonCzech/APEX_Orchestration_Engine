"""Engine-level abort: kill the external load run when graph-level cancel isn't enough.

Flow (plan Part 1, "Durability & the execution phase" — the engine kill switch):

1. Discover the EngineHandle: thread state ``values.engine_handle`` via the loopback
   LangGraph API (the caller's key is forwarded, so @auth.on scoping applies), falling
   back to the latest ``engine_runs`` projection row;
2. resolve the engine adapter with the run's persisted project scope;
3. ``adapter.abort(handle, reason=...)`` — failures propagate and leave the graph
   poller alive so a failed kill never strands the external load;
4. leave an active LangGraph monitor alive so it can collect results and durably
   finalize the execution checkpoint;
5. only settle/tear down a projection-only run when the LangGraph thread is gone.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import structlog
from langgraph_sdk.errors import NotFoundError
from pydantic import ValidationError

from apex.adapters.registry import PortKind
from apex.auth.identity import ScopeRef
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.pipeline import (
    TERMINAL_PHASE_STATUSES,
    EngineConnectionAffinityMissingError,
    EngineHandle,
    Phase,
    PhaseStatus,
)
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineProviderRunNotFoundError,
    EngineRunPhase,
    EngineRunStatus,
)
from apex.services.connections import (
    ConnectionResolver,
    ResolvedAdapter,
    close_adapter,
    get_connection_resolver,
)
from apex.services.pipeline_read import (
    ActiveRunSnapshotUnstableError,
    LangGraphClientLike,
    TooManyActiveRunsError,
)
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

DEFAULT_ABORT_REASON = "operator abort"
STOPPING_CONFIRM_ATTEMPTS = 20
STOPPING_CONFIRM_INTERVAL_S = 0.25
ACTIVE_RUN_PAGE_SIZE = 100
MAX_ACTIVE_RUN_SNAPSHOT = 1_000
ACTIVE_RUN_STABILITY_ATTEMPTS = 4
MAX_STATE_HANDLE_SEARCH_NODES = 10_000
MAX_STATE_HANDLE_SEARCH_DEPTH = 64


class EngineRunNotFoundError(Exception):
    """No engine handle is discoverable for the thread (neither state nor projection)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"no engine run found for thread {thread_id!r}")


class EngineAbortConfirmationPendingError(Exception):
    """The provider is stopping and no durable graph monitor can confirm it."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"external engine abort for thread {thread_id!r} is still awaiting confirmation"
        )


class EngineProvisioningAbortPendingError(Exception):
    """The current attempt reserved a provider lease but has not checkpointed a handle."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"engine provisioning for thread {thread_id!r} has not checkpointed an abort handle"
        )


class EngineGraphFinalizationPendingError(Exception):
    """The provider stopped but a RUNNING execution checkpoint has no monitor."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"graph finalization for thread {thread_id!r} requires pipeline recovery")


class EngineProjectionFinalizationPendingError(Exception):
    """A projection-only provider run stopped but teardown must be retried."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"projection-only engine finalization for thread {thread_id!r} requires retry"
        )


class EngineProviderAbortError(RuntimeError):
    """The resolved engine provider failed or violated its abort contract."""


class EngineRunsRepoLike(Protocol):
    """Structural slice of EngineRunsRepository used by the abort service."""

    async def get_latest_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> Any: ...

    async def get_latest_abortable_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> Any: ...

    async def mark_aborted(
        self,
        thread_id: str,
        *,
        projection_id: str,
        attempt: int,
        expected_external_run_id: str | None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int: ...

    async def release_read_transaction(self) -> None: ...


@dataclass
class EngineAbortResult:
    thread_id: str
    engine: str
    external_run_id: str | None
    cancelled_runs: list[str] = field(default_factory=list)
    phase: str | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class _AbortTarget:
    handle: EngineHandle
    connection_version: datetime | None = None
    project_id: str | None = None
    app_id: str | None = None
    attempt: int | None = None
    phase: str | None = None
    projection_id: str | None = None
    projection_external_run_id: str | None = None
    bound_at: datetime | None = None


@dataclass(frozen=True)
class _StateTarget:
    found: bool
    attempt_bound: bool
    target: _AbortTarget | None
    attempt: int | None = None
    phase_status: str | None = None


class EngineAbortService:
    """Constructed per-request with a loopback client carrying the caller's key."""

    def __init__(
        self,
        client: LangGraphClientLike,
        repo: EngineRunsRepoLike,
        resolver: ConnectionResolver | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._client = client
        self._repo = repo
        self._resolver = resolver if resolver is not None else get_connection_resolver()
        self._allowed_scopes = tuple(allowed_scopes) if allowed_scopes is not None else None
        self._allowed_project_ids = allowed_project_ids

    async def abort(self, thread_id: str, *, reason: str | None = None) -> EngineAbortResult:
        runs_before_target = await self._active_graph_runs(thread_id)
        state_target = await self._target_from_state(thread_id)
        projection_target = await self._target_from_projection(thread_id)
        target: _AbortTarget | None
        if state_target.attempt_bound:
            # A modern checkpoint explicitly identifies the current execution
            # attempt. Never fall back to an older projection when that attempt is
            # pending, terminal, or otherwise has no live provider handle.
            target = state_target.target
            if (
                target is None
                and projection_target is not None
                and projection_target.attempt == state_target.attempt
            ):
                if projection_target.handle.external_run_id:
                    # The required handle projection committed before its graph
                    # checkpoint. It is safe to kill this exact current attempt.
                    target = projection_target
                elif projection_target.phase == EngineRunPhase.PROVISIONING.value:
                    # Do not graph-cancel while provider creation may be in flight;
                    # retry once provisioning checkpoints or settles the lease.
                    await self._release_read_transaction()
                    raise EngineProvisioningAbortPendingError(thread_id)
        elif not state_target.found:
            # Projection-only recovery for a pruned/missing LangGraph thread.
            target = projection_target
        else:
            # Legacy checkpoints did not bind handles to phase attempts. A terminal
            # latest projection proves their top-level handle is historical. A live
            # projection (or active graph run when projection writes were unavailable)
            # is required before trusting it.
            latest = await self._repo.get_latest_for_thread(
                thread_id,
                allowed_scopes=self._allowed_scopes,
                allowed_project_ids=self._allowed_project_ids,
            )
            latest_status = _optional_str(getattr(latest, "status", None))
            if latest_status in {phase.value for phase in TERMINAL_ENGINE_PHASES}:
                target = None
            elif projection_target is not None:
                target = state_target.target or projection_target
            elif state_target.target is not None and runs_before_target:
                target = state_target.target
            else:
                target = None

        if (
            target is not None
            and projection_target is not None
            and (
                target.attempt is None
                or projection_target.attempt is None
                or target.attempt == projection_target.attempt
            )
        ):
            # Bind checkpoint state to the exact durable projection identity before
            # borrowing its scope/generation.  A stale legacy state handle must not
            # abort one provider run and then terminalize a different projection.
            try:
                target = _merge_abort_targets(target, projection_target)
            except ValueError:
                await self._release_read_transaction()
                raise EngineConnectionAffinityMissingError from None
        if target is None:
            await self._release_read_transaction()
            raise EngineRunNotFoundError(thread_id)
        if get_settings().is_locked_down and (
            not target.handle.connection_id or target.connection_version is None
        ):
            # A provider-local id plus connection row id is still insufficient when
            # that row can be edited in place.  Without its runtime generation, an
            # abort can be redirected to today's endpoint for the same row.
            await self._release_read_transaction()
            raise EngineConnectionAffinityMissingError

        # Repository discovery uses the request-scoped session. End that read-only
        # transaction before resolver/provider/loopback I/O so an abort cannot pin
        # a database snapshot or pooled connection for the duration of a remote
        # stop. The exact terminal CAS below starts a fresh transaction.
        await self._release_read_transaction()

        # Only runs active on both sides of target discovery, and created no later
        # than that target's checkpoint/projection, can belong to this attempt.
        # Missing timestamps fail closed and leave the graph poller to settle.
        runs_after_target = await self._active_graph_runs(thread_id)
        monitor_run_ids = _stable_monitor_candidate_ids(
            runs_before_target,
            runs_after_target,
            target,
        )

        return await self._abort_target(
            thread_id,
            target,
            state_target,
            monitor_run_ids,
            reason=reason,
        )

    async def _abort_target(
        self,
        thread_id: str,
        target: _AbortTarget,
        state_target: _StateTarget,
        monitor_run_ids: tuple[str, ...],
        *,
        reason: str | None,
    ) -> EngineAbortResult:
        adapter: Any | None = None
        try:
            adapter, handle = await self._resolve_abort_adapter(target)
            return await self._abort_using_adapter(
                thread_id,
                target,
                state_target,
                monitor_run_ids,
                adapter,
                handle,
                reason=reason,
            )
        finally:
            if adapter is not None:
                try:
                    await close_adapter(adapter)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - close cannot change kill outcome
                    logger.warning(
                        "engine_abort.adapter_close_failed",
                        thread_id=thread_id,
                        error=bounded_diagnostic(exc),
                    )

    async def _resolve_abort_adapter(self, target: _AbortTarget) -> tuple[Any, EngineHandle]:
        handle = _copy_engine_handle(target.handle)
        adapter: Any | None = None
        try:
            resolve_with_metadata = getattr(self._resolver, "resolve_with_metadata", None)
            if callable(resolve_with_metadata):
                metadata_resolver = cast(Callable[..., Awaitable[Any]], resolve_with_metadata)
                resolved = await metadata_resolver(
                    PortKind.EXECUTION_ENGINE,
                    connection_id=handle.connection_id,
                    project_id=target.project_id,
                    expected_provider=handle.engine,
                )
                if not isinstance(resolved, ResolvedAdapter):
                    raise RuntimeError("engine resolver returned invalid connection metadata")
                adapter = resolved.adapter
                if resolved.connection_id != handle.connection_id:
                    raise RuntimeError(
                        "engine resolver did not honor the abort connection affinity"
                    )
                if target.connection_version is not None and (
                    not resolved.persisted
                    or _optional_datetime(resolved.connection_version) != target.connection_version
                ):
                    raise RuntimeError(
                        "engine connection changed after the run reserved its generation"
                    )
                resolved_connection_id = resolved.connection_id
            else:
                if target.connection_version is not None and get_settings().is_locked_down:
                    raise RuntimeError(
                        "engine resolver cannot verify the abort connection generation"
                    )
                adapter, resolved_connection_id = await self._resolver.resolve_with_connection_id(
                    PortKind.EXECUTION_ENGINE,
                    connection_id=handle.connection_id,
                    project_id=target.project_id,
                    expected_provider=handle.engine,
                )
            handle = EngineHandle(
                engine=handle.engine,
                connection_id=resolved_connection_id,
                external_run_id=handle.external_run_id,
                idempotency_key=handle.idempotency_key,
                extras=dict(handle.extras),
            )
            return adapter, handle
        except BaseException as exc:  # adapter checkout must be released on cancellation too
            if adapter is not None:
                try:
                    await close_adapter(adapter)
                except BaseException:  # preserve the original resolution/cancellation failure
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise EngineProviderAbortError(
                "engine provider could not be resolved for abort"
            ) from exc

    async def _abort_using_adapter(
        self,
        thread_id: str,
        target: _AbortTarget,
        state_target: _StateTarget,
        monitor_run_ids: tuple[str, ...],
        adapter: Any,
        handle: EngineHandle,
        *,
        reason: str | None,
    ) -> EngineAbortResult:
        observed_phase: EngineRunPhase | None = None
        confirmed = False
        provider_handle = _copy_engine_handle(handle)
        try:
            await adapter.abort(provider_handle, reason=_abort_reason(reason))
        except Exception as exc:  # noqa: BLE001 - provider boundary
            # Provider code must not be able to forge one of the service's
            # retryable finalization exceptions and change the HTTP outcome.
            raise EngineProviderAbortError("engine provider abort failed") from exc

        try:
            # Provider adapters legitimately enrich ``extras`` while reconciling an
            # ambiguous run, but affinity and durable resource identity come only
            # from the checkpoint/projection and resolved connection. Never let a
            # plugin redirect subsequent status/teardown calls by mutating them.
            handle = _restamp_provider_handle(handle, provider_handle)
            observed_phase = await self._provider_phase(adapter, handle)
            has_active_monitor = await self._has_active_target_run(thread_id, monitor_run_ids)
            if observed_phase is EngineRunPhase.STOPPING:
                # Check the durable run queue, not merely checkpoint existence:
                # completed/cancelled graph runs retain their state and handle.
                if not has_active_monitor:
                    observed_phase = await self._confirm_without_graph_monitor(
                        thread_id, adapter, handle
                    )
            confirmed = observed_phase in TERMINAL_ENGINE_PHASES
            if observed_phase not in {
                *TERMINAL_ENGINE_PHASES,
                EngineRunPhase.STOPPING,
            }:
                raise RuntimeError(
                    f"external engine remains nonterminal after abort: {observed_phase.value}"
                )
        except (
            EngineAbortConfirmationPendingError,
            EngineGraphFinalizationPendingError,
            EngineProjectionFinalizationPendingError,
        ):
            raise
        except Exception as exc:
            # Do not cancel the poller or claim the projection is aborted when the
            # external provider rejected/failed the kill. The operator must see the
            # failure and a still-live graph can continue observing the run.
            raise EngineProviderAbortError("engine provider abort failed") from exc

        projection_only = not state_target.found
        if confirmed and has_active_monitor:
            # The monitor owns collection, teardown, terminal projection, and
            # the final execution checkpoint. Cancelling it here loses results
            # when abort races with a natural provider completion/failure. This
            # remains true even if a state read briefly cannot see the thread.
            return EngineAbortResult(
                thread_id=thread_id,
                engine=handle.engine,
                external_run_id=handle.external_run_id,
                phase=observed_phase.value,
                confirmed=True,
            )

        if confirmed and state_target.found:
            # The captured monitor may have completed while the provider abort was
            # being confirmed. Re-read state before declaring recovery necessary.
            latest_state = await self._target_from_state(thread_id)
            if _state_requires_finalization(latest_state, target):
                raise EngineGraphFinalizationPendingError(thread_id)
            if _state_proves_target_finalized(latest_state, target):
                # A terminal checkpoint proves the graph already performed its own
                # collection/teardown/projection sequence. Do not repeat it here.
                return EngineAbortResult(
                    thread_id=thread_id,
                    engine=handle.engine,
                    external_run_id=handle.external_run_id,
                    phase=observed_phase.value,
                    confirmed=True,
                )
            projection_only = True

        if confirmed and projection_only:
            # A projection can become visible just before its graph checkpoint,
            # while the loopback state read still briefly returns 404. Recheck
            # after the provider stop before destructive teardown so that a newly
            # visible checkpoint/monitor retains ownership of result collection.
            latest_state = await self._target_from_state(thread_id)
            if _state_requires_finalization(latest_state, target):
                raise EngineGraphFinalizationPendingError(thread_id)
            if _state_proves_target_finalized(latest_state, target):
                return EngineAbortResult(
                    thread_id=thread_id,
                    engine=handle.engine,
                    external_run_id=handle.external_run_id,
                    phase=observed_phase.value,
                    confirmed=True,
                )

        # There is no checkpoint/monitor that can collect or tear down a
        # projection-only provider run, so the abort service owns finalization.
        if confirmed and projection_only:
            try:
                await adapter.teardown(_copy_engine_handle(handle))
            except Exception as exc:  # noqa: BLE001 — retain the lease for exact retry
                logger.warning(
                    "engine_abort.teardown_failed",
                    thread_id=thread_id,
                    error=bounded_diagnostic(exc),
                )
                # Do not mark the projection terminal and release its connection
                # lease while provider resources remain undisposed. A later abort
                # retries the idempotent abort + teardown from the same durable
                # handle even though the external load is already stopped.
                raise EngineProjectionFinalizationPendingError(thread_id) from exc

        if (
            confirmed
            and projection_only
            and target.projection_id is not None
            and target.attempt is not None
        ):
            try:
                marker = getattr(self._repo, "mark_terminal", None)
                if marker is not None:
                    projected = await marker(
                        thread_id,
                        observed_phase.value,
                        projection_id=target.projection_id,
                        attempt=target.attempt,
                        expected_external_run_id=target.projection_external_run_id,
                        allowed_scopes=self._allowed_scopes,
                        allowed_project_ids=self._allowed_project_ids,
                    )
                elif observed_phase is EngineRunPhase.ABORTED:
                    projected = await self._repo.mark_aborted(
                        thread_id,
                        projection_id=target.projection_id,
                        attempt=target.attempt,
                        expected_external_run_id=target.projection_external_run_id,
                        allowed_scopes=self._allowed_scopes,
                        allowed_project_ids=self._allowed_project_ids,
                    )
                else:
                    projected = 0
            except Exception as exc:  # noqa: BLE001 — surface retryable durable finalization
                logger.warning(
                    "engine_abort.projection_update_failed",
                    thread_id=thread_id,
                    error=bounded_diagnostic(exc),
                )
                raise EngineProjectionFinalizationPendingError(thread_id) from exc
            else:
                if projected == 0:
                    logger.warning("engine_abort.projection_update_empty", thread_id=thread_id)
                    # Teardown succeeded, but the compare-and-set did not prove
                    # that this exact durable lease became terminal. Report a
                    # retryable finalization gap instead of returning a false
                    # success while a nonterminal projection may remain active.
                    raise EngineProjectionFinalizationPendingError(thread_id)

        return EngineAbortResult(
            thread_id=thread_id,
            engine=handle.engine,
            external_run_id=handle.external_run_id,
            phase=observed_phase.value if observed_phase is not None else None,
            confirmed=confirmed,
        )

    # ── handle discovery ─────────────────────────────────────────────────────

    async def _provider_phase(self, adapter: Any, handle: EngineHandle) -> EngineRunPhase:
        """Normalize provider disappearance as a confirmed abort on every poll."""

        try:
            status = await adapter.get_status(_copy_engine_handle(handle))
        except EngineProviderRunNotFoundError:
            return EngineRunPhase.ABORTED
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise EngineProviderAbortError("engine provider status check failed") from exc
        try:
            dump = getattr(status, "model_dump", None)
            payload = dump(mode="python") if callable(dump) else status
            return EngineRunStatus.model_validate(payload).phase
        except Exception as exc:  # noqa: BLE001 - untrusted provider result
            raise EngineProviderAbortError("engine provider returned an invalid status") from exc

    async def _confirm_without_graph_monitor(
        self, thread_id: str, adapter: Any, handle: EngineHandle
    ) -> EngineRunPhase:
        """Synchronously confirm STOPPING or return a retryable API failure.

        The checkpoint/projection handle remains untouched on timeout, so a
        subsequent operator retry can drive the idempotent provider abort again.
        """

        for _ in range(STOPPING_CONFIRM_ATTEMPTS):
            await asyncio.sleep(STOPPING_CONFIRM_INTERVAL_S)
            phase = await self._provider_phase(adapter, handle)
            if phase in TERMINAL_ENGINE_PHASES:
                return phase
            if phase is not EngineRunPhase.STOPPING:
                raise RuntimeError(
                    f"external engine remains nonterminal after abort: {phase.value}"
                )
        raise EngineAbortConfirmationPendingError(thread_id)

    async def _target_from_state(self, thread_id: str) -> _StateTarget:
        try:
            state = await self._client.threads.get_state(thread_id)
        except NotFoundError:
            return _StateTarget(found=False, attempt_bound=False, target=None, attempt=None)
        values = state.get("values") or {}
        values = values if isinstance(values, dict) else {}
        results = values.get("phase_results")
        execution = results.get(Phase.EXECUTION.value) if isinstance(results, dict) else None
        attempt_bound = isinstance(execution, dict)
        if attempt_bound:
            assert isinstance(execution, dict)
            phase_status = _optional_str(execution.get("status"))
            if phase_status != PhaseStatus.RUNNING.value:
                return _StateTarget(
                    found=True,
                    attempt_bound=True,
                    target=None,
                    attempt=_optional_int(execution.get("attempt")),
                    phase_status=phase_status,
                )
            raw = execution.get("engine_handle")
            attempt = _optional_int(execution.get("attempt"))
        else:
            raw = _find_engine_handle(state)
            attempt = None
            phase_status = None
        if not isinstance(raw, dict):
            return _StateTarget(
                found=True,
                attempt_bound=attempt_bound,
                target=None,
                attempt=attempt,
                phase_status=phase_status,
            )
        try:
            handle = EngineHandle.model_validate(raw)
        except ValidationError:
            logger.warning("engine_abort.malformed_state_handle", thread_id=thread_id)
            return _StateTarget(
                found=True,
                attempt_bound=attempt_bound,
                target=None,
                attempt=attempt,
                phase_status=phase_status,
            )
        run_config = values.get("run_config")
        run_config = run_config if isinstance(run_config, dict) else {}
        return _StateTarget(
            found=True,
            attempt_bound=attempt_bound,
            target=_AbortTarget(
                handle=handle,
                connection_version=(
                    _optional_datetime(execution.get("engine_connection_version"))
                    if isinstance(execution, dict)
                    else None
                ),
                project_id=_optional_str(run_config.get("project_id")),
                app_id=_optional_str(run_config.get("app_id")),
                attempt=attempt,
                bound_at=_optional_datetime(state.get("created_at")),
            ),
            attempt=attempt,
            phase_status=phase_status,
        )

    async def _target_from_projection(self, thread_id: str) -> _AbortTarget | None:
        row = await self._repo.get_latest_abortable_for_thread(
            thread_id,
            allowed_scopes=self._allowed_scopes,
            allowed_project_ids=self._allowed_project_ids,
        )
        if row is None:
            return None
        try:
            persisted_handle = EngineHandle.model_validate(row.handle or {})
        except ValidationError:
            persisted_handle = None
        raw_connection_id = getattr(row, "connection_id", None)
        # Projection columns are the scoped, queryable source of truth. Handle JSON
        # is retained for provider extras/idempotency and as a legacy affinity
        # fallback only where an older row genuinely has no projected value.
        handle = EngineHandle(
            engine=row.engine,
            connection_id=_optional_str(raw_connection_id)
            or (persisted_handle.connection_id if persisted_handle is not None else None),
            external_run_id=_optional_str(row.external_run_id)
            or (persisted_handle.external_run_id if persisted_handle is not None else None),
            idempotency_key=(
                persisted_handle.idempotency_key
                if persisted_handle is not None
                else f"abort:{thread_id}:{getattr(row, 'attempt', 0)}"
            ),
            extras=dict(persisted_handle.extras) if persisted_handle is not None else {},
        )
        return _AbortTarget(
            handle=handle,
            connection_version=_optional_datetime(
                getattr(row, "execution_connection_version", None)
            ),
            project_id=_optional_str(getattr(row, "project_id", None)),
            app_id=_optional_str(getattr(row, "app_id", None)),
            attempt=_optional_int(getattr(row, "attempt", None)),
            phase=_optional_str(getattr(row, "status", None)),
            projection_id=_optional_str(getattr(row, "id", None)),
            projection_external_run_id=_optional_str(getattr(row, "external_run_id", None)),
            bound_at=_optional_datetime(getattr(row, "started_at", None)),
        )

    # ── graph-run cancellation ───────────────────────────────────────────────

    async def _active_graph_runs(self, thread_id: str) -> dict[str, dict[str, Any]]:
        previous: frozenset[str] | None = None
        latest: dict[str, dict[str, Any]] = {}
        for _ in range(ACTIVE_RUN_STABILITY_ATTEMPTS):
            latest = await self._active_graph_runs_once(thread_id)
            current = frozenset(latest)
            if previous is not None and current == previous:
                return latest
            previous = current
            await asyncio.sleep(0)
        raise ActiveRunSnapshotUnstableError(thread_id)

    async def _active_graph_runs_once(self, thread_id: str) -> dict[str, dict[str, Any]]:
        runs: dict[str, dict[str, Any]] = {}
        fetched = 0
        try:
            for status in ("running", "pending"):
                offset = 0
                while True:
                    remaining = MAX_ACTIVE_RUN_SNAPSHOT + 1 - fetched
                    if remaining <= 0:
                        logger.warning(
                            "engine_abort.active_run_snapshot_truncated",
                            thread_id=thread_id,
                            limit=MAX_ACTIVE_RUN_SNAPSHOT,
                        )
                        raise TooManyActiveRunsError(thread_id, MAX_ACTIVE_RUN_SNAPSHOT)
                    limit = min(ACTIVE_RUN_PAGE_SIZE, remaining)
                    page = await self._client.runs.list(
                        thread_id,
                        status=status,
                        limit=limit,
                        offset=offset,
                        select=["run_id", "status", "created_at"],
                    )
                    if not page:
                        break
                    fetched += len(page)
                    if fetched > MAX_ACTIVE_RUN_SNAPSHOT:
                        logger.warning(
                            "engine_abort.active_run_snapshot_truncated",
                            thread_id=thread_id,
                            limit=MAX_ACTIVE_RUN_SNAPSHOT,
                        )
                        raise TooManyActiveRunsError(thread_id, MAX_ACTIVE_RUN_SNAPSHOT)
                    for run in page:
                        run_id = _optional_str(run.get("run_id"))
                        if run_id is not None and run_id not in runs:
                            runs[run_id] = dict(run)
                    offset += len(page)
        except NotFoundError:
            return {}
        return runs

    async def _has_active_target_run(self, thread_id: str, target_run_ids: tuple[str, ...]) -> bool:
        if not target_run_ids:
            return False
        active = set(await self._active_graph_runs(thread_id))
        return any(run_id in active for run_id in target_run_ids)

    async def _release_read_transaction(self) -> None:
        release = getattr(self._repo, "release_read_transaction", None)
        if release is not None:
            await release()


def _find_engine_handle(value: Any) -> dict[str, Any] | None:
    """Find a handle in bounded nested-subgraph state returned by the SDK."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()
    visited = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_STATE_HANDLE_SEARCH_DEPTH:
            continue
        if not isinstance(current, dict | list):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        visited += 1
        if visited > MAX_STATE_HANDLE_SEARCH_NODES:
            return None
        if isinstance(current, dict):
            direct = current.get("engine_handle")
            if isinstance(direct, dict):
                return direct
            # Reverse push order preserves the old depth-first values/state/tasks
            # preference while avoiding Python recursion and cyclic test doubles.
            for key in reversed(("values", "state", "tasks")):
                stack.append((current.get(key), depth + 1))
        else:
            for item in reversed(current):
                stack.append((item, depth + 1))
    return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _copy_engine_handle(handle: EngineHandle) -> EngineHandle:
    """Return a validated deep copy before crossing an adapter/plugin boundary."""

    return EngineHandle.model_validate(handle.model_dump(mode="python")).model_copy(deep=True)


def _merge_abort_targets(primary: _AbortTarget, projection: _AbortTarget) -> _AbortTarget:
    """Merge projection ownership only when both targets name one exact run."""

    left = primary.handle
    right = projection.handle
    if left.engine != right.engine or left.idempotency_key != right.idempotency_key:
        raise ValueError("engine abort targets have different durable identities")
    if (
        left.connection_id is not None
        and right.connection_id is not None
        and left.connection_id != right.connection_id
    ):
        raise ValueError("engine abort targets have different connection affinity")
    if (
        left.external_run_id is not None
        and right.external_run_id is not None
        and left.external_run_id != right.external_run_id
    ):
        raise ValueError("engine abort targets have different external run ids")
    if (
        primary.project_id is not None
        and projection.project_id is not None
        and primary.project_id != projection.project_id
    ) or (
        primary.app_id is not None
        and projection.app_id is not None
        and primary.app_id != projection.app_id
    ):
        raise ValueError("engine abort targets have different ownership")
    if (
        primary.connection_version is not None
        and projection.connection_version is not None
        and primary.connection_version != projection.connection_version
    ):
        raise ValueError("engine abort targets have different connection generations")

    # The required projection is the durable provider-output barrier and normally
    # carries the richest handle.  A state-only external id is retained for older
    # rows that were projected before start output was captured.
    source = right if right.external_run_id is not None else left
    handle = EngineHandle(
        engine=left.engine,
        connection_id=left.connection_id or right.connection_id,
        external_run_id=left.external_run_id or right.external_run_id,
        idempotency_key=left.idempotency_key,
        extras=dict(source.extras),
    )
    return _AbortTarget(
        handle=handle,
        connection_version=primary.connection_version or projection.connection_version,
        project_id=primary.project_id or projection.project_id,
        app_id=primary.app_id or projection.app_id,
        attempt=primary.attempt or projection.attempt,
        phase=primary.phase or projection.phase,
        projection_id=projection.projection_id,
        projection_external_run_id=projection.projection_external_run_id,
        # The checkpoint timestamp shares the run server's clock and is the
        # tightest boundary. Projection time is the legacy fallback.
        bound_at=primary.bound_at or projection.bound_at,
    )


def _restamp_provider_handle(
    trusted: EngineHandle,
    provider_handle: EngineHandle,
) -> EngineHandle:
    """Accept only validated provider-owned extras; retain trusted affinity."""

    candidate = EngineHandle.model_validate(provider_handle.model_dump(mode="python"))
    return EngineHandle(
        engine=trusted.engine,
        connection_id=trusted.connection_id,
        external_run_id=trusted.external_run_id,
        idempotency_key=trusted.idempotency_key,
        extras=dict(candidate.extras),
    )


def _abort_reason(reason: str | None) -> str:
    value = reason or DEFAULT_ABORT_REASON
    if len(value) > 1_024 or "\x00" in value:
        raise ValueError("engine abort reason must be at most 1024 characters without U+0000")
    return value


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _state_requires_finalization(state: _StateTarget, target: _AbortTarget) -> bool:
    """Whether this exact provider attempt still has a RUNNING graph checkpoint."""

    if not state.found:
        return False
    if state.attempt_bound:
        if (
            target.attempt is not None
            and state.attempt is not None
            and state.attempt != target.attempt
        ):
            return False
        # A same-attempt nonterminal checkpoint retains ownership even when its
        # provider handle has not yet become visible. A conflicting same-attempt
        # handle is also unsafe to tear down from this recovery path.
        if state.phase_status not in {status.value for status in TERMINAL_PHASE_STATUSES}:
            return True
        return state.target is not None and not _targets_name_same_run(state.target, target)
    # Legacy checkpoints have no attempt/status binding. A retained handle is the
    # only evidence available and must fail closed instead of deleting results.
    return state.target is not None


def _state_proves_target_finalized(state: _StateTarget, target: _AbortTarget) -> bool:
    """Return true only for a terminal checkpoint bound to this exact attempt."""

    if not state.found or not state.attempt_bound:
        return False
    if state.attempt is None or target.attempt is None or state.attempt != target.attempt:
        return False
    if state.phase_status not in {status.value for status in TERMINAL_PHASE_STATUSES}:
        return False
    return state.target is None or _targets_name_same_run(state.target, target)


def _targets_name_same_run(left: _AbortTarget, right: _AbortTarget) -> bool:
    try:
        _merge_abort_targets(left, right)
    except ValueError:
        return False
    return True


def _stable_monitor_candidate_ids(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    target: _AbortTarget,
) -> tuple[str, ...]:
    stable = before.keys() & after.keys()
    if target.bound_at is None:
        # Without a trustworthy target timestamp, every stable run is a possible
        # results monitor. Preserving an unrelated run is safer than deleting the
        # target provider's only recoverable output.
        return tuple(run_id for run_id in before if run_id in stable)
    return tuple(
        run_id
        for run_id in before
        if run_id in stable
        and (
            (
                created_at := _optional_datetime(
                    after[run_id].get("created_at") or before[run_id].get("created_at")
                )
            )
            is None
            or created_at <= target.bound_at
        )
    )
