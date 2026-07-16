"""Gate CAS resume + abort: superseded 409, invalid action 422, conflict mapping."""

import asyncio
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
from apex.graphs.pipeline.configurable import Limits, PipelineConfigurable
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
from apex.services.pipeline_read import (
    PipelineReadService,
    RerunActiveRunConflictError,
    RerunConfigurationConflictError,
    _ensure_durable_config_ownership,
    _ensure_rerun_checkpoint_is_terminal,
    _run_config_from_state,
)

JsonDict = dict[str, Any]

GATE_PAYLOAD = {
    "schema_version": 1,
    "kind": "phase_review",
    "phase": "execution",
    "summary": "Execution completed.",
    "result_preview": {
        "summary": "Execution completed.",
        "reasoning_digest": "Provider results were collected.",
    },
    "artifacts": [],
    "warnings": [],
    "dialogue_tail": [],
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
        self.create_result: JsonDict = {"run_id": "run-42"}
        self.create_calls: list[JsonDict] = []
        self.cancelled: list[str] = []

    async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> JsonDict:
        self.create_calls.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        if self.conflict_on_create:
            raise _conflict()
        return self.create_result

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
    include_run_config: bool = True,
) -> JsonDict:
    # Mirrors the real get_state shape: plan_resolver checkpoints the complete
    # run config, with values["limits"] retained for old-thread compatibility.
    values: JsonDict = {}
    if limits is not None:
        values["limits"] = limits
    if include_run_config:
        values["run_config"] = (
            run_config if run_config is not None else PipelineConfigurable().snapshot()
        )
    return {
        "values": values,
        "tasks": [{"interrupts": [{"id": interrupt_id, "value": GATE_PAYLOAD}]}],
        "interrupts": [],
    }


def rerun_state(
    run_config: JsonDict,
    *,
    phase_results: JsonDict | None = None,
) -> JsonDict:
    values: JsonDict = {"run_config": run_config}
    if phase_results is not None:
        values["phase_results"] = phase_results
    return {"values": values, "tasks": [], "interrupts": []}


def trusted_rerun_snapshot() -> JsonDict:
    return PipelineConfigurable.model_validate(
        {
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
                "phase/execution": {
                    "content": "trusted prompt",
                    "version_id": "version-7",
                },
            },
            "pre_execution_context": ["trusted server-side context"],
            "model_by_phase": {},
            "agent_backend": "anthropic",
            "load_test": {"vusers": 17, "duration_s": 30},
            "limits": {"poll_interval_s": 7.0, "poll_timeout_s": 70.0},
        }
    ).snapshot()


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


async def test_rerun_rejects_credential_shaped_native_run_id() -> None:
    snapshot = trusted_rerun_snapshot()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = FakeRuns()
    runs.create_result = {"run_id": "password=rerun-native-id-secret-canary"}

    with pytest.raises(RuntimeError, match="invalid identifier") as excinfo:
        await PipelineReadService(FakeClient(threads, runs)).rerun_pipeline(
            "t-1",
            phases=["execution"],
            gates_mode="inherit",
            idempotency_key="rerun-request-1",
            principal_id="consumer-a",
        )

    assert "rerun-native-id-secret-canary" not in str(excinfo.value)


async def test_rerun_conflict_reconciliation_miss_detaches_sdk_request() -> None:
    canary = "Bearer rerun-conflict-request-secret-canary"

    class ConflictingRuns(FakeRuns):
        async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> JsonDict:
            del thread_id, assistant_id, kwargs
            request = httpx.Request(
                "POST",
                "http://loopback/threads/t-1/runs",
                headers={"Authorization": canary},
            )
            raise ConflictError(
                "provider conflict",
                response=httpx.Response(409, request=request),
                body={"diagnostic": canary},
            )

    snapshot = trusted_rerun_snapshot()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    service = PipelineReadService(FakeClient(threads, ConflictingRuns()))

    with pytest.raises(RerunActiveRunConflictError) as excinfo:
        await service.rerun_pipeline(
            "t-1",
            phases=["execution"],
            gates_mode="inherit",
            idempotency_key="rerun-request-1",
            principal_id="consumer-a",
        )

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in repr(excinfo.value)


async def test_rerun_snapshot_failure_definitively_settles_sibling_read() -> None:
    state_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class Threads(FakeThreads):
        async def get(self, thread_id: str) -> JsonDict:
            del thread_id
            await state_started.wait()
            raise RuntimeError("thread snapshot unavailable")

        async def get_state(self, thread_id: str) -> JsonDict:
            del thread_id
            state_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
            raise AssertionError("get_state should have been cancelled")

    service = PipelineReadService(FakeClient(Threads({}), FakeRuns()))
    operation = asyncio.create_task(
        service.rerun_pipeline(
            "t-1",
            phases=["execution"],
            gates_mode="inherit",
            idempotency_key="snapshot-failure",
            principal_id="consumer-a",
        )
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert not operation.done()
    release_cleanup.set()
    with pytest.raises(RuntimeError, match="thread snapshot unavailable"):
        await operation


async def test_resume_snapshot_parent_cancellation_settles_both_reads() -> None:
    both_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    active = 0
    cleaned = 0

    class Threads(FakeThreads):
        async def _read(self) -> JsonDict:
            nonlocal active, cleaned
            active += 1
            if active == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned += 1
                if cleaned == 2:
                    cleanup_started.set()
                await release_cleanup.wait()
            raise AssertionError("cancelled native read resumed")

        async def get(self, thread_id: str) -> JsonDict:
            del thread_id
            return await self._read()

        async def get_state(self, thread_id: str) -> JsonDict:
            del thread_id
            return await self._read()

    service = PipelineReadService(FakeClient(Threads({}), FakeRuns()))
    operation = asyncio.create_task(service.resume_gate("t-1", "int-1", "approve", {}))
    await asyncio.wait_for(both_started.wait(), timeout=1)

    operation.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert not operation.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cleaned == 2


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
    "phase_entry",
    [
        {"status": "awaiting_output_review", "attempt": 4},
        {"status": "awaiting_prompt_review", "attempt": 4},
        {"status": "running", "attempt": 4},
        {"status": "pending", "attempt": 4},
        {"status": "unknown", "attempt": 4},
        {"status": "succeeded", "attempt": "4"},
    ],
)
def test_rerun_rejects_unfinished_or_malformed_checkpoint_before_create(
    phase_entry: JsonDict,
) -> None:
    runs = DurableRerunRuns()
    threads = FakeThreads(
        {
            "t-1": rerun_state(
                trusted_rerun_snapshot(),
                phase_results={"execution": phase_entry},
            )
        },
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["reporting"],
                "gates_mode": "inherit",
                "idempotency_key": "must-resume-the-old-attempt",
            },
        )

    assert response.status_code == 409
    assert response.json()["title"] == "rerun_configuration_conflict"
    assert runs.create_calls == []


def test_rerun_accepts_a_terminal_checkpoint_as_a_new_attempt() -> None:
    runs = DurableRerunRuns()
    threads = FakeThreads(
        {
            "t-1": rerun_state(
                trusted_rerun_snapshot(),
                phase_results={"execution": {"status": "succeeded", "attempt": 4}},
            )
        },
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )

    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={
                "phases": ["execution"],
                "gates_mode": "inherit",
                "idempotency_key": "new-terminal-attempt",
            },
        )

    assert response.status_code == 202
    assert len(runs.create_calls) == 1


def test_rerun_checkpoint_guard_rejects_mapping_subclasses_without_hooks() -> None:
    class HostileEntry(dict[str, Any]):
        called = False

        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            self.called = True
            raise AssertionError("checkpoint mapping hooks must not execute")

    hostile_entry = HostileEntry(status="awaiting_output_review", attempt=3)

    with pytest.raises(RerunConfigurationConflictError, match="phase results are invalid"):
        _ensure_rerun_checkpoint_is_terminal(
            {"values": {"phase_results": {"execution": hostile_entry}}}
        )

    assert hostile_entry.called is False


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


def test_resume_engine_recovery_gate_retries_the_exact_durable_interrupt() -> None:
    runs = FakeRuns()
    state = state_with_interrupt("engine-retry-1")
    state["tasks"][0]["interrupts"][0]["value"] = {
        "schema_version": 1,
        "kind": "engine_cleanup_retry",
        "phase": "execution",
        "attempt": 2,
        "thread_id": "t-1",
        "actions": ["retry"],
        "error": "provider abort unavailable",
        "message": "Resume the exact durable provider attempt.",
    }
    client_app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(client_app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/engine-retry-1/resume",
            json={"action": "retry"},
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-42"}
    call = runs.create_calls[0]
    assert call["multitask_strategy"] == "reject"
    assert call["durability"] == "sync"
    assert call["command"] == {"resume": {"engine-retry-1": {"action": "retry"}}}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "kind": "unknown_recovery_gate",
            "phase": "execution",
            "attempt": 2,
            "thread_id": "t-1",
            "actions": ["retry"],
            "message": "Retry the durable provider attempt.",
        },
        {
            "schema_version": 1,
            "kind": "engine_cleanup_retry",
            "phase": "reporting",
            "attempt": 2,
            "thread_id": "t-1",
            "actions": ["retry"],
            "message": "Retry the durable provider attempt.",
        },
        {
            "schema_version": 1,
            "kind": "engine_cleanup_retry",
            "phase": "execution",
            "attempt": 2,
            "thread_id": "t-1",
            "actions": ["approve"],
            "message": "Retry the durable provider attempt.",
        },
    ],
)
def test_resume_gate_rejects_hidden_or_malformed_pending_gate(payload: JsonDict) -> None:
    runs = FakeRuns()
    state = state_with_interrupt("engine-retry-1")
    state["tasks"][0]["interrupts"][0]["value"] = payload
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/engine-retry-1/resume",
            json={"action": payload["actions"][0]},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "kind": "prompt_review",
            "phase": "execution",
            "actions": ["approve", "modify", "skip_phase", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "phase_review",
            "phase": "execution",
            "actions": ["approve", "revise", "discuss", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "prompt_review",
            "phase": "execution",
            "prompt": {
                "system": None,
                "user": " ",
                "application": None,
                "source": {"origin": "catalog", "ref": None},
            },
            "additional_context": "",
            "context_packets": [],
            "tools": [],
            "editable": True,
            "actions": ["approve", "modify", "skip_phase", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "phase_review",
            "phase": "execution",
            "summary": None,
            "result_preview": {"summary": None, "reasoning_digest": None},
            "artifacts": [],
            "warnings": [],
            "dialogue_tail": [],
            "actions": ["approve", "revise", "discuss", "abort"],
        },
    ],
)
def test_resume_gate_rejects_incomplete_or_empty_human_review(payload: JsonDict) -> None:
    runs = FakeRuns()
    state = state_with_interrupt("review-1")
    state["tasks"][0]["interrupts"][0]["value"] = payload
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/review-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


@pytest.mark.parametrize(
    ("payload", "canary"),
    [
        (
            {
                "schema_version": 1,
                "kind": "prompt_review",
                "phase": "execution",
                "prompt": {
                    "system": "Authorization: Bearer prompt-review-secret-canary",
                    "user": "Review the execution prompt.",
                    "application": None,
                    "source": {"origin": "catalog", "ref": None},
                },
                "additional_context": "",
                "context_packets": [],
                "tools": [],
                "editable": True,
                "actions": ["approve", "modify", "skip_phase", "abort"],
            },
            "prompt-review-secret-canary",
        ),
        (
            {
                "schema_version": 1,
                "kind": "phase_review",
                "phase": "execution",
                "summary": "token=phase-review-secret-canary",
                "result_preview": {
                    "summary": "Execution completed.",
                    "reasoning_digest": None,
                },
                "artifacts": [],
                "warnings": [],
                "dialogue_tail": [],
                "actions": ["approve", "revise", "discuss", "abort"],
            },
            "phase-review-secret-canary",
        ),
    ],
)
def test_resume_gate_rejects_redacted_human_review_contract(
    payload: JsonDict,
    canary: str,
) -> None:
    runs = FakeRuns()
    state = state_with_interrupt("review-1")
    state["tasks"][0]["interrupts"][0]["value"] = payload
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/review-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert canary not in response.text
    assert runs.create_calls == []


def test_resume_engine_recovery_gate_rejects_non_retry_action() -> None:
    runs = FakeRuns()
    state = state_with_interrupt("engine-retry-1")
    state["tasks"][0]["interrupts"][0]["value"] = {
        "schema_version": 1,
        "kind": "engine_cleanup_retry",
        "phase": "execution",
        "attempt": 2,
        "thread_id": "t-1",
        "actions": ["retry"],
        "message": "Retry the durable provider attempt.",
    }
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/engine-retry-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 422
    assert response.json()["title"] == "action is not allowed for this gate"
    assert runs.create_calls == []


def test_resume_engine_recovery_gate_rejects_cross_thread_payload_binding() -> None:
    runs = FakeRuns()
    state = state_with_interrupt("engine-retry-1")
    state["tasks"][0]["interrupts"][0]["value"] = {
        "schema_version": 1,
        "kind": "engine_cleanup_retry",
        "phase": "execution",
        "attempt": 2,
        "thread_id": "another-thread",
        "actions": ["retry"],
        "message": "Retry the durable provider attempt.",
    }
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/engine-retry-1/resume",
            json={"action": "retry"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"run_config": None},
        {"run_config": "legacy-default-provider"},
        {"run_config": {}},
    ],
)
def test_resume_gate_rejects_missing_or_malformed_durable_run_config(
    values: dict[str, Any],
) -> None:
    runs = FakeRuns()
    state = state_with_interrupt("int-1", include_run_config=False)
    state["values"] = values
    app = make_app(FakeClient(FakeThreads({"t-1": state}), runs))

    with TestClient(app) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume",
            json={"action": "approve"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "pipeline_configuration_conflict"
    assert runs.create_calls == []


def test_durable_replay_boundaries_reject_mapping_subclasses_without_hooks() -> None:
    class HostileMapping(dict[str, Any]):
        called = False

        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            self.called = True
            raise AssertionError("native response mapping hooks must not execute")

    hostile_state = HostileMapping()
    hostile_values = HostileMapping()
    hostile_thread = HostileMapping()
    hostile_metadata = HostileMapping()

    with pytest.raises(RerunConfigurationConflictError, match="state is invalid"):
        _run_config_from_state(hostile_state)
    with pytest.raises(RerunConfigurationConflictError, match="snapshot is missing"):
        _run_config_from_state({"values": hostile_values})
    with pytest.raises(RerunConfigurationConflictError, match="metadata is invalid"):
        _ensure_durable_config_ownership(PipelineConfigurable(), hostile_thread)
    with pytest.raises(RerunConfigurationConflictError, match="metadata is invalid"):
        _ensure_durable_config_ownership(
            PipelineConfigurable(),
            {"metadata": hostile_metadata},
        )

    assert hostile_state.called is False
    assert hostile_values.called is False
    assert hostile_thread.called is False
    assert hostile_metadata.called is False


async def test_resume_gate_rejects_path_delimiting_native_run_id() -> None:
    runs = FakeRuns()
    runs.create_result = {"run_id": "run/../../sibling?token=resume-secret-canary"}
    service = PipelineReadService(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs)
    )

    with pytest.raises(RuntimeError, match="invalid identifier") as excinfo:
        await service.resume_gate("t-1", "int-1", "approve", {})

    assert "resume-secret-canary" not in str(excinfo.value)


async def test_resume_gate_rejects_run_response_subclass_without_hooks() -> None:
    class HostileRunResponse(dict[str, Any]):
        called = False

        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            self.called = True
            raise AssertionError("run response hooks must not execute")

    response = HostileRunResponse(run_id="run-unsafe")
    runs = FakeRuns()
    runs.create_result = response
    service = PipelineReadService(
        FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs)
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await service.resume_gate("t-1", "int-1", "approve", {})

    assert response.called is False


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
    snapshot = PipelineConfigurable.model_validate(
        {
            "assistant_id": "assistant-golden",
            "project_id": "project-a",
            "app_id": "app-a",
            "engine": "loadrunner",
            "connections": {"execution_engine": "lre-a"},
            "agent_backend": "anthropic",
            "load_test": {},
            "gates": {
                "execution": {"prompt_review": "auto", "output_review": "gated"},
            },
            "limits": {"poll_interval_s": 7.0, "poll_timeout_s": 70.0},
        }
    ).snapshot()
    runs = FakeRuns()
    client_app = make_app(
        FakeClient(
            FakeThreads(
                {"t-1": state_with_interrupt("int-1", run_config=snapshot)},
                {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
            ),
            runs,
        )
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


def test_resume_gate_rejects_checkpoint_scope_drift_before_create() -> None:
    snapshot = PipelineConfigurable(
        project_id="project-b",
        app_id="app-b",
    ).snapshot()
    runs = FakeRuns()
    app = make_app(
        FakeClient(
            FakeThreads(
                {"t-1": state_with_interrupt("int-1", run_config=snapshot)},
                {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
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


def test_resume_gate_rejects_invalid_durable_limits_without_starting_run() -> None:
    runs = FakeRuns()
    run_config = PipelineConfigurable().snapshot()
    run_config["limits"] = {
        **run_config["limits"],
        "poll_timeout_s": "credential-shaped-invalid-value",
    }
    app = make_app(
        FakeClient(
            FakeThreads(
                {
                    "t-1": state_with_interrupt(
                        "int-1",
                        run_config=run_config,
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
    run_config = PipelineConfigurable.model_validate({"limits": limits}).snapshot()
    runs = FakeRuns()
    client_app = make_app(
        FakeClient(
            FakeThreads(
                {
                    "t-1": state_with_interrupt(
                        "int-1",
                        limits=limits,
                        run_config=run_config,
                    )
                }
            ),
            runs,
        )
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
