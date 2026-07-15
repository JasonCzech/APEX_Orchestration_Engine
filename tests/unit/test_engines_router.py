"""/engines history + abort against fake repo / loopback client / engine adapter."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError
from structlog.testing import capture_logs

import apex.services.engine_abort as engine_abort_service
from apex.adapters.registry import PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import (
    ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
    EngineConnectionAffinityMissingError,
    EngineHandle,
)
from apex.persistence.models import EngineRun
from apex.ports.execution_engine import (
    EngineProviderRunNotFoundError,
    EngineRunPhase,
    EngineRunStatus,
)
from apex.routers.engines import (
    get_engine_abort_service,
    get_engine_runs_repository,
    router,
)
from apex.services.connections import ResolvedAdapter
from apex.services.engine_abort import (
    EngineAbortService,
    EngineGraphFinalizationPendingError,
    EngineProviderAbortError,
)
from apex.services.pipeline_read import (
    TooManyActiveRunsError,
    engine_info_from_values,
    map_thread_summary,
)

JsonDict = dict[str, Any]

OPERATOR = ConsumerIdentity(
    consumer_id="op-1",
    name="op",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.OPERATOR,
    scopes=[ScopeRef(project_id="p1")],
)
VIEWER = ConsumerIdentity(
    consumer_id="view-1",
    name="viewer",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.VIEWER,
    scopes=[ScopeRef(project_id="p1")],
)
APP_VIEWER = ConsumerIdentity(
    consumer_id="app-view-1",
    name="app-viewer",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.VIEWER,
    scopes=[ScopeRef(project_id="p1", app_id="app-a")],
)
APP_OPERATOR = APP_VIEWER.model_copy(
    update={"consumer_id": "app-op-1", "name": "app-operator", "role": Role.OPERATOR}
)

T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _not_found() -> NotFoundError:
    request = httpx.Request("GET", "http://loopback/threads/x")
    return NotFoundError("not found", response=httpx.Response(404, request=request), body=None)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeEngineRunsRepository:
    """In-memory stand-in matching EngineRunsRepository's surface."""

    def __init__(self) -> None:
        self.rows: list[EngineRun] = []
        self.list_calls = 0
        self.thread_list_calls = 0
        self.aborted_threads: list[str] = []
        self.mark_aborted_error: Exception | None = None
        self.mark_terminal_result: int | None = None
        self.read_transactions_released = 0

    def seed(
        self,
        *,
        thread_id: str,
        attempt: int = 1,
        engine: str = "sim",
        status: str = "running",
        external_run_id: str | None = None,
        handle: JsonDict | None = None,
        project_id: str | None = "p1",
        app_id: str | None = None,
        ownership_known: bool = True,
        scope_ownership_known: bool | None = None,
        artifact_namespace: str | None = None,
        artifact_connection_id: str | None = None,
        connection_id: str | None = None,
        execution_connection_version: datetime | None = None,
        started_at: datetime | None = None,
    ) -> EngineRun:
        run = EngineRun(
            id=f"{thread_id}-{attempt}",
            thread_id=thread_id,
            project_id=project_id,
            app_id=app_id,
            ownership_known=ownership_known,
            scope_ownership_known=(
                ownership_known if scope_ownership_known is None else scope_ownership_known
            ),
            attempt=attempt,
            engine=engine,
            external_run_id=external_run_id,
            artifact_namespace=artifact_namespace,
            artifact_connection_id=artifact_connection_id,
            connection_id=connection_id,
            execution_connection_version=execution_connection_version,
            handle=handle if handle is not None else {},
            status=status,
            started_at=started_at or T0,
            ended_at=None,
            summary=None,
        )
        self.rows.append(run)
        return run

    @staticmethod
    def _visible(run: EngineRun, allowed_scopes: Sequence[ScopeRef]) -> bool:
        return any(
            run.project_id == scope.project_id
            and (
                scope.app_id is None
                or run.app_id == scope.app_id
                or (run.app_id is None and run.ownership_known and run.scope_ownership_known)
            )
            for scope in allowed_scopes
        )

    @staticmethod
    def _mutable(run: EngineRun, allowed_scopes: Sequence[ScopeRef]) -> bool:
        return any(
            run.project_id == scope.project_id
            and (scope.app_id is None or run.app_id == scope.app_id)
            for scope in allowed_scopes
        )

    async def list_runs(
        self,
        *,
        engine: str | None = None,
        status: str | None = None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EngineRun], int]:
        self.list_calls += 1
        rows = [
            r
            for r in self.rows
            if (engine is None or r.engine == engine) and (status is None or r.status == status)
        ]
        if allowed_scopes is not None:
            rows = [r for r in rows if self._visible(r, allowed_scopes)]
        elif allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id in allowed_project_ids]
        rows.sort(key=lambda r: (r.started_at, r.id), reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def list_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EngineRun]:
        self.thread_list_calls += 1
        rows = [r for r in self.rows if r.thread_id == thread_id]
        if allowed_scopes is not None:
            rows = [r for r in rows if self._visible(r, allowed_scopes)]
        elif allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id in allowed_project_ids]
        rows.sort(key=lambda r: r.attempt, reverse=True)
        return rows[offset : offset + limit]

    async def get_latest_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        rows = await self.list_for_thread(
            thread_id,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
            limit=1,
        )
        return rows[0] if rows else None

    async def get_latest_abortable_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        rows = [
            r
            for r in self.rows
            if r.thread_id == thread_id and r.status not in ("completed", "failed", "aborted")
        ]
        if allowed_scopes is not None:
            rows = [r for r in rows if self._mutable(r, allowed_scopes)]
        elif allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id in allowed_project_ids]
        rows.sort(key=lambda r: r.attempt, reverse=True)
        return rows[0] if rows else None

    async def get_by_external_run_id(
        self,
        external_run_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        rows = [r for r in self.rows if r.external_run_id == external_run_id]
        if allowed_scopes is not None:
            rows = [r for r in rows if self._visible(r, allowed_scopes)]
        elif allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id in allowed_project_ids]
        rows.sort(key=lambda r: (r.started_at, r.id), reverse=True)
        return rows[0] if rows else None

    async def mark_aborted(
        self,
        thread_id: str,
        *,
        projection_id: str,
        attempt: int,
        expected_external_run_id: str | None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int:
        return await self.mark_terminal(
            thread_id,
            "aborted",
            projection_id=projection_id,
            attempt=attempt,
            expected_external_run_id=expected_external_run_id,
            allowed_scopes=allowed_scopes,
            allowed_project_ids=allowed_project_ids,
        )

    async def mark_terminal(
        self,
        thread_id: str,
        status: str,
        *,
        projection_id: str,
        attempt: int,
        expected_external_run_id: str | None,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int:
        if self.mark_aborted_error is not None:
            raise self.mark_aborted_error
        if self.mark_terminal_result is not None:
            return self.mark_terminal_result
        if status == "aborted":
            self.aborted_threads.append(thread_id)
        count = 0
        for run in self.rows:
            if (
                run.id == projection_id
                and run.thread_id == thread_id
                and run.attempt == attempt
                and run.external_run_id == expected_external_run_id
                and (allowed_scopes is None or self._mutable(run, allowed_scopes))
                and (allowed_project_ids is None or run.project_id in allowed_project_ids)
                and run.status not in ("completed", "failed", "aborted")
            ):
                run.status = status
                run.ended_at = datetime.now(UTC)
                count += 1
        return count

    async def release_read_transaction(self) -> None:
        self.read_transactions_released += 1


class FakeThreads:
    def __init__(self, states: dict[str, JsonDict] | None = None) -> None:
        self.states = states or {}

    async def get_state(self, thread_id: str) -> JsonDict:
        try:
            return self.states[thread_id]
        except KeyError:
            raise _not_found() from None


class FakeRuns:
    def __init__(self, runs: list[JsonDict] | None = None, known_threads: set[str] | None = None):
        self.runs = [
            {
                **run,
                "created_at": run.get("created_at") or (T0 - timedelta(seconds=1)).isoformat(),
            }
            for run in (runs or [])
        ]
        self.known_threads = known_threads
        self.cancelled: list[tuple[str, str]] = []

    async def list(
        self,
        thread_id: str,
        *,
        status: str | None = None,
        limit: int = 10,
        offset: int = 0,
        **_: Any,
    ) -> list[JsonDict]:
        if self.known_threads is not None and thread_id not in self.known_threads:
            raise _not_found()
        rows = [r for r in self.runs if status is None or r.get("status") == status]
        return rows[offset : offset + limit]

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        self.cancelled.append((thread_id, run_id))
        self.runs = [run for run in self.runs if run.get("run_id") != run_id]


class FakeClient:
    def __init__(self, threads: FakeThreads, runs: FakeRuns) -> None:
        self.threads = threads
        self.runs = runs


class FakeEngineAdapter:
    def __init__(
        self,
        teardown_error: Exception | None = None,
        abort_error: Exception | None = None,
        status_phase: EngineRunPhase = EngineRunPhase.ABORTED,
        status_results: list[EngineRunPhase | Exception] | None = None,
    ) -> None:
        self.aborts: list[tuple[EngineHandle, str]] = []
        self.teardowns: list[EngineHandle] = []
        self.teardown_error = teardown_error
        self.abort_error = abort_error
        self.status_phase = status_phase
        self.status_results = list(status_results or [])

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        self.aborts.append((handle, reason))
        if self.abort_error is not None:
            raise self.abort_error

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        result: EngineRunPhase | Exception = (
            self.status_results.pop(0) if self.status_results else self.status_phase
        )
        if isinstance(result, Exception):
            raise result
        return EngineRunStatus(phase=result, progress_pct=100.0)

    async def teardown(self, handle: EngineHandle) -> None:
        if self.teardown_error is not None:
            raise self.teardown_error
        self.teardowns.append(handle)


class FakeResolver:
    def __init__(self, adapter: FakeEngineAdapter, *, provider: str = "sim") -> None:
        self.adapter = adapter
        self.provider = provider
        self.calls: list[tuple[PortKind, str | None, str | None, str | None]] = []

    async def resolve_with_connection_id(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
        *,
        expected_provider: str | None = None,
        options_overlay: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        self.calls.append((kind, connection_id, project_id, expected_provider))
        if expected_provider is not None and expected_provider != self.provider:
            raise ValueError(
                f"connection provider {self.provider!r} does not match {expected_provider!r}"
            )
        return self.adapter, connection_id or "default-engine"


# ── App wiring ───────────────────────────────────────────────────────────────


def make_client(
    repo: FakeEngineRunsRepository,
    identity: ConsumerIdentity,
    service: EngineAbortService | None = None,
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_engine_runs_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity
    if service is not None:
        app.dependency_overrides[get_engine_abort_service] = lambda: service
    return TestClient(app)


def abort_fixture(
    *,
    repo: FakeEngineRunsRepository | None = None,
    states: dict[str, JsonDict] | None = None,
    runs: FakeRuns | None = None,
    teardown_error: Exception | None = None,
    abort_error: Exception | None = None,
    status_phase: EngineRunPhase = EngineRunPhase.ABORTED,
    status_results: list[EngineRunPhase | Exception] | None = None,
) -> tuple[FakeEngineRunsRepository, FakeRuns, FakeEngineAdapter, FakeResolver, TestClient]:
    repo = repo if repo is not None else FakeEngineRunsRepository()
    runs = runs if runs is not None else FakeRuns()
    adapter = FakeEngineAdapter(
        teardown_error=teardown_error,
        abort_error=abort_error,
        status_phase=status_phase,
        status_results=status_results,
    )
    resolver = FakeResolver(adapter)
    client = FakeClient(FakeThreads(states), runs)
    service = EngineAbortService(client, repo, resolver=resolver)  # type: ignore[arg-type]
    return repo, runs, adapter, resolver, make_client(repo, OPERATOR, service)


# ── History reads ────────────────────────────────────────────────────────────


def test_list_engine_runs_envelope_newest_first() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-old", started_at=T0 - timedelta(hours=2))
    repo.seed(thread_id="t-new", started_at=T0)
    repo.seed(thread_id="t-mid", started_at=T0 - timedelta(hours=1))
    with make_client(repo, VIEWER) as client:
        response = client.get("/v1/engines/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    assert [item["thread_id"] for item in body["items"]] == ["t-new", "t-mid", "t-old"]


def test_list_engine_runs_filters_and_pagination() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", engine="sim", status="running", started_at=T0)
    repo.seed(thread_id="t-2", engine="sim", status="completed", started_at=T0 - timedelta(hours=1))
    repo.seed(
        thread_id="t-3", engine="jmeter", status="running", started_at=T0 - timedelta(hours=2)
    )
    with make_client(repo, VIEWER) as client:
        by_engine = client.get("/v1/engines/runs", params={"engine": "sim"}).json()
        by_status = client.get("/v1/engines/runs", params={"status": "running"}).json()
        paged = client.get("/v1/engines/runs", params={"limit": 1, "offset": 1}).json()
        bad_status = client.get("/v1/engines/runs", params={"status": "bogus"})
    assert [i["thread_id"] for i in by_engine["items"]] == ["t-1", "t-2"]
    assert by_engine["total"] == 2
    assert [i["thread_id"] for i in by_status["items"]] == ["t-1", "t-3"]
    assert [i["thread_id"] for i in paged["items"]] == ["t-2"]
    assert paged["total"] == 3  # total is the unpaged filtered count
    assert bad_status.status_code == 422  # status is the EngineRunPhase enum


def test_list_engine_runs_rejects_huge_offset_before_repository() -> None:
    repo = FakeEngineRunsRepository()

    with make_client(repo, VIEWER) as client:
        response = client.get("/v1/engines/runs", params={"offset": 10_001})

    assert response.status_code == 422
    assert repo.list_calls == 0


def test_get_engine_runs_per_thread_newest_attempt_first() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", attempt=1, status="failed", started_at=T0 - timedelta(hours=1))
    repo.seed(thread_id="t-1", attempt=2, status="running", started_at=T0)
    repo.seed(thread_id="t-2", attempt=1, status="completed")
    with make_client(repo, VIEWER) as client:
        rows = client.get("/v1/engines/runs/t-1").json()
        empty = client.get("/v1/engines/runs/missing").json()
    assert [(r["attempt"], r["status"]) for r in rows] == [(2, "running"), (1, "failed")]
    assert empty == []


def test_engine_run_history_never_exposes_internal_provider_handle() -> None:
    repo = FakeEngineRunsRepository()
    canary = "history-provider-token-canary"
    repo.seed(
        thread_id="t-secret-handle",
        handle={
            "engine": "sim",
            "connection_id": "dev-engine-sim",
            "external_run_id": "run-1",
            "idempotency_key": "idem-1",
            "extras": {
                "provider_token": canary,
                "metadata": f"Bearer {canary}",
                "run_id": "run-1",
            },
            "api_key": canary,
        },
    )

    with make_client(repo, VIEWER) as client:
        response = client.get("/v1/engines/runs/t-secret-handle")

    assert response.status_code == 200
    assert canary not in response.text
    assert "handle" not in response.json()[0]


def test_engine_run_history_quarantines_malformed_legacy_json() -> None:
    repo = FakeEngineRunsRepository()
    canary = "legacy-handle-canary"
    row = repo.seed(
        thread_id="t-malformed-history",
        handle={
            "engine": "sim",
            "idempotency_key": "idem-1",
            "extras": {"provider_data": canary * 3_000},
        },
    )
    row.summary = {"engine": "sim", "passed": True, "notes": canary * 3_000}

    with make_client(repo, VIEWER) as client:
        response = client.get("/v1/engines/runs/t-malformed-history")

    assert response.status_code == 200
    assert "handle" not in response.json()[0]
    assert response.json()[0]["summary"] is None
    assert canary not in response.text


def test_engine_run_history_redacts_summary_diagnostics() -> None:
    repo = FakeEngineRunsRepository()
    canary = "summary-token-canary"
    row = repo.seed(thread_id="t-summary")
    row.summary = {
        "engine": "sim",
        "passed": False,
        "kpis": {"error_rate": 1.0, f"Bearer {canary}": 7.0},
        "sla_breaches": [f"authorization: Bearer {canary}"],
        "notes": f"token={canary}",
        "api_key": canary,
    }

    with make_client(repo, VIEWER) as client:
        response = client.get("/v1/engines/runs/t-summary")

    assert response.status_code == 200
    assert canary not in response.text
    summary = response.json()[0]["summary"]
    assert summary["notes"] == "token=[REDACTED]"
    assert summary["sla_breaches"] == ["authorization: [REDACTED]"]
    assert summary["kpis"] == {"error_rate": 1.0}
    assert "api_key" not in summary


def test_get_engine_runs_per_thread_is_paginated_and_bounded() -> None:
    repo = FakeEngineRunsRepository()
    for attempt in range(1, 5):
        repo.seed(thread_id="t-1", attempt=attempt)

    with make_client(repo, VIEWER) as client:
        page = client.get("/v1/engines/runs/t-1", params={"limit": 2, "offset": 1})
        too_deep = client.get("/v1/engines/runs/t-1", params={"offset": 10_001})

    assert page.status_code == 200
    assert [row["attempt"] for row in page.json()] == [3, 2]
    assert too_deep.status_code == 422
    assert repo.thread_list_calls == 1


def test_engine_runs_are_scoped_by_project() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-p1", project_id="p1", external_run_id="run-p1")
    repo.seed(thread_id="t-p2", project_id="p2", external_run_id="run-p2")
    with make_client(repo, VIEWER) as client:
        listed = client.get("/v1/engines/runs").json()
        hidden = client.get("/v1/engines/runs/t-p2").json()
    assert [item["thread_id"] for item in listed["items"]] == ["t-p1"]
    assert hidden == []


def test_engine_runs_are_scoped_by_app_without_exposing_storage_affinity() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-app-a",
        project_id="p1",
        app_id="app-a",
        artifact_namespace="engine-runs/a",
        artifact_connection_id="artifacts-a",
    )
    repo.seed(thread_id="t-app-b", project_id="p1", app_id="app-b")
    repo.seed(thread_id="t-project-level", project_id="p1", app_id=None, ownership_known=True)
    repo.seed(thread_id="t-legacy", project_id="p1", app_id=None, ownership_known=False)

    with make_client(repo, APP_VIEWER) as client:
        listed = client.get("/v1/engines/runs").json()
        hidden = client.get("/v1/engines/runs/t-app-b").json()

    assert {item["thread_id"] for item in listed["items"]} == {"t-app-a", "t-project-level"}
    app_row = next(item for item in listed["items"] if item["thread_id"] == "t-app-a")
    assert app_row["app_id"] == "app-a"
    assert "artifact_namespace" not in app_row
    assert "artifact_connection_id" not in app_row
    assert "external_run_id" not in app_row
    assert hidden == []


# ── Abort flow ───────────────────────────────────────────────────────────────

STATE_HANDLE = {
    "engine": "sim",
    "connection_id": "dev-engine-sim",
    "external_run_id": "sim-abc123",
    "idempotency_key": "idem-1",
    "extras": {"started_at": "1.0"},
}


def current_execution_state(
    *, handle: JsonDict = STATE_HANDLE, status: str = "running", attempt: int = 1
) -> JsonDict:
    return {
        "values": {
            "engine_handle": handle,
            "phase_results": {
                "execution": {
                    "status": status,
                    "attempt": attempt,
                    "engine_handle": handle,
                }
            },
        }
    }


def test_abort_uses_state_handle_and_preserves_terminal_monitor() -> None:
    runs = FakeRuns(
        runs=[{"run_id": "r-run", "status": "running"}, {"run_id": "r-pend", "status": "pending"}]
    )
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    repo, fake_runs, adapter, resolver, client = abort_fixture(
        repo=repo, states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}}, runs=runs
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={"reason": "smoke went up"})
    assert response.status_code == 202
    assert response.json() == {
        "thread_id": "t-1",
        "engine": "sim",
        "external_run_id": "sim-abc123",
        "cancelled_runs": [],
        "phase": "aborted",
        "confirmed": True,
    }
    # The adapter saw the state handle and operator reason, but the graph monitor
    # retains ownership of collection, teardown, and durable finalization.
    (handle, reason), *_ = adapter.aborts
    assert handle == EngineHandle.model_validate(STATE_HANDLE)
    assert reason == "smoke went up"
    assert adapter.teardowns == []
    # adapter resolved through the handle's connection id
    # This old-style state has no run_config; projection ownership is merged
    # without replacing the more current state handle.
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim", "p1", "sim")]
    assert fake_runs.cancelled == []
    assert repo.aborted_threads == []
    assert repo.rows[0].status == "running"
    assert repo.read_transactions_released == 1


def test_abort_rejects_nul_bearing_provider_reason() -> None:
    _repo, _runs, adapter, _resolver, client = abort_fixture(
        states={"t-1": current_execution_state()}
    )

    with client:
        response = client.post(
            "/v1/engines/runs/t-1/abort",
            json={"reason": "stop\x00forged-log-suffix"},
        )

    assert response.status_code == 422
    assert adapter.aborts == []


def test_abort_preserves_captured_monitor_and_does_not_touch_replacement_attempt() -> None:
    repo = FakeEngineRunsRepository()
    first = repo.seed(thread_id="t-race", attempt=1, status="running", handle=STATE_HANDLE)
    runs = FakeRuns(runs=[{"run_id": "old-run", "status": "running"}])

    class RacingAdapter(FakeEngineAdapter):
        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            # The repository read transaction must be gone before provider I/O.
            assert repo.read_transactions_released == 1
            await super().abort(handle, reason=reason)
            # Attempt two starts while the external abort for attempt one settles.
            runs.runs.append({"run_id": "new-run", "status": "pending"})
            repo.seed(thread_id="t-race", attempt=2, status="running", handle=STATE_HANDLE)

    adapter = RacingAdapter()
    service = EngineAbortService(
        FakeClient(FakeThreads({"t-race": current_execution_state(attempt=1)}), runs),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-race"))

    assert result.cancelled_runs == []
    assert runs.cancelled == []
    assert first.status == "running"
    assert repo.rows[1].status == "running"


def test_abort_does_not_capture_replacement_run_during_target_discovery() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-transition", attempt=1, status="running", handle=STATE_HANDLE)

    class TransitionRuns(FakeRuns):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot = 0

        async def list(
            self,
            thread_id: str,
            *,
            status: str | None = None,
            offset: int = 0,
            **_kwargs: Any,
        ) -> list[JsonDict]:
            assert thread_id == "t-transition"
            current = (
                {
                    "run_id": "attempt-1-run",
                    "status": "running",
                    "created_at": (T0 - timedelta(seconds=1)).isoformat(),
                }
                if self.snapshot == 0
                else {
                    "run_id": "attempt-2-run",
                    "status": "running",
                    "created_at": (T0 + timedelta(seconds=1)).isoformat(),
                }
            )
            result = [current] if status == "running" and offset == 0 else []
            if status == "pending" and offset == 0:
                self.snapshot += 1
            return result

    runs = TransitionRuns()
    adapter = FakeEngineAdapter()
    service = EngineAbortService(
        FakeClient(
            FakeThreads({"t-transition": current_execution_state(attempt=1)}),
            runs,
        ),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    with pytest.raises(EngineGraphFinalizationPendingError):
        asyncio.run(service.abort("t-transition"))

    assert runs.cancelled == []
    assert adapter.teardowns == []
    assert repo.rows[0].status == "running"


def test_abort_paginates_more_than_sdk_default_active_runs() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-many", attempt=1, status="running", handle=STATE_HANDLE)
    records = [
        {
            "run_id": f"run-{index:02d}",
            "status": "running",
            "created_at": (T0 - timedelta(seconds=1)).isoformat(),
        }
        for index in range(25)
    ]

    class PaginatedRuns(FakeRuns):
        async def list(
            self,
            thread_id: str,
            *,
            status: str | None = None,
            limit: int = 10,
            offset: int = 0,
            **_kwargs: Any,
        ) -> list[JsonDict]:
            assert thread_id == "t-many"
            matching = [run for run in self.runs if status is None or run["status"] == status]
            # Model a server that caps pages at its default size even when the
            # client requests more.
            return matching[offset : offset + min(limit, 10)]

    runs = PaginatedRuns(records)
    service = EngineAbortService(
        FakeClient(FakeThreads({"t-many": current_execution_state(attempt=1)}), runs),
        repo,
        resolver=FakeResolver(FakeEngineAdapter()),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-many"))

    assert result.cancelled_runs == []
    assert runs.cancelled == []
    assert repo.rows[0].status == "running"


@pytest.mark.parametrize(
    "provider_phase",
    [EngineRunPhase.COMPLETED, EngineRunPhase.FAILED, EngineRunPhase.ABORTED],
)
def test_abort_preserves_results_monitor_for_every_terminal_provider_phase(
    provider_phase: EngineRunPhase,
) -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-natural-finish", attempt=1, status="running", handle=STATE_HANDLE)

    runs = FakeRuns([{"run_id": "results-monitor", "status": "running"}])
    adapter = FakeEngineAdapter(status_phase=provider_phase)
    service = EngineAbortService(
        FakeClient(
            FakeThreads({"t-natural-finish": current_execution_state(attempt=1)}),
            runs,
        ),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-natural-finish"))

    assert result.phase == provider_phase.value
    assert result.confirmed is True
    assert result.cancelled_runs == []
    assert runs.cancelled == []
    assert runs.runs[0]["run_id"] == "results-monitor"
    assert adapter.teardowns == []
    assert repo.rows[0].status == "running"


def test_abort_fails_closed_when_active_run_snapshot_exceeds_cap() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-over-cap", attempt=1, status="running", handle=STATE_HANDLE)
    records = [
        {
            "run_id": f"run-{index:04d}",
            "status": "running",
            "created_at": (T0 - timedelta(seconds=1)).isoformat(),
        }
        for index in range(engine_abort_service.MAX_ACTIVE_RUN_SNAPSHOT + 1)
    ]

    class PaginatedRuns(FakeRuns):
        def __init__(self, runs: list[JsonDict]) -> None:
            super().__init__(runs=runs)
            self.selects: list[list[str] | None] = []

        async def list(
            self,
            thread_id: str,
            *,
            status: str | None = None,
            limit: int = 100,
            offset: int = 0,
            select: list[str] | None = None,
            **_kwargs: Any,
        ) -> list[JsonDict]:
            assert thread_id == "t-over-cap"
            self.selects.append(select)
            matching = [run for run in self.runs if status is None or run["status"] == status]
            return matching[offset : offset + limit]

    runs = PaginatedRuns(records)
    adapter = FakeEngineAdapter()
    service = EngineAbortService(
        FakeClient(FakeThreads({"t-over-cap": current_execution_state(attempt=1)}), runs),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    with pytest.raises(TooManyActiveRunsError):
        asyncio.run(service.abort("t-over-cap"))

    assert adapter.aborts == []
    assert runs.cancelled == []
    assert runs.selects and all(
        select == ["run_id", "status", "created_at"] for select in runs.selects
    )


def test_abort_falls_back_to_projection_handle() -> None:
    repo = FakeEngineRunsRepository()
    projection_handle = {
        "engine": "sim",
        "connection_id": "conn-from-row",
        "external_run_id": "sim-row",
        "idempotency_key": "idem-row",
        "extras": {},
    }
    repo.seed(thread_id="t-gone", attempt=1, handle={"engine": "sim"}, status="failed")
    repo.seed(thread_id="t-gone", attempt=2, handle=projection_handle, status="running")
    # thread unknown to the loopback API entirely: state 404s AND runs.list 404s
    runs = FakeRuns(known_threads=set())
    repo, fake_runs, adapter, resolver, client = abort_fixture(repo=repo, states={}, runs=runs)
    with client:
        response = client.post("/v1/engines/runs/t-gone/abort", json={})
    assert response.status_code == 202
    body = response.json()
    assert body["external_run_id"] == "sim-row"  # latest attempt's handle won
    assert body["cancelled_runs"] == []  # missing thread tolerated
    (handle, reason), *_ = adapter.aborts
    assert handle.connection_id == "conn-from-row"
    assert reason == "operator abort"  # default reason
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "conn-from-row", "p1", "sim")]
    assert repo.rows[1].status == "aborted"


def test_abort_projection_columns_override_stale_handle_affinity() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-affinity",
        engine="sim",
        external_run_id="sim-row",
        connection_id="conn-row",
        handle={
            "engine": "sim",
            "connection_id": None,
            "external_run_id": "sim-json",
            "idempotency_key": "idem-json",
            "extras": {},
        },
        status="running",
    )
    runs = FakeRuns(known_threads=set())
    repo, _, adapter, resolver, client = abort_fixture(repo=repo, states={}, runs=runs)

    with client:
        response = client.post("/v1/engines/runs/t-affinity/abort", json={})

    assert response.status_code == 202
    (handle, _reason), *_ = adapter.aborts
    assert handle.external_run_id == "sim-row"
    assert handle.connection_id == "conn-row"
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "conn-row", "p1", "sim")]


def test_abort_rejects_mismatched_legacy_state_and_projection_identity() -> None:
    projection_handle = {
        **STATE_HANDLE,
        "external_run_id": "sim-different",
        "idempotency_key": "different-attempt",
    }
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="identity-mismatch",
        status="running",
        external_run_id=projection_handle["external_run_id"],
        connection_id=projection_handle["connection_id"],
        handle=projection_handle,
    )
    adapter = FakeEngineAdapter()
    resolver = FakeResolver(adapter)
    service = EngineAbortService(
        FakeClient(
            FakeThreads({"identity-mismatch": {"values": {"engine_handle": STATE_HANDLE}}}),
            FakeRuns(runs=[{"run_id": "monitor", "status": "running"}]),
        ),
        repo,
        resolver=resolver,  # type: ignore[arg-type]
    )

    with pytest.raises(EngineConnectionAffinityMissingError):
        asyncio.run(service.abort("identity-mismatch"))

    assert resolver.calls == []
    assert adapter.aborts == []


def test_locked_abort_rejects_connection_generation_drift_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_abort_service,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    expected_version = T0
    changed_version = T0 + timedelta(seconds=1)
    handle = {
        **STATE_HANDLE,
        "connection_id": "engine-connection",
        "external_run_id": "sim-generation-fence",
        "idempotency_key": "generation-fence",
    }
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="generation-fence",
        status="running",
        external_run_id=handle["external_run_id"],
        connection_id=handle["connection_id"],
        execution_connection_version=expected_version,
        handle=handle,
    )
    calls: list[str] = []

    class ClosableAdapter(FakeEngineAdapter):
        async def aclose(self) -> None:
            calls.append("close")

    adapter = ClosableAdapter()

    class MetadataResolver:
        async def resolve_with_metadata(self, *_args: Any, **_kwargs: Any) -> ResolvedAdapter:
            return ResolvedAdapter(
                adapter=adapter,
                connection_id="engine-connection",
                connection_version=changed_version,
                persisted=True,
            )

    state = current_execution_state(handle=handle)
    state["values"]["phase_results"]["execution"].update(
        {
            "engine_connection_affinity_staged": True,
            "engine_connection_id": "engine-connection",
            "engine_connection_persisted": True,
            "engine_connection_version": expected_version.isoformat(),
        }
    )
    service = EngineAbortService(
        FakeClient(
            FakeThreads({"generation-fence": state}),
            FakeRuns(runs=[{"run_id": "monitor", "status": "running"}]),
        ),
        repo,
        resolver=MetadataResolver(),  # type: ignore[arg-type]
    )

    with pytest.raises(EngineProviderAbortError, match="could not be resolved"):
        asyncio.run(service.abort("generation-fence"))

    assert adapter.aborts == []
    assert calls == ["close"]


def test_locked_abort_rejects_state_only_legacy_run_without_connection_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_abort_service,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    legacy_handle = {
        "engine": "loadrunner",
        "connection_id": None,
        "external_run_id": "42",
        "idempotency_key": "legacy-state-run",
        "extras": {},
    }
    repo, _, adapter, resolver, client = abort_fixture(
        states={"legacy-state": current_execution_state(handle=legacy_handle)},
    )

    with client:
        response = client.post("/v1/engines/runs/legacy-state/abort", json={})

    assert response.status_code == 409
    assert response.json()["title"] == ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL
    assert resolver.calls == []
    assert adapter.aborts == []
    assert repo.read_transactions_released == 1


def test_locked_abort_rejects_projection_only_legacy_run_without_connection_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_abort_service,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    repo = FakeEngineRunsRepository()
    row = repo.seed(
        thread_id="legacy-projection",
        engine="loadrunner",
        external_run_id="42",
        connection_id=None,
        handle={
            "engine": "loadrunner",
            "connection_id": None,
            "external_run_id": "42",
            "idempotency_key": "legacy-projection-run",
            "extras": {},
        },
        status="running",
    )
    repo, _, adapter, resolver, client = abort_fixture(
        repo=repo,
        states={},
        runs=FakeRuns(known_threads=set()),
    )

    with client:
        response = client.post("/v1/engines/runs/legacy-projection/abort", json={})

    assert response.status_code == 409
    assert response.json()["title"] == ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL
    assert resolver.calls == []
    assert adapter.aborts == []
    assert row.status == "running"
    assert repo.read_transactions_released == 1


def test_unlocked_abort_keeps_static_default_compatibility_for_legacy_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine_abort_service,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=False),
    )
    legacy_handle = {
        "engine": "sim",
        "connection_id": None,
        "external_run_id": "sim-legacy",
        "idempotency_key": "legacy-dev-run",
        "extras": {},
    }
    _, _, adapter, resolver, client = abort_fixture(
        states={"legacy-dev": current_execution_state(handle=legacy_handle)},
        runs=FakeRuns(runs=[{"run_id": "monitor", "status": "running"}]),
    )

    with client:
        response = client.post("/v1/engines/runs/legacy-dev/abort", json={})

    assert response.status_code == 202
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, None, None, "sim")]
    assert len(adapter.aborts) == 1


@pytest.mark.parametrize(
    "provider_phase",
    [EngineRunPhase.COMPLETED, EngineRunPhase.FAILED, EngineRunPhase.ABORTED],
)
def test_projection_only_abort_owns_terminalization(
    provider_phase: EngineRunPhase,
) -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-projection-only", handle=STATE_HANDLE, status="running")
    adapter = FakeEngineAdapter(status_phase=provider_phase)
    service = EngineAbortService(
        FakeClient(FakeThreads({}), FakeRuns(known_threads=set())),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-projection-only"))

    assert result.phase == provider_phase.value
    assert row.status == provider_phase.value
    assert adapter.teardowns == [EngineHandle.model_validate(STATE_HANDLE)]


def test_projection_only_abort_rechecks_state_before_destructive_teardown() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(
        thread_id="state-appears",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )

    class AppearingThreads:
        def __init__(self) -> None:
            self.calls = 0

        async def get_state(self, thread_id: str) -> JsonDict:
            self.calls += 1
            if self.calls == 1:
                raise _not_found()
            return current_execution_state()

    adapter = FakeEngineAdapter()
    service = EngineAbortService(
        FakeClient(AppearingThreads(), FakeRuns()),  # type: ignore[arg-type]
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    with pytest.raises(EngineGraphFinalizationPendingError):
        asyncio.run(service.abort("state-appears"))

    assert len(adapter.aborts) == 1
    assert adapter.teardowns == []
    assert row.status == "running"


@pytest.mark.parametrize("initial_state_found", [False, True])
def test_abort_does_not_assign_old_projection_to_a_newer_state_attempt(
    initial_state_found: bool,
) -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(
        thread_id="attempt-advanced",
        attempt=1,
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    newer_handle = {
        **STATE_HANDLE,
        "external_run_id": "sim-new-attempt",
        "idempotency_key": "idem-new-attempt",
    }

    class AdvancingThreads:
        def __init__(self) -> None:
            self.calls = 0

        async def get_state(self, thread_id: str) -> JsonDict:
            self.calls += 1
            if self.calls == 1:
                if initial_state_found:
                    return current_execution_state(attempt=1)
                raise _not_found()
            return current_execution_state(handle=newer_handle, attempt=2)

    adapter = FakeEngineAdapter()
    service = EngineAbortService(
        FakeClient(AdvancingThreads(), FakeRuns()),  # type: ignore[arg-type]
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("attempt-advanced"))

    assert result.confirmed is True
    assert row.status == "aborted"
    assert adapter.teardowns == [EngineHandle.model_validate(STATE_HANDLE)]


def test_abort_provider_cannot_mutate_trusted_handle_affinity() -> None:
    class MutatingAdapter(FakeEngineAdapter):
        def __init__(self) -> None:
            super().__init__(status_phase=EngineRunPhase.ABORTED)
            self.status_handles: list[EngineHandle] = []

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            await super().abort(handle, reason=reason)
            handle.engine = "attacker"
            handle.connection_id = "attacker-store"
            handle.external_run_id = "other-customer-run"
            handle.idempotency_key = "attacker-key"
            # A provider may legitimately reconcile opaque state in extras.
            handle.extras = {"run_id": "reconciled-42"}

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            self.status_handles.append(handle)
            assert handle.engine == "sim"
            assert handle.connection_id == "dev-engine-sim"
            assert handle.external_run_id == "sim-abc123"
            assert handle.idempotency_key == "idem-1"
            assert handle.extras == {"run_id": "reconciled-42"}
            handle.external_run_id = "status-redirect"
            return EngineRunStatus(phase=EngineRunPhase.ABORTED, progress_pct=100.0)

        async def teardown(self, handle: EngineHandle) -> None:
            assert handle.engine == "sim"
            assert handle.connection_id == "dev-engine-sim"
            assert handle.external_run_id == "sim-abc123"
            assert handle.idempotency_key == "idem-1"
            assert handle.extras == {"run_id": "reconciled-42"}
            await super().teardown(handle)

    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-mutating-provider", handle=STATE_HANDLE, status="running")
    adapter = MutatingAdapter()
    service = EngineAbortService(
        FakeClient(FakeThreads({}), FakeRuns(known_threads=set())),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-mutating-provider"))

    assert result.engine == "sim"
    assert result.external_run_id == "sim-abc123"
    assert result.phase == "aborted"
    assert row.status == "aborted"
    assert adapter.aborts[0][0] is not adapter.status_handles[0]
    assert adapter.status_handles[0] is not adapter.teardowns[0]


def test_projection_only_teardown_failure_retains_lease_for_abort_retry() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-projection-teardown", handle=STATE_HANDLE, status="running")
    adapter = FakeEngineAdapter(
        status_phase=EngineRunPhase.ABORTED,
        teardown_error=OSError("provider cleanup unavailable"),
    )
    service = EngineAbortService(
        FakeClient(FakeThreads({}), FakeRuns(known_threads=set())),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    with make_client(repo, OPERATOR, service) as client:
        response = client.post("/v1/engines/runs/t-projection-teardown/abort", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert row.status == "running"
    assert len(adapter.aborts) == 1
    assert adapter.teardowns == []


def test_projection_fallback_still_preserves_an_active_monitor() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-state-race", handle=STATE_HANDLE, status="running")
    runs = FakeRuns([{"run_id": "results-monitor", "status": "running"}])
    runs.runs[0].pop("created_at")  # legacy SDK payload: fail closed as a possible monitor
    adapter = FakeEngineAdapter(status_phase=EngineRunPhase.COMPLETED)
    service = EngineAbortService(
        FakeClient(FakeThreads({}), runs),
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.abort("t-state-race"))

    assert result.confirmed is True
    assert result.cancelled_runs == []
    assert runs.cancelled == []
    assert row.status == "running"
    assert adapter.teardowns == []


def test_app_only_operator_cannot_fallback_abort_project_level_projection() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-project-wide",
        project_id="p1",
        app_id=None,
        ownership_known=True,
        handle=STATE_HANDLE,
        status="running",
    )
    adapter = FakeEngineAdapter()
    resolver = FakeResolver(adapter)
    service = EngineAbortService(
        FakeClient(FakeThreads({}), FakeRuns(known_threads=set())),
        repo,
        resolver=resolver,  # type: ignore[arg-type]
        allowed_scopes=APP_OPERATOR.scopes,
    )

    with make_client(repo, APP_OPERATOR, service) as client:
        response = client.post("/v1/engines/runs/t-project-wide/abort", json={})

    assert response.status_code == 404
    assert adapter.aborts == []
    assert resolver.calls == []
    assert repo.aborted_threads == []
    assert repo.rows[0].status == "running"


def test_abort_state_without_handle_falls_back_then_404() -> None:
    # state exists but carries no engine_handle; projection empty -> 404 problem
    _, _, adapter, _, client = abort_fixture(states={"t-1": {"values": {}}})
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert adapter.aborts == []


def test_abort_running_checkpoint_without_monitor_requires_recovery() -> None:
    repo, fake_runs, adapter, _, client = abort_fixture(states={"t-1": current_execution_state()})
    with client:
        response = client.post("/v1/engines/runs/t-1/abort")  # no body at all
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == (
        "external engine stopped but graph finalization is pending recovery; resume the pipeline"
    )
    assert adapter.aborts[0][1] == "operator abort"
    assert fake_runs.cancelled == []
    assert adapter.teardowns == []


def test_abort_keeps_graph_monitor_alive_while_provider_is_stopping() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    runs = FakeRuns(runs=[{"run_id": "r-run", "status": "running"}])
    repo, fake_runs, _adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        runs=runs,
        status_phase=EngineRunPhase.STOPPING,
    )

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 202
    assert response.json()["phase"] == "stopping"
    assert response.json()["confirmed"] is False
    assert response.json()["cancelled_runs"] == []
    assert fake_runs.cancelled == []
    assert repo.rows[0].status == "running"


def test_abort_stale_checkpoint_without_active_run_confirms_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_abort_service, "STOPPING_CONFIRM_INTERVAL_S", 0)
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    repo, fake_runs, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        status_results=[
            EngineRunPhase.STOPPING,
            EngineRunPhase.STOPPING,
            EngineRunPhase.ABORTED,
        ],
    )

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 503
    assert response.json()["title"] == (
        "external engine stopped but graph finalization is pending recovery; resume the pipeline"
    )
    assert fake_runs.cancelled == []
    assert repo.rows[0].status == "running"
    assert adapter.teardowns == []


def test_abort_confirmation_treats_provider_disappearance_as_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_abort_service, "STOPPING_CONFIRM_INTERVAL_S", 0)
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    repo, _, _, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        status_results=[
            EngineRunPhase.STOPPING,
            EngineProviderRunNotFoundError("provider run is gone"),
        ],
    )

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 503
    assert response.json()["title"] == (
        "external engine stopped but graph finalization is pending recovery; resume the pipeline"
    )
    assert repo.rows[0].status == "running"


def test_abort_does_not_treat_provider_parser_keyerror_as_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_abort_service, "STOPPING_CONFIRM_INTERVAL_S", 0)
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    repo, fake_runs, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        status_results=[
            EngineRunPhase.STOPPING,
            KeyError("missing provider response field"),
        ],
    )

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 502
    assert response.json()["title"] == "engine provider abort failed"
    assert fake_runs.cancelled == []
    assert adapter.teardowns == []
    assert repo.rows[0].status == "running"


def test_abort_without_monitor_returns_retryable_503_while_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_abort_service, "STOPPING_CONFIRM_ATTEMPTS", 2)
    monkeypatch.setattr(engine_abort_service, "STOPPING_CONFIRM_INTERVAL_S", 0)
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
    )
    repo, fake_runs, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        status_phase=EngineRunPhase.STOPPING,
    )

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == (
        "external engine is still stopping; retry abort to confirm termination"
    )
    assert fake_runs.cancelled == []
    assert repo.rows[0].status == "running"
    assert adapter.teardowns == []


def test_abort_during_required_provisioning_reservation_keeps_graph_alive() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-provisioning",
        attempt=1,
        status="provisioning",
        handle={"engine": "sim", "connection_id": "dev-engine-sim"},
    )
    runs = FakeRuns(runs=[{"run_id": "provider-create", "status": "running"}])
    state = {
        "values": {
            "phase_results": {
                "execution": {"status": "running", "attempt": 1, "engine_handle": None}
            }
        }
    }
    repo, fake_runs, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-provisioning": state},
        runs=runs,
    )

    with client:
        response = client.post("/v1/engines/runs/t-provisioning/abort", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert fake_runs.cancelled == []
    assert adapter.aborts == []
    assert repo.rows[0].status == "provisioning"


def test_abort_surfaces_teardown_failure_and_retains_projection() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-1", status="running", handle=STATE_HANDLE)
    repo, _, adapter, _, client = abort_fixture(
        repo=repo,
        states={},
        runs=FakeRuns(known_threads=set()),
        teardown_error=RuntimeError("boom"),
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={"reason": "kill it"})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert adapter.aborts[0][1] == "kill it"
    assert adapter.teardowns == []
    assert row.status == "running"


def test_abort_surfaces_projection_failure_after_successful_teardown() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-projection-write", status="running", handle=STATE_HANDLE)
    repo.mark_aborted_error = RuntimeError("db down")
    repo, _, adapter, _, client = abort_fixture(
        repo=repo,
        states={},
        runs=FakeRuns(known_threads=set()),
    )

    with client:
        response = client.post("/v1/engines/runs/t-projection-write/abort", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert adapter.teardowns == [EngineHandle.model_validate(STATE_HANDLE)]
    assert row.status == "running"


def test_abort_surfaces_projection_cas_miss_after_successful_teardown() -> None:
    repo = FakeEngineRunsRepository()
    row = repo.seed(thread_id="t-projection-cas-miss", status="running", handle=STATE_HANDLE)
    repo.mark_terminal_result = 0
    repo, _, adapter, _, client = abort_fixture(
        repo=repo,
        states={},
        runs=FakeRuns(known_threads=set()),
    )

    with client:
        response = client.post("/v1/engines/runs/t-projection-cas-miss/abort", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert adapter.teardowns == [EngineHandle.model_validate(STATE_HANDLE)]
    assert row.status == "running"


def test_abort_failure_does_not_cancel_graph_or_mark_projection() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", status="running", handle=STATE_HANDLE)
    runs = FakeRuns(runs=[{"run_id": "r-run", "status": "running"}])
    repo, fake_runs, adapter, _, client = abort_fixture(
        repo=repo,
        states={
            "t-1": {
                "values": {
                    "engine_handle": STATE_HANDLE,
                    "run_config": {"project_id": "p1", "app_id": "app-a"},
                }
            }
        },
        runs=runs,
        abort_error=RuntimeError("provider unavailable"),
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 502
    assert "provider unavailable" not in response.text
    assert adapter.aborts
    assert fake_runs.cancelled == []
    assert repo.aborted_threads == []
    assert repo.rows[0].status == "running"


def test_abort_uses_project_from_nested_state_handle() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="running",
        external_run_id=STATE_HANDLE["external_run_id"],
        connection_id=STATE_HANDLE["connection_id"],
        handle=STATE_HANDLE,
        project_id="project-a",
        app_id="app-a",
    )
    nested = {
        "values": {"run_config": {"project_id": "project-a", "app_id": "app-a"}},
        "tasks": [{"state": {"values": {"engine_handle": STATE_HANDLE}}}],
    }
    repo, _, _, resolver, client = abort_fixture(
        repo=repo,
        states={"t-1": nested},
        runs=FakeRuns(runs=[{"run_id": "monitor", "status": "running"}]),
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 202
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim", "project-a", "sim")]


def test_abort_rejects_connection_provider_mismatch_before_external_kill() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", status="running", handle=STATE_HANDLE)
    runs = FakeRuns(runs=[{"run_id": "r-run", "status": "running"}])
    adapter = FakeEngineAdapter()
    resolver = FakeResolver(adapter, provider="loadrunner")
    service = EngineAbortService(
        FakeClient(
            FakeThreads(
                {
                    "t-1": {
                        "values": {
                            "engine_handle": STATE_HANDLE,
                            "run_config": {"project_id": "p1"},
                        }
                    }
                }
            ),
            runs,
        ),
        repo,
        resolver=resolver,  # type: ignore[arg-type]
    )

    with pytest.raises(EngineProviderAbortError, match="could not be resolved"):
        asyncio.run(service.abort("t-1"))

    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim", "p1", "sim")]
    assert adapter.aborts == []
    assert runs.cancelled == []
    assert repo.aborted_threads == []


def test_abort_rejects_terminal_state_handle() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        status="completed",
        external_run_id="external-1",
        handle=STATE_HANDLE,
    )
    repo, _, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
    )

    with capture_logs(), client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 404
    assert adapter.aborts == []


def test_abort_rejects_previous_attempt_handle_during_rerun() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(
        thread_id="t-1",
        attempt=1,
        status="completed",
        external_run_id="sim-abc123",
        handle=STATE_HANDLE,
    )
    rerun_state = current_execution_state(status="pending", attempt=2)
    repo, _, adapter, _, client = abort_fixture(repo=repo, states={"t-1": rerun_state})

    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 404
    assert adapter.aborts == []


def test_abort_requires_operator_role() -> None:
    repo = FakeEngineRunsRepository()
    adapter = FakeEngineAdapter()
    service = EngineAbortService(
        FakeClient(FakeThreads({}), FakeRuns()),  # type: ignore[arg-type]
        repo,
        resolver=FakeResolver(adapter),  # type: ignore[arg-type]
    )
    with make_client(repo, VIEWER, service) as client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})
    assert response.status_code == 403
    assert adapter.aborts == []


def test_engine_handle_search_is_depth_bounded_and_cycle_safe() -> None:
    deeply_nested: JsonDict = {}
    cursor = deeply_nested
    for _ in range(engine_abort_service.MAX_STATE_HANDLE_SEARCH_DEPTH + 100):
        nested: JsonDict = {}
        cursor["state"] = nested
        cursor = nested
    cursor["engine_handle"] = STATE_HANDLE

    assert engine_abort_service._find_engine_handle(deeply_nested) is None

    cyclic: JsonDict = {}
    cyclic["state"] = cyclic
    assert engine_abort_service._find_engine_handle(cyclic) is None


# ── Facade surfacing (additive `engine` field on pipeline rows) ──────────────


def test_engine_info_from_values_maps_handle() -> None:
    values = {"engine_handle": STATE_HANDLE}
    assert engine_info_from_values(values) == {
        "engine": "sim",
        "external_run_id": "sim-abc123",
    }
    assert engine_info_from_values({}) is None
    assert engine_info_from_values(None) is None
    assert engine_info_from_values({"engine_handle": "junk"}) is None
    assert engine_info_from_values({"engine_handle": {"external_run_id": "x"}}) is None


def test_map_thread_summary_includes_engine_info() -> None:
    thread = {
        "thread_id": "t-1",
        "status": "busy",
        "metadata": {},
        "values": {"engine_handle": STATE_HANDLE},
        "interrupts": {},
    }
    summary = map_thread_summary(thread)
    assert summary["engine"] == {"engine": "sim", "external_run_id": "sim-abc123"}
    assert map_thread_summary({"thread_id": "t-2", "values": {}})["engine"] is None
