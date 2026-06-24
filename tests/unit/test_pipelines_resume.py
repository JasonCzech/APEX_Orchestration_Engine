"""Gate CAS resume + abort: superseded 409, invalid action 422, conflict mapping."""

from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import ConflictError

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.graphs.pipeline.configurable import Limits
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.routers.pipelines import get_pipeline_read_service, router
from apex.services.pipeline_read import PipelineReadService

JsonDict = dict[str, Any]

GATE_PAYLOAD = {
    "schema_version": 1,
    "kind": "phase_review",
    "phase": "execution",
    "actions": ["approve", "revise", "discuss", "abort"],
}


def _conflict() -> ConflictError:
    request = httpx.Request("POST", "http://loopback/threads/t-1/runs")
    return ConflictError("conflict", response=httpx.Response(409, request=request), body=None)


class FakeThreads:
    def __init__(self, states: dict[str, JsonDict]):
        self.states = states

    async def get_state(self, thread_id: str) -> JsonDict:
        from langgraph_sdk.errors import NotFoundError

        try:
            return self.states[thread_id]
        except KeyError:
            request = httpx.Request("GET", "http://loopback/threads/x/state")
            raise NotFoundError(
                "not found", response=httpx.Response(404, request=request), body=None
            ) from None


class FakeRuns:
    def __init__(self, *, conflict_on_create: bool = False, runs: list[JsonDict] | None = None):
        self.conflict_on_create = conflict_on_create
        self.runs = runs or []
        self.create_calls: list[JsonDict] = []
        self.cancelled: list[str] = []

    async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> JsonDict:
        self.create_calls.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        if self.conflict_on_create:
            raise _conflict()
        return {"run_id": "run-42"}

    async def list(self, thread_id: str, *, status: str | None = None, **_: Any) -> list[JsonDict]:
        return [r for r in self.runs if status is None or r["status"] == status]

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        self.cancelled.append(run_id)


class FakeClient:
    def __init__(self, threads: FakeThreads, runs: FakeRuns):
        self.threads = threads
        self.runs = runs


def identity(role: Role) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1", name="op", consumer_type=ConsumerType.DASHBOARD, role=role
    )


def make_app(client: FakeClient, role: Role = Role.OPERATOR) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_pipeline_read_service] = lambda: PipelineReadService(client)
    app.dependency_overrides[get_current_identity] = lambda: identity(role)
    return app


def state_with_interrupt(interrupt_id: str, *, limits: dict[str, Any] | None = None) -> JsonDict:
    # Mirrors the real get_state shape: plan_resolver checkpoints the resolved limits into
    # values["limits"] (graph.plan_resolver), which is what _limits_from_state reads.
    return {
        "values": {"limits": limits} if limits is not None else {},
        "tasks": [{"interrupts": [{"id": interrupt_id, "value": GATE_PAYLOAD}]}],
        "interrupts": [],
    }


def test_resume_gate_accepts_and_creates_reject_run() -> None:
    runs = FakeRuns()
    client_app = make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs))
    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve", "note": "lgtm"},
        )
    assert response.status_code == 202
    assert response.json() == {"run_id": "run-42"}
    call = runs.create_calls[0]
    assert call["assistant_id"] == "pipeline"
    assert call["multitask_strategy"] == "reject"
    assert call["config"]["recursion_limit"] > 25
    assert call["command"] == {"resume": {"action": "approve", "note": "lgtm"}}


def test_resume_gate_uses_thread_limits_for_recursion_budget() -> None:
    limits = {"poll_interval_s": 0.5, "poll_timeout_s": 3600.0}
    runs = FakeRuns()
    client_app = make_app(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1", limits=limits)}), runs)
    )
    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 202
    assert runs.create_calls[0]["config"]["recursion_limit"] == recommended_recursion_limit(
        Limits.model_validate(limits)
    )


def test_resume_gate_superseded_when_interrupt_id_differs() -> None:
    runs = FakeRuns()
    client_app = make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-2")}), runs))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"})
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "gate_superseded"
    assert body["pending_gate"]["interrupt_id"] == "int-2"
    assert body["pending_gate"]["kind"] == "phase_review"
    assert runs.create_calls == []  # CAS precheck blocked the run


def test_resume_gate_superseded_when_no_interrupt_pending() -> None:
    state = {"values": {}, "tasks": [], "interrupts": []}
    client_app = make_app(FakeClient(FakeThreads({"t-1": state}), FakeRuns()))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"})
    assert response.status_code == 409
    assert response.json()["pending_gate"] is None


def test_resume_gate_invalid_action_is_422() -> None:
    client_app = make_app(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), FakeRuns())
    )
    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume", json={"action": "modify"}
        )  # phase_review gate has no "modify"
    assert response.status_code == 422
    assert "modify" in response.json()["title"]


def test_resume_gate_maps_sdk_conflict_to_superseded() -> None:
    runs = FakeRuns(conflict_on_create=True)
    client_app = make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"})
    assert response.status_code == 409
    assert response.json()["title"] == "gate_superseded"


def test_resume_gate_unknown_thread_is_404() -> None:
    client_app = make_app(FakeClient(FakeThreads({}), FakeRuns()))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/nope/gates/int-1/resume", json={"action": "approve"})
    assert response.status_code == 404


def test_resume_gate_requires_operator_role() -> None:
    client_app = make_app(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), FakeRuns()),
        role=Role.VIEWER,
    )
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"})
    assert response.status_code == 403


def test_abort_cancels_running_and_pending_runs() -> None:
    runs = FakeRuns(
        runs=[
            {"run_id": "r-run", "status": "running"},
            {"run_id": "r-pend", "status": "pending"},
            {"run_id": "r-done", "status": "success"},
        ]
    )
    client_app = make_app(FakeClient(FakeThreads({}), runs))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")
    assert response.status_code == 202
    assert response.json() == {"cancelled_run_ids": ["r-run", "r-pend"]}
    assert runs.cancelled == ["r-run", "r-pend"]


def test_abort_with_no_active_run_is_409() -> None:
    client_app = make_app(FakeClient(FakeThreads({}), FakeRuns()))
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")
    assert response.status_code == 409
    assert response.json()["title"] == "no_active_run"


def test_abort_requires_operator_role() -> None:
    client_app = make_app(FakeClient(FakeThreads({}), FakeRuns()), role=Role.VIEWER)
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")
    assert response.status_code == 403
