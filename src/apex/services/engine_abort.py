"""Engine-level abort: kill the external load run when graph-level cancel isn't enough.

Flow (plan Part 1, "Durability & the execution phase" — the engine kill switch):

1. Discover the EngineHandle: thread state ``values.engine_handle`` via the loopback
   LangGraph API (the caller's key is forwarded, so @auth.on scoping applies), falling
   back to the latest ``engine_runs`` projection row;
2. resolve the engine adapter with the run's persisted project scope;
3. ``adapter.abort(handle, reason=...)`` — failures propagate and leave the graph
   poller alive so a failed kill never strands the external load;
4. cancel the thread's pending/running LangGraph runs only after the kill lands;
5. best-effort projection update to status "aborted" (the projection never gates an
   abort, mirroring the write side in apex.services.engine_runs).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from langgraph_sdk.errors import NotFoundError
from pydantic import ValidationError

from apex.adapters.registry import PortKind
from apex.auth.identity import ScopeRef
from apex.domain.pipeline import EngineHandle
from apex.services.connections import ConnectionResolver, get_connection_resolver
from apex.services.pipeline_read import LangGraphClientLike

logger = structlog.get_logger(__name__)

DEFAULT_ABORT_REASON = "operator abort"


class EngineRunNotFoundError(Exception):
    """No engine handle is discoverable for the thread (neither state nor projection)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"no engine run found for thread {thread_id!r}")


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
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int: ...


@dataclass
class EngineAbortResult:
    thread_id: str
    engine: str
    external_run_id: str | None
    cancelled_runs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AbortTarget:
    handle: EngineHandle
    project_id: str | None = None
    app_id: str | None = None


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
        target = await self._target_from_state(thread_id)
        projection_target: _AbortTarget | None = None
        if target is None or target.project_id is None:
            projection_target = await self._target_from_projection(thread_id)
        if target is None:
            target = projection_target
        elif projection_target is not None:
            # Old checkpoints predate run_config. Keep the state handle, but use
            # projection ownership to resolve project-scoped connections safely.
            target = _AbortTarget(
                handle=target.handle,
                project_id=target.project_id or projection_target.project_id,
                app_id=target.app_id or projection_target.app_id,
            )
        if target is None:
            raise EngineRunNotFoundError(thread_id)

        handle = target.handle
        adapter, _resolved_connection_id = await self._resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE,
            connection_id=handle.connection_id,
            project_id=target.project_id,
            expected_provider=handle.engine,
        )
        try:
            await adapter.abort(handle, reason=reason or DEFAULT_ABORT_REASON)
        except Exception:
            # Do not cancel the poller or claim the projection is aborted when the
            # external provider rejected/failed the kill. The operator must see the
            # failure and a still-live graph can continue observing the run.
            raise
        finally:
            try:
                await adapter.teardown(handle)
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort after a kill
                logger.warning("engine_abort.teardown_failed", thread_id=thread_id, error=str(exc))

        cancelled = await self._cancel_runs(thread_id)

        try:
            projected = await self._repo.mark_aborted(
                thread_id,
                allowed_scopes=self._allowed_scopes,
                allowed_project_ids=self._allowed_project_ids,
            )
        except Exception as exc:  # noqa: BLE001 — projection writes never gate an abort
            logger.warning(
                "engine_abort.projection_update_failed", thread_id=thread_id, error=str(exc)
            )
        else:
            if projected == 0:
                logger.warning("engine_abort.projection_update_empty", thread_id=thread_id)

        return EngineAbortResult(
            thread_id=thread_id,
            engine=handle.engine,
            external_run_id=handle.external_run_id,
            cancelled_runs=cancelled,
        )

    # ── handle discovery ─────────────────────────────────────────────────────

    async def _target_from_state(self, thread_id: str) -> _AbortTarget | None:
        try:
            state = await self._client.threads.get_state(thread_id)
        except NotFoundError:
            return None
        values = state.get("values") or {}
        raw = _find_engine_handle(state)
        if not isinstance(raw, dict):
            return None
        try:
            handle = EngineHandle.model_validate(raw)
        except ValidationError:
            logger.warning("engine_abort.malformed_state_handle", thread_id=thread_id)
            return None
        run_config = values.get("run_config") if isinstance(values, dict) else None
        run_config = run_config if isinstance(run_config, dict) else {}
        return _AbortTarget(
            handle=handle,
            project_id=_optional_str(run_config.get("project_id")),
            app_id=_optional_str(run_config.get("app_id")),
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
            handle = EngineHandle.model_validate(row.handle or {})
        except ValidationError:
            # The row always carries engine/external_run_id columns even when the
            # handle JSON is degenerate — synthesize the minimal handle.
            handle = EngineHandle(engine=row.engine, external_run_id=row.external_run_id)
        return _AbortTarget(
            handle=handle,
            project_id=_optional_str(getattr(row, "project_id", None)),
            app_id=_optional_str(getattr(row, "app_id", None)),
        )

    # ── graph-run cancellation ───────────────────────────────────────────────

    async def _cancel_runs(self, thread_id: str) -> list[str]:
        cancelled: list[str] = []
        try:
            for status in ("running", "pending"):
                for run in await self._client.runs.list(thread_id, status=status):
                    await self._client.runs.cancel(thread_id, run["run_id"])
                    cancelled.append(run["run_id"])
        except NotFoundError:
            # Thread unknown to the loopback API (projection-only handle): the engine
            # abort already happened; there is nothing graph-side to cancel.
            logger.info("engine_abort.no_thread_runs", thread_id=thread_id)
        return cancelled


def _find_engine_handle(value: Any) -> dict[str, Any] | None:
    """Find a handle in top-level or nested-subgraph state returned by the SDK."""
    if isinstance(value, dict):
        direct = value.get("engine_handle")
        if isinstance(direct, dict):
            return direct
        for key in ("values", "state", "tasks"):
            found = _find_engine_handle(value.get(key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_engine_handle(item)
            if found is not None:
                return found
    return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
