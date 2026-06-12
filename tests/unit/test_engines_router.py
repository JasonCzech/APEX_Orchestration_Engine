"""/engines history + abort against fake repo / loopback client / engine adapter."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError

from apex.adapters.registry import PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
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
    consumer_id="op-1", name="op", consumer_type=ConsumerType.DASHBOARD, role=Role.OPERATOR
)
VIEWER = ConsumerIdentity(
    consumer_id="view-1", name="viewer", consumer_type=ConsumerType.DASHBOARD, role=Role.VIEWER
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
        started_at: datetime | None = None,
    ) -> EngineRun:
        run = EngineRun(
            id=f"{thread_id}-{attempt}",
            thread_id=thread_id,
            attempt=attempt,
            engine=engine,
            external_run_id=external_run_id,
            handle=handle if handle is not None else {},
            status=status,
            started_at=started_at or T0,
            ended_at=None,
            summary=None,
        )
        self.rows.append(run)
        return run

    async def list_runs(
        self,
        *,
        engine: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EngineRun], int]:
        rows = [
            r
            for r in self.rows
            if (engine is None or r.engine == engine) and (status is None or r.status == status)
        ]
        rows.sort(key=lambda r: (r.started_at, r.id), reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def list_for_thread(self, thread_id: str) -> list[EngineRun]:
        rows = [r for r in self.rows if r.thread_id == thread_id]
        rows.sort(key=lambda r: r.attempt, reverse=True)
        return rows

    async def get_latest_for_thread(self, thread_id: str) -> EngineRun | None:
        rows = await self.list_for_thread(thread_id)
        return rows[0] if rows else None

    async def mark_aborted(self, thread_id: str) -> int:
        if self.mark_aborted_error is not None:
            raise self.mark_aborted_error
        self.aborted_threads.append(thread_id)
        count = 0
        for run in self.rows:
            if run.thread_id == thread_id and run.status not in ("completed", "failed", "aborted"):
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
    def __init__(self, teardown_error: Exception | None = None) -> None:
        self.aborts: list[tuple[EngineHandle, str]] = []
        self.teardowns: list[EngineHandle] = []
        self.teardown_error = teardown_error

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        self.aborts.append((handle, reason))

    async def teardown(self, handle: EngineHandle) -> None:
        if self.teardown_error is not None:
            raise self.teardown_error
        self.teardowns.append(handle)


class FakeResolver:
    def __init__(self, adapter: FakeEngineAdapter) -> None:
        self.adapter = adapter
        self.calls: list[tuple[PortKind, str | None]] = []

    async def resolve(
        self, kind: PortKind, connection_id: str | None = None, project_id: str | None = None
    ) -> Any:
        self.calls.append((kind, connection_id))
        return self.adapter


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
) -> tuple[FakeEngineRunsRepository, FakeRuns, FakeEngineAdapter, FakeResolver, TestClient]:
    repo = repo if repo is not None else FakeEngineRunsRepository()
    runs = runs if runs is not None else FakeRuns()
    adapter = FakeEngineAdapter(teardown_error=teardown_error)
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
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "dev-engine-sim")]
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
    assert resolver.calls == [(PortKind.EXECUTION_ENGINE, "conn-from-row")]
    assert repo.rows[1].status == "aborted"


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
