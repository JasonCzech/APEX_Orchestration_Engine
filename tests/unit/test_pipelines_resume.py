"""Gate CAS resume + abort: superseded 409, invalid action 422, conflict mapping."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import ConflictError

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.domain.pipeline import (
    ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
    EngineConnectionAffinityMissingError,
)
from apex.graphs.pipeline.configurable import Limits
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.routers.engines import get_engine_abort_service
from apex.routers.pipelines import get_pipeline_read_service, router
from apex.services.engine_abort import (
    EngineAbortConfirmationPendingError,
    EngineAbortResult,
    EngineGraphFinalizationPendingError,
    EngineProvisioningAbortPendingError,
    EngineRunNotFoundError,
)
from apex.services.langgraph_client import (
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
)
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
    def __init__(
        self,
        states: dict[str, JsonDict],
        metadata: dict[str, JsonDict] | None = None,
    ):
        self.states = states
        self.metadata = metadata or {}

    async def get(self, thread_id: str) -> JsonDict:
        if thread_id not in self.states:
            await self.get_state(thread_id)
        return {"thread_id": thread_id, "metadata": self.metadata.get(thread_id, {})}

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

    async def list(
        self,
        thread_id: str,
        *,
        status: str | None = None,
        limit: int = 10,
        offset: int = 0,
        **_: Any,
    ) -> list[JsonDict]:
        rows = [r for r in self.runs if status is None or r["status"] == status]
        return rows[offset : offset + limit]

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        self.cancelled.append(run_id)
        self.runs = [run for run in self.runs if run.get("run_id") != run_id]


class DurableRerunRuns(FakeRuns):
    def __init__(self, *, lose_first_response: bool = False) -> None:
        super().__init__()
        self.lose_first_response = lose_first_response

    async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> JsonDict:
        call = {"thread_id": thread_id, "assistant_id": assistant_id, **kwargs}
        self.create_calls.append(call)
        run = {
            "run_id": f"rerun-{len(self.create_calls)}",
            "status": "running",
            "metadata": kwargs.get("metadata") or {},
            "created_at": "2026-07-15T00:00:00+00:00",
        }
        self.runs.insert(0, run)
        if self.lose_first_response and len(self.create_calls) == 1:
            raise httpx.ReadTimeout("response lost after commit")
        return run


class FakeClient:
    def __init__(self, threads: FakeThreads, runs: FakeRuns):
        self.threads = threads
        self.runs = runs


class FakeEngineAbort:
    def __init__(
        self,
        result: EngineAbortResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def abort(self, thread_id: str, *, reason: str | None = None) -> EngineAbortResult:
        self.calls.append(thread_id)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise EngineRunNotFoundError(thread_id)
        return self.result


def identity(role: Role) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1", name="op", consumer_type=ConsumerType.DASHBOARD, role=role
    )


def make_app(
    client: FakeClient,
    role: Role = Role.OPERATOR,
    engine_abort: FakeEngineAbort | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_pipeline_read_service] = lambda: PipelineReadService(client)
    app.dependency_overrides[get_engine_abort_service] = lambda: engine_abort or FakeEngineAbort()
    app.dependency_overrides[get_current_identity] = lambda: identity(role)
    return app


def state_with_interrupt(
    interrupt_id: str,
    *,
    limits: dict[str, Any] | None = None,
    run_config: dict[str, Any] | None = None,
) -> JsonDict:
    # Mirrors the real get_state shape: plan_resolver checkpoints the complete
    # run config, with values["limits"] retained for old-thread compatibility.
    values: JsonDict = {}
    if limits is not None:
        values["limits"] = limits
    if run_config is not None:
        values["run_config"] = run_config
    return {
        "values": values,
        "tasks": [{"interrupts": [{"id": interrupt_id, "value": GATE_PAYLOAD}]}],
        "interrupts": [],
    }


def rerun_state(run_config: JsonDict) -> JsonDict:
    return {"values": {"run_config": run_config}, "tasks": [], "interrupts": []}


def trusted_rerun_snapshot() -> JsonDict:
    return {
        "assistant_id": "assistant-golden",
        "project_id": "project-a",
        "app_id": "app-a",
        "environment_id": "env-a",
        "environment_target": "https://private.example.test/load",
        "environment_target_version": 7,
        "engine": "loadrunner",
        "connections": {
            "execution_engine": "conn-engine",
            "artifact_store": "conn-artifact",
        },
        "phases": ["story_analysis", "execution"],
        "gates": {
            "execution": {"prompt_review": "auto", "output_review": "gated"},
        },
        "prompt_overrides": {
            "phase/execution": {"content": "trusted prompt", "version_id": "version-7"},
        },
        "pre_execution_context": ["trusted server-side context"],
        "model_by_phase": {},
        "agent_backend": "anthropic",
        "load_test": {"vusers": 17, "duration_s": 30},
        "limits": {"poll_interval_s": 7.0, "poll_timeout_s": 70.0},
    }


def test_rerun_facade_preserves_trusted_config_and_only_overrides_plan_controls() -> None:
    snapshot = trusted_rerun_snapshot()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = DurableRerunRuns()

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["execution", "reporting"],
                "gates_mode": "auto",
                "idempotency_key": "rerun-request-1",
            },
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "rerun-1"}
    call = runs.create_calls[0]
    assert call["assistant_id"] == "assistant-golden"
    assert call["input"] == {}
    assert call["stream_mode"] == "custom"
    assert call["stream_subgraphs"] is True
    assert call["stream_resumable"] is True
    assert call["durability"] == "sync"
    assert call["multitask_strategy"] == "reject"
    restored = call["config"]["configurable"]
    assert restored["connections"] == snapshot["connections"]
    assert restored["environment_target"] == snapshot["environment_target"]
    assert restored["prompt_overrides"] == snapshot["prompt_overrides"]
    assert restored["pre_execution_context"] == snapshot["pre_execution_context"]
    assert restored["load_test"] == snapshot["load_test"]
    assert restored["phases"] == ["execution", "reporting"]
    assert restored["start_phase"] is None and restored["stop_after"] is None
    assert all(
        policy == {"prompt_review": "auto", "output_review": "auto"}
        for policy in restored["gates"].values()
    )


async def test_rerun_ambiguous_retry_adopts_hashed_claim_without_raw_key_metadata() -> None:
    canary = "Bearer rerun-idempotency-secret-canary"
    snapshot = trusted_rerun_snapshot()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = DurableRerunRuns(lose_first_response=True)
    service = PipelineReadService(FakeClient(threads, runs))
    arguments = {
        "phases": ["execution"],
        "gates_mode": "inherit",
        "idempotency_key": canary,
        "principal_id": "consumer-a",
    }

    with pytest.raises(httpx.ReadTimeout, match="response lost"):
        await service.rerun_pipeline("t-1", **arguments)
    adopted = await service.rerun_pipeline("t-1", **arguments)

    assert adopted == "rerun-1"
    assert len(runs.create_calls) == 1
    metadata = runs.runs[0]["metadata"]
    assert set(metadata) == {RERUN_CLAIM_METADATA_KEY, RERUN_FINGERPRINT_METADATA_KEY}
    assert canary not in repr(metadata)
    assert "consumer-a" not in repr(metadata)


def test_rerun_rejects_idempotency_reuse_with_different_overrides() -> None:
    snapshot = trusted_rerun_snapshot()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = DurableRerunRuns()

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        first = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["execution"],
                "gates_mode": "inherit",
                "idempotency_key": "same-claim",
            },
        )
        conflict = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["reporting"],
                "gates_mode": "inherit",
                "idempotency_key": "same-claim",
            },
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["title"] == "rerun_idempotency_conflict"
    assert len(runs.create_calls) == 1


def test_rerun_rejects_checkpoint_scope_drift_before_create() -> None:
    threads = FakeThreads(
        {"t-1": rerun_state(trusted_rerun_snapshot())},
        {"t-1": {"project_id": "other-project", "app_id": "app-a"}},
    )
    runs = DurableRerunRuns()

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["execution"],
                "gates_mode": "inherit",
                "idempotency_key": "drift-check",
            },
        )

    assert response.status_code == 409
    assert response.json()["title"] == "rerun_configuration_conflict"
    assert runs.create_calls == []


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("script_refs", ["legacy-attacker-selected-script"]),
        ("test_id", 731),
        ("test_instance_id", 947),
    ],
)
def test_rerun_rejects_legacy_untrusted_provider_selectors(selector: str, value: Any) -> None:
    snapshot = trusted_rerun_snapshot()
    snapshot["load_test"] = {selector: value}
    runs = DurableRerunRuns()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["execution"],
                "gates_mode": "inherit",
                "idempotency_key": f"legacy-{selector}",
            },
        )

    assert response.status_code == 409
    assert response.json()["title"] == "rerun_configuration_conflict"
    assert runs.create_calls == []


def test_rerun_unknown_or_unscoped_thread_is_generic_404_without_create() -> None:
    runs = DurableRerunRuns()
    with TestClient(make_app(FakeClient(FakeThreads({}), runs))) as client:
        response = client.post(
            "/v1/pipelines/cross-scope-canary/rerun",
            json={
                "phases": ["execution"],
                "gates_mode": "inherit",
                "idempotency_key": "hidden-thread",
            },
        )

    assert response.status_code == 404
    body = response.json()
    assert body.get("detail") == "pipeline thread not found" or body.get("title") == (
        "pipeline thread not found"
    )
    assert "cross-scope-canary" not in response.text
    assert runs.create_calls == []


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
    assert call["durability"] == "sync"
    assert call["config"]["recursion_limit"] > 25
    assert call["command"] == {"resume": {"int-1": {"action": "approve", "note": "lgtm"}}}


def test_resume_gate_rejects_nul_before_checkpoint_write() -> None:
    runs = FakeRuns()
    client_app = make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs))

    with TestClient(client_app) as client:
        message = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "discuss", "message": "unsafe\u0000message"},
        )
        prompt = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "modify", "prompt": {"system": "unsafe\u0000prompt"}},
        )

    assert message.status_code == 422
    assert prompt.status_code == 422
    assert runs.create_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "discuss", "message": "Authorization: Bearer gate-secret-canary"},
        {"action": "revise", "instructions": "password=gate-secret-canary"},
        {"action": "approve", "note": "https://user:gate-secret-canary@example.test"},
        {"action": "revise", "prompt": {"system": "private_key=gate-secret-canary"}},
    ],
)
def test_resume_gate_rejects_credentials_before_durable_resume(
    payload: dict[str, Any],
) -> None:
    runs = FakeRuns()
    client_app = make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs))

    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json=payload,
        )

    assert response.status_code == 422
    assert "gate-secret-canary" not in response.text
    assert runs.create_calls == []


def test_resume_gate_rejects_legacy_checkpointed_credentials_before_replay() -> None:
    runs = FakeRuns()
    snapshot = {
        "assistant_id": "pipeline",
        "pre_execution_context": ["Authorization: Bearer replay-secret-canary"],
    }
    client_app = make_app(
        FakeClient(
            FakeThreads({"t-1": state_with_interrupt("int-1", run_config=snapshot)}),
            runs,
        )
    )

    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert "replay-secret-canary" not in response.text
    assert runs.create_calls == []


def test_resume_gate_restores_complete_checkpointed_run_config() -> None:
    snapshot = {
        "assistant_id": "assistant-golden",
        "project_id": "project-a",
        "app_id": "app-a",
        "engine": "loadrunner",
        "connections": {"execution_engine": "lre-a"},
        "agent_backend": "anthropic",
        "load_test": {},
        "gates": {"execution": {"prompt_review": "auto", "output_review": "gated"}},
        "limits": {"poll_interval_s": 7.0, "poll_timeout_s": 70.0},
    }
    runs = FakeRuns()
    client_app = make_app(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1", run_config=snapshot)}), runs)
    )
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve"})

    assert response.status_code == 202
    call = runs.create_calls[0]
    assert call["assistant_id"] == "assistant-golden"
    restored = call["config"]["configurable"]
    assert restored["assistant_id"] == "assistant-golden"
    assert restored["project_id"] == "project-a"
    assert restored["app_id"] == "app-a"
    assert restored["engine"] == "loadrunner"
    assert restored["connections"] == {"execution_engine": "lre-a"}
    assert restored["agent_backend"] == "anthropic"
    assert restored["load_test"] == {}


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("script_refs", ["legacy-attacker-selected-script"]),
        ("test_id", 731),
        ("test_instance_id", 947),
    ],
)
def test_resume_gate_rejects_legacy_untrusted_provider_selectors(selector: str, value: Any) -> None:
    snapshot = trusted_rerun_snapshot()
    snapshot["load_test"] = {selector: value}
    runs = FakeRuns()
    app = make_app(
        FakeClient(
            FakeThreads({"t-1": state_with_interrupt("int-1", run_config=snapshot)}),
            runs,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


def test_resume_gate_rejects_invalid_legacy_limits_without_starting_run() -> None:
    runs = FakeRuns()
    app = make_app(
        FakeClient(
            FakeThreads(
                {
                    "t-1": state_with_interrupt(
                        "int-1",
                        limits={"poll_timeout_s": "credential-shaped-invalid-value"},
                    )
                }
            ),
            runs,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


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


def test_resume_gate_superseded_response_drops_interrupt_payload_secrets() -> None:
    canary = "SUPERSEDED_GATE_SECRET_CANARY"
    payload = {**GATE_PAYLOAD, "provider_token": canary}
    state = {
        "values": {},
        "tasks": [{"interrupts": [{"id": "int-2", "value": payload}]}],
        "interrupts": [],
    }
    client_app = make_app(FakeClient(FakeThreads({"t-1": state}), FakeRuns()))

    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["pending_gate"] == {
        "interrupt_id": "int-2",
        "kind": "phase_review",
        "phase": "execution",
    }
    assert canary not in response.text


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
    assert response.json()["title"] == "action is not allowed for this gate"


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
    assert response.json() == {
        "cancelled_run_ids": ["r-run", "r-pend"],
        "phase": None,
        "confirmed": False,
    }
    assert runs.cancelled == ["r-run", "r-pend"]


def test_abort_uses_engine_kill_switch_when_execution_handle_exists() -> None:
    runs = FakeRuns(runs=[{"run_id": "should-not-fallback", "status": "running"}])
    engine_abort = FakeEngineAbort(
        EngineAbortResult(
            thread_id="t-1",
            engine="loadrunner",
            external_run_id="42",
            cancelled_runs=["engine-cancelled-run"],
        )
    )
    client_app = make_app(FakeClient(FakeThreads({}), runs), engine_abort=engine_abort)
    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")

    assert response.status_code == 202
    assert response.json() == {
        "cancelled_run_ids": ["engine-cancelled-run"],
        "phase": None,
        "confirmed": False,
    }
    assert engine_abort.calls == ["t-1"]
    assert runs.cancelled == []


def test_abort_surfaces_retryable_engine_confirmation_timeout() -> None:
    runs = FakeRuns(runs=[{"run_id": "must-remain", "status": "running"}])
    engine_abort = FakeEngineAbort(error=EngineAbortConfirmationPendingError("t-1"))
    client_app = make_app(FakeClient(FakeThreads({}), runs), engine_abort=engine_abort)

    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert runs.cancelled == []


def test_abort_during_engine_provisioning_never_falls_back_to_graph_cancel() -> None:
    runs = FakeRuns(runs=[{"run_id": "must-remain", "status": "running"}])
    engine_abort = FakeEngineAbort(error=EngineProvisioningAbortPendingError("t-1"))
    client_app = make_app(FakeClient(FakeThreads({}), runs), engine_abort=engine_abort)

    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert runs.cancelled == []


def test_abort_without_engine_monitor_surfaces_finalization_recovery() -> None:
    runs = FakeRuns(runs=[{"run_id": "must-remain", "status": "running"}])
    engine_abort = FakeEngineAbort(error=EngineGraphFinalizationPendingError("t-1"))
    client_app = make_app(FakeClient(FakeThreads({}), runs), engine_abort=engine_abort)

    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == (
        "external engine stopped but graph finalization is pending recovery; resume the pipeline"
    )
    assert runs.cancelled == []


def test_abort_surfaces_missing_legacy_engine_connection_affinity() -> None:
    runs = FakeRuns(runs=[{"run_id": "must-remain", "status": "running"}])
    engine_abort = FakeEngineAbort(error=EngineConnectionAffinityMissingError())
    client_app = make_app(FakeClient(FakeThreads({}), runs), engine_abort=engine_abort)

    with TestClient(client_app) as client:
        response = client.post("/v1/pipelines/t-1/abort")

    assert response.status_code == 409
    assert response.json()["title"] == ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL
    assert runs.cancelled == []


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
