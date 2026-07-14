"""/engines history + abort against fake repo / loopback client / engine adapter."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError
from structlog.testing import capture_logs

from apex.adapters.registry import PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import EngineHandle
from apex.persistence.models import EngineRun
from apex.routers.engines import (
    get_engine_abort_service,
    get_engine_runs_repository,
    router,
)
from apex.services.engine_abort import EngineAbortService
from apex.services.pipeline_read import engine_info_from_values, map_thread_summary

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
        self.aborted_threads: list[str] = []
        self.mark_aborted_error: Exception | None = None

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
        artifact_namespace: str | None = None,
        artifact_connection_id: str | None = None,
        started_at: datetime | None = None,
    ) -> EngineRun:
        run = EngineRun(
            id=f"{thread_id}-{attempt}",
            thread_id=thread_id,
            project_id=project_id,
            app_id=app_id,
            ownership_known=ownership_known,
            attempt=attempt,
            engine=engine,
            external_run_id=external_run_id,
            artifact_namespace=artifact_namespace,
            artifact_connection_id=artifact_connection_id,
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
                or (run.app_id is None and run.ownership_known)
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
    ) -> list[EngineRun]:
        rows = [r for r in self.rows if r.thread_id == thread_id]
        if allowed_scopes is not None:
            rows = [r for r in rows if self._visible(r, allowed_scopes)]
        elif allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id in allowed_project_ids]
        rows.sort(key=lambda r: r.attempt, reverse=True)
        return rows

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
        )
        return rows[0] if rows else None

    async def get_latest_abortable_for_thread(
        self,
        thread_id: str,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> EngineRun | None:
        rows = [r for r in self.rows if r.thread_id == thread_id]
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
        allowed_scopes: Sequence[ScopeRef] | None = None,
        allowed_project_ids: tuple[str, ...] | None = None,
    ) -> int:
        if self.mark_aborted_error is not None:
            raise self.mark_aborted_error
        self.aborted_threads.append(thread_id)
        count = 0
        for run in self.rows:
            if (
                run.thread_id == thread_id
                and (allowed_scopes is None or self._mutable(run, allowed_scopes))
                and (allowed_project_ids is None or run.project_id in allowed_project_ids)
                and run.status not in ("completed", "failed", "aborted")
            ):
                run.status = "aborted"
                run.ended_at = datetime.now(UTC)
                count += 1
        return count


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
        self.runs = runs or []
        self.known_threads = known_threads
        self.cancelled: list[tuple[str, str]] = []

    async def list(self, thread_id: str, *, status: str | None = None, **_: Any) -> list[JsonDict]:
        if self.known_threads is not None and thread_id not in self.known_threads:
            raise _not_found()
        return [r for r in self.runs if status is None or r.get("status") == status]

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        self.cancelled.append((thread_id, run_id))


class FakeClient:
    def __init__(self, threads: FakeThreads, runs: FakeRuns) -> None:
        self.threads = threads
        self.runs = runs


class FakeEngineAdapter:
    def __init__(
        self,
        teardown_error: Exception | None = None,
        abort_error: Exception | None = None,
    ) -> None:
        self.aborts: list[tuple[EngineHandle, str]] = []
        self.teardowns: list[EngineHandle] = []
        self.teardown_error = teardown_error
        self.abort_error = abort_error

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        self.aborts.append((handle, reason))
        if self.abort_error is not None:
            raise self.abort_error

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
) -> tuple[FakeEngineRunsRepository, FakeRuns, FakeEngineAdapter, FakeResolver, TestClient]:
    repo = repo if repo is not None else FakeEngineRunsRepository()
    runs = runs if runs is not None else FakeRuns()
    adapter = FakeEngineAdapter(teardown_error=teardown_error, abort_error=abort_error)
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


def test_engine_runs_are_scoped_by_project() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-p1", project_id="p1", external_run_id="run-p1")
    repo.seed(thread_id="t-p2", project_id="p2", external_run_id="run-p2")
    with make_client(repo, VIEWER) as client:
        listed = client.get("/v1/engines/runs").json()
        hidden = client.get("/v1/engines/runs/t-p2").json()
    assert [item["thread_id"] for item in listed["items"]] == ["t-p1"]
    assert hidden == []


def test_engine_runs_are_scoped_by_app_and_expose_artifact_ownership() -> None:
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
    assert app_row["artifact_namespace"] == "engine-runs/a"
    assert app_row["artifact_connection_id"] == "artifacts-a"
    assert hidden == []


# ── Abort flow ───────────────────────────────────────────────────────────────

STATE_HANDLE = {
    "engine": "sim",
    "connection_id": "dev-engine-sim",
    "external_run_id": "sim-abc123",
    "idempotency_key": "idem-1",
    "extras": {"started_at": "1.0"},
}


def test_abort_uses_handle_from_state_and_cancels_runs() -> None:
    runs = FakeRuns(
        runs=[{"run_id": "r-run", "status": "running"}, {"run_id": "r-pend", "status": "pending"}]
    )
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", status="running")
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
        "cancelled_runs": ["r-run", "r-pend"],
    }
    # adapter saw the state handle and the operator's reason; teardown followed
    (handle, reason), *_ = adapter.aborts
    assert handle == EngineHandle.model_validate(STATE_HANDLE)
    assert reason == "smoke went up"
    assert adapter.teardowns == [handle]
    # adapter resolved through the handle's connection id
    # This old-style state has no run_config; projection ownership is merged
    # without replacing the more current state handle.
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim", "p1", "sim")]
    # both active graph runs were cancelled and the projection row flipped
    assert fake_runs.cancelled == [("t-1", "r-run"), ("t-1", "r-pend")]
    assert repo.aborted_threads == ["t-1"]
    assert repo.rows[0].status == "aborted"


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


def test_abort_tolerates_no_active_runs_and_no_body() -> None:
    repo, fake_runs, adapter, _, client = abort_fixture(
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}}
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort")  # no body at all
    assert response.status_code == 202
    assert response.json()["cancelled_runs"] == []
    assert adapter.aborts[0][1] == "operator abort"
    assert fake_runs.cancelled == []


def test_abort_survives_teardown_and_projection_failures() -> None:
    repo = FakeEngineRunsRepository()
    repo.mark_aborted_error = RuntimeError("db down")
    repo, _, adapter, _, client = abort_fixture(
        repo=repo,
        states={"t-1": {"values": {"engine_handle": STATE_HANDLE}}},
        teardown_error=RuntimeError("boom"),
    )
    with client:
        response = client.post("/v1/engines/runs/t-1/abort", json={"reason": "kill it"})
    assert response.status_code == 202  # teardown + projection are best-effort
    assert adapter.aborts[0][1] == "kill it"
    assert adapter.teardowns == []


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
    with pytest.raises(RuntimeError, match="provider unavailable"), client:
        client.post("/v1/engines/runs/t-1/abort", json={})

    assert adapter.aborts
    assert fake_runs.cancelled == []
    assert repo.aborted_threads == []
    assert repo.rows[0].status == "running"


def test_abort_uses_project_from_nested_state_handle() -> None:
    repo = FakeEngineRunsRepository()
    repo.seed(thread_id="t-1", status="running")
    nested = {
        "values": {"run_config": {"project_id": "project-a", "app_id": "app-a"}},
        "tasks": [{"state": {"values": {"engine_handle": STATE_HANDLE}}}],
    }
    repo, _, _, resolver, client = abort_fixture(repo=repo, states={"t-1": nested})
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

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(service.abort("t-1"))

    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim", "p1", "sim")]
    assert adapter.aborts == []
    assert runs.cancelled == []
    assert repo.aborted_threads == []


def test_abort_logs_empty_projection_update() -> None:
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

    with capture_logs() as logs, client:
        response = client.post("/v1/engines/runs/t-1/abort", json={})

    assert response.status_code == 202
    assert adapter.aborts
    assert any(event["event"] == "engine_abort.projection_update_empty" for event in logs)


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
