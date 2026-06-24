"""Engine-level abort: kill the external load run when graph-level cancel isn't enough.

Flow (plan Part 1, "Durability & the execution phase" — the engine kill switch):

1. Discover the EngineHandle: thread state ``values.engine_handle`` via the loopback
   LangGraph API (the caller's key is forwarded, so @auth.on scoping applies), falling
   back to the latest ``engine_runs`` projection row;
2. cancel the thread's pending/running LangGraph runs (tolerates none — the poll loop
   may already be gone while the external run is still burning);
3. resolve the engine adapter from handle.connection_id via the connection resolver;
4. ``adapter.abort(handle, reason=...)`` — failures propagate (the operator must know
   the kill did not land) — then best-effort ``teardown``;
5. best-effort projection update to status "aborted" (the projection never gates an
   abort, mirroring the write side in apex.services.engine_runs).
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from langgraph_sdk.errors import NotFoundError
from pydantic import ValidationError

from apex.adapters.registry import PortKind
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
        self, thread_id: str, *, allowed_project_ids: tuple[str, ...] | None = None
    ) -> Any: ...

    async def mark_aborted(
        self, thread_id: str, *, allowed_project_ids: tuple[str, ...] | None = None
    ) -> int: ...


@dataclass
class EngineAbortResult:
    thread_id: str
    engine: str
    external_run_id: str | None
    cancelled_runs: list[str] = field(default_factory=list)


class EngineAbortService:
    """Constructed per-request with a loopback client carrying the caller's key."""

    def __init__(
        self,
        client: LangGraphClientLike,
        repo: EngineRunsRepoLike,
        resolver: ConnectionResolver | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._client = client
        self._repo = repo
        self._resolver = resolver if resolver is not None else get_connection_resolver()
        self._allowed_project_ids = allowed_project_ids

    async def abort(self, thread_id: str, *, reason: str | None = None) -> EngineAbortResult:
        handle = await self._handle_from_state(thread_id)
        if handle is None:
            handle = await self._handle_from_projection(thread_id)
        if handle is None:
            raise EngineRunNotFoundError(thread_id)

        cancelled = await self._cancel_runs(thread_id)
        adapter = await self._resolver.resolve(
            PortKind.EXECUTION_ENGINE, connection_id=handle.connection_id
        )
        abort_error: Exception | None = None
        try:
            await adapter.abort(handle, reason=reason or DEFAULT_ABORT_REASON)
        except Exception as exc:  # noqa: BLE001 — operator must still see the failed kill
            abort_error = exc
        try:
            await adapter.teardown(handle)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort after a kill
            logger.warning("engine_abort.teardown_failed", thread_id=thread_id, error=str(exc))

        try:
            await self._repo.mark_aborted(thread_id, allowed_project_ids=self._allowed_project_ids)
        except Exception as exc:  # noqa: BLE001 — projection writes never gate an abort
            logger.warning(
                "engine_abort.projection_update_failed", thread_id=thread_id, error=str(exc)
            )

        if abort_error is not None:
            raise abort_error

        return EngineAbortResult(
            thread_id=thread_id,
            engine=handle.engine,
            external_run_id=handle.external_run_id,
            cancelled_runs=cancelled,
        )

    # ── handle discovery ─────────────────────────────────────────────────────

    async def _handle_from_state(self, thread_id: str) -> EngineHandle | None:
        try:
            state = await self._client.threads.get_state(thread_id)
        except NotFoundError:
            return None
        raw = (state.get("values") or {}).get("engine_handle")
        if not isinstance(raw, dict):
            return None
        try:
            return EngineHandle.model_validate(raw)
        except ValidationError:
            logger.warning("engine_abort.malformed_state_handle", thread_id=thread_id)
            return None

    async def _handle_from_projection(self, thread_id: str) -> EngineHandle | None:
        row = await self._repo.get_latest_for_thread(
            thread_id, allowed_project_ids=self._allowed_project_ids
        )
        if row is None:
            return None
        try:
            return EngineHandle.model_validate(row.handle or {})
        except ValidationError:
            # The row always carries engine/external_run_id columns even when the
            # handle JSON is degenerate — synthesize the minimal handle.
            return EngineHandle(engine=row.engine, external_run_id=row.external_run_id)

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
