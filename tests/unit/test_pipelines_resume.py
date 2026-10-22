"""Gate CAS resume + abort: superseded 409, invalid action 422, conflict mapping."""

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, StateSnapshot
from langgraph_sdk.errors import ConflictError

from apex.adapters.stubs import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.app.security import (
    _direct_langgraph_body_limit_error,
    _direct_langgraph_query_limit_error,
)
from apex.auth.identity import RUNTIME_ACTOR_ID_KEY, ConsumerIdentity, ConsumerType, Role
from apex.domain.pipeline import (
    ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL,
    EngineConnectionAffinityMissingError,
    Phase,
)
from apex.graphs.pipeline import phase_subgraph
from apex.graphs.pipeline.configurable import Limits, PipelineConfigurable
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.graphs.pipeline.gates import build_prompt_review_payload
from apex.graphs.pipeline.graph import builder
from apex.routers.engines import get_engine_abort_service
from apex.routers.pipelines import get_pipeline_read_service, router
from apex.services.connections import ConnectionResolver
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


async def test_rerun_reconciliation_queries_satisfy_native_guard_before_and_after_retry() -> None:
    from apex.services import langgraph_client

    class GuardedRuns(DurableRerunRuns):
        async def list(self, thread_id: str, **kwargs: Any) -> list[JsonDict]:
            error = _direct_langgraph_query_limit_error(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/threads/{thread_id}/runs",
                    "headers": [
                        (
                            langgraph_client._TRUSTED_LOOPBACK_HEADER.encode(),
                            langgraph_client._TRUSTED_LOOPBACK_TOKEN.encode(),
                        ),
                    ],
                    "query_string": urlencode(kwargs, doseq=True).encode(),
                }
            )
            assert error is None
            return await super().list(thread_id, **kwargs)

    threads = FakeThreads(
        {"t-1": rerun_state(trusted_rerun_snapshot())},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = GuardedRuns(lose_first_response=True)
    service = PipelineReadService(FakeClient(threads, runs))
    arguments = {
        "phases": ["execution"],
        "gates_mode": "auto",
        "idempotency_key": "guarded-rerun",
        "principal_id": "consumer-a",
    }
    with pytest.raises(httpx.ReadTimeout, match="response lost"):
        await service.rerun_pipeline("t-1", **arguments)
    assert await service.rerun_pipeline("t-1", **arguments) == "rerun-1"
    assert len(runs.create_calls) == 1


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
    operation = asyncio.create_task(
        service.resume_gate("t-1", "int-1", "approve", {}, principal_id="c1")
    )
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
    assert call["config"]["configurable"][RUNTIME_ACTOR_ID_KEY] == "c1"
    assert call["command"] == {"resume": {"int-1": {"action": "approve", "note": "lgtm"}}}


@pytest.mark.parametrize(
    "field", ["actor", "principal_id", "langgraph_auth_user_id", RUNTIME_ACTOR_ID_KEY]
)
def test_resume_route_rejects_caller_supplied_attribution(field: str) -> None:
    runs = FakeRuns()
    with TestClient(
        make_app(FakeClient(FakeThreads({"t-1": state_with_interrupt("int-1")}), runs))
    ) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/int-1/resume", json={"action": "approve", field: "forged"}
        )
    assert response.status_code == 422
    assert runs.create_calls == []


@pytest.mark.parametrize("principal_id", ["", "x" * 256, "invalid\x00actor", "password=secret"])
async def test_resume_service_requires_valid_attribution_before_starting_a_run(
    principal_id: str,
) -> None:
    runs = FakeRuns()
    service = PipelineReadService(FakeClient(FakeThreads({}), runs))
    with pytest.raises(ValueError, match="authenticated principal"):
        await service.resume_gate("t-1", "int-1", "approve", {}, principal_id=principal_id)
    assert runs.create_calls == []


@pytest.mark.parametrize("skip", [False, True])
def test_resume_route_persists_each_authenticated_consumer_in_real_graph_approvals(
    monkeypatch: pytest.MonkeyPatch, skip: bool
) -> None:
    """Exercise HTTP dependency -> facade -> checkpointed graph, with two operators."""
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())
    MemoryArtifactStore.clear()
    graph = builder.compile(checkpointer=InMemorySaver())
    initial_config: RunnableConfig = {
        "configurable": {
            "thread_id": "t-1",
            "phases": ["story_analysis"],
            "langgraph_auth_user_id": "previous-operator",
            "langgraph_auth_user": {"identity": "previous-operator"},
        }
    }
    graph.invoke({"title": "Attribution regression"}, initial_config)

    class GraphThreads(FakeThreads):
        async def get_state(self, thread_id: str) -> JsonDict:
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return {
                "values": snapshot.values,
                "tasks": [
                    {
                        "interrupts": [
                            {"id": item.id, "value": item.value} for item in task.interrupts
                        ]
                    }
                    for task in snapshot.tasks
                ],
            }

    class GraphRuns(FakeRuns):
        async def create(self, thread_id: str, assistant_id: str, **kwargs: Any) -> JsonDict:
            assert (
                _direct_langgraph_body_limit_error(
                    "run_create", json.dumps(kwargs).encode(), trusted_loopback=True
                )
                is None
            )
            result = await super().create(thread_id, assistant_id, **kwargs)
            # Simulate persisted runtime metadata being merged into a new run.
            # The facade must explicitly override it with this request's actor.
            run_config: RunnableConfig = {
                "configurable": {
                    **initial_config["configurable"],
                    **kwargs["config"]["configurable"],
                },
                "recursion_limit": kwargs["config"]["recursion_limit"],
            }
            await graph.ainvoke(Command(resume=kwargs["command"]["resume"]), run_config)
            return result

    runs = GraphRuns()
    app = make_app(FakeClient(GraphThreads({"t-1": {}}), runs))
    current_consumer = ConsumerIdentity(
        consumer_id="consumer-one",
        name="First operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
    )
    app.dependency_overrides[get_current_identity] = lambda: current_consumer

    def pending_id() -> str:
        return graph.get_state(initial_config).tasks[0].interrupts[0].id

    def persisted_approvals() -> list[JsonDict]:
        snapshot = graph.get_state(initial_config, subgraphs=True)
        if snapshot.tasks:
            subgraph_state = snapshot.tasks[0].state
            assert isinstance(subgraph_state, StateSnapshot)
            values = subgraph_state.values
        else:
            values = snapshot.values
        return values["phase_results"]["story_analysis"]["approvals"]

    def decide(client: TestClient, action: str, **extras: Any) -> None:
        response = client.post(
            f"/v1/pipelines/t-1/gates/{pending_id()}/resume", json={"action": action, **extras}
        )
        assert response.status_code == 202, response.text

    try:
        with TestClient(app) as client:
            if skip:
                decide(client, "skip_phase")
                assert [(a["action"], a["actor"]) for a in persisted_approvals()] == [
                    ("skip_phase", "consumer-one")
                ]
            else:
                original_interrupt_id = pending_id()
                decide(client, "approve")
                assert [(a["action"], a["actor"]) for a in persisted_approvals()] == [
                    ("approve", "consumer-one")
                ]
                current_consumer = current_consumer.model_copy(
                    update={"consumer_id": "consumer-two"}
                )
                stale = client.post(
                    f"/v1/pipelines/t-1/gates/{original_interrupt_id}/resume",
                    json={"action": "approve"},
                )
                assert stale.status_code == 409
                assert len(runs.create_calls) == 1
                decide(client, "discuss", message="Explain the scope")
                current_consumer = current_consumer.model_copy(
                    update={"consumer_id": "consumer-one"}
                )
                decide(client, "revise", instructions="Include the latency target")
                current_consumer = current_consumer.model_copy(
                    update={"consumer_id": "consumer-two"}
                )
                decide(client, "approve")
                assert [(a["action"], a["actor"]) for a in persisted_approvals()] == [
                    ("approve", "consumer-one"),
                    ("discuss", "consumer-two"),
                    ("revise", "consumer-one"),
                    ("approve", "consumer-two"),
                ]
            response = client.get("/v1/pipelines/t-1")
            assert response.status_code == 200
            public_approvals = response.json()["values"]["phase_results"]["story_analysis"][
                "approvals"
            ]
            assert [(item["action"], item["actor"]) for item in public_approvals] == [
                (item["action"], item["actor"]) for item in persisted_approvals()
            ]
            final = graph.get_state(initial_config)
            assert not final.tasks
            assert "langgraph_auth_user_id" not in final.values["run_config"]
            assert RUNTIME_ACTOR_ID_KEY not in final.values["run_config"]
            assert RUNTIME_ACTOR_ID_KEY not in response.text
            approvals = persisted_approvals()
            assert len({item["id"] for item in approvals}) == len(approvals)
    finally:
        MemoryArtifactStore.clear()


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


def test_resume_large_unicode_prompt_modify_preserves_the_exact_edit() -> None:
    runs = FakeRuns()
    state = state_with_interrupt("prompt-1")
    original = {"system": "测" * 45_000, "user": "验" * 45_000}
    state["tasks"][0]["interrupts"][0]["value"] = build_prompt_review_payload(
        Phase.EXECUTION, original, {"origin": "run_override"}, [], []
    )
    edited = {
        "system": original["system"] + " edited",
        "user": original["user"],
        "application": None,
    }
    with TestClient(make_app(FakeClient(FakeThreads({"t-1": state}), runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/prompt-1/resume",
            json={"action": "modify", "prompt": edited},
        )
    assert response.status_code == 202
    assert len(runs.create_calls) == 1
    assert runs.create_calls[0]["command"]["resume"]["prompt-1"]["prompt"] == edited


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
        await service.resume_gate("t-1", "int-1", "approve", {}, principal_id="c1")

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
        await service.resume_gate("t-1", "int-1", "approve", {}, principal_id="c1")

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


async def test_cycle_bound_execution_cannot_be_rerun_with_changed_or_cleared_binding() -> None:
    snapshot = trusted_rerun_snapshot()
    snapshot["cycle_dispatch_id"] = "dispatch"
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)}, {"t-1": {"project_id": "project-a", "app_id": "app-a"}}
    )
    runs = FakeRuns()
    service = PipelineReadService(FakeClient(threads, runs))
    with pytest.raises(RerunConfigurationConflictError, match="new approved cycle binding"):
        await service.rerun_pipeline(
            "t-1",
            phases=["execution"],
            gates_mode="inherit",
            idempotency_key="forged-rerun",
            principal_id="consumer-a",
        )
    assert runs.create_calls == []


def test_cycle_bound_rerun_http_explains_required_new_preparation() -> None:
    snapshot = trusted_rerun_snapshot()
    snapshot["cycle_dispatch_id"] = "dispatch"
    runs = FakeRuns()
    threads = FakeThreads(
        {"t-1": rerun_state(snapshot)}, {"t-1": {"project_id": "project-a", "app_id": "app-a"}}
    )
    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/rerun",
            json={"phases": ["execution"], "gates_mode": "inherit", "idempotency_key": "rerun"},
        )
    assert response.status_code == 409
    assert response.json()["title"] == "rerun_configuration_conflict"
    assert "new approved cycle binding" in response.json()["detail"]
    assert runs.create_calls == []


@pytest.mark.parametrize("engine", ["sim", "apex_load", "loadrunner"])
def test_environment_context_without_target_can_replay_only_for_simulation(engine: str) -> None:
    config = PipelineConfigurable(
        engine=engine, project_id="project", app_id="app", environment_id="environment"
    )
    thread = {"metadata": {"project_id": "project", "app_id": "app"}}
    if engine == "sim":
        _ensure_durable_config_ownership(config, thread)
        with pytest.raises(RerunConfigurationConflictError):
            _ensure_durable_config_ownership(config, {"metadata": {"project_id": "project"}})
    else:
        with pytest.raises(RerunConfigurationConflictError):
            _ensure_durable_config_ownership(config, thread)


@pytest.mark.parametrize("change_system", [False, True])
def test_http_context_entry_edits_keep_system_locked(change_system) -> None:
    runs = FakeRuns()
    state = state_with_interrupt("prompt-1")
    original = {
        "system": "Locked shared system",
        "user": "Phase context",
        "system_locked": True,
        "application": "=== APPLICATION CONTEXT: shared ===\nOriginal context",
        "contexts": [
            {
                "namespace": "application",
                "is_shared": True,
                "key": "shared",
                "prompt_id": "app-prompt",
                "version_id": "app-v1",
                "version": 1,
                "content": "Original context",
            }
        ],
        "selections": [],
    }
    state["tasks"][0]["interrupts"][0]["value"] = build_prompt_review_payload(
        Phase.EXECUTION, original, {"origin": "catalog", "ref": "manifest:v1"}, [], []
    )
    edited = {
        "user": "Edited phase",
        "context_edits": [{"prompt_id": "app-prompt", "content": "Phase-local context"}],
    }
    if change_system:
        edited["system"] = "Attempted replacement"
    with TestClient(make_app(FakeClient(FakeThreads({"t-1": state}), runs))) as client:
        response = client.post(
            "/v1/pipelines/t-1/gates/prompt-1/resume", json={"action": "modify", "prompt": edited}
        )
    assert response.status_code == (422 if change_system else 202)
    if change_system:
        assert not runs.create_calls
    else:
        assert runs.create_calls[0]["command"]["resume"]["prompt-1"]["prompt"] == edited


def test_copy_setup_preserves_selected_versions_without_manifest_or_target_authority() -> None:
    system = {
        "namespace": "system",
        "key": "alternative",
        "prompt_id": "sys",
        "version_id": "sys-v1",
        "version": 1,
        "content": "Frozen system",
    }
    phase = {
        "namespace": "phase",
        "phase": "story_analysis",
        "key": "alternative",
        "prompt_id": "phase",
        "version_id": "phase-v2",
        "version": 2,
        "content": "Frozen phase",
    }

    def ref(item):
        return {"prompt_id": item["prompt_id"], "version_id": item["version_id"]}

    selections = {
        "system": ref(system),
        "phases": {"story_analysis": ref(phase)},
        "application": [],
        "environment": [],
    }
    manifest = {
        "system": system,
        "phases": {"story_analysis": phase},
        "application": [],
        "environment": [],
    }
    cfg = PipelineConfigurable.model_validate(
        {
            "phases": ["story_analysis"],
            "project_id": "project-a",
            "app_id": "app-a",
            "prompt_selections": selections,
            "prompt_manifest": manifest,
        }
    )
    state = rerun_state(cfg.snapshot())
    state["values"].update(title="Original test", request="Original request")
    threads = FakeThreads(
        {"t-1": state}, metadata={"t-1": {"project_id": "project-a", "app_id": "app-a"}}
    )
    with TestClient(make_app(FakeClient(threads, FakeRuns()))) as client:
        response = client.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 200
    exported = response.json()
    assert exported["configurable"]["prompt_selections"]["system"] == {
        **ref(system),
        "override_content": None,
    }
    assert exported["configurable"]["prompt_selections"]["phases"]["story_analysis"] == {
        **ref(phase),
        "override_content": None,
    }
    assert "prompt_manifest" not in exported["configurable"]
    assert "environment_target" not in exported["configurable"]
    assert "cycle_dispatch_id" not in exported["configurable"]


def test_copy_setup_uses_the_submitted_launch_when_no_plan_was_recorded() -> None:
    """A run that failed before plan_resolver has no checkpoint; copy what was sent."""
    scope = {
        "schema_version": 1,
        "participants": [
            {
                "id": "backend",
                "layer": "backend",
                "project_id": "project-a",
                "application_id": "app-a",
                "environment_id": "env-a",
            }
        ],
        "primary_participant_id": "backend",
        "traffic_entry_participant_id": "backend",
    }
    selections = {
        "system": {"prompt_id": "sys", "version_id": "sys-v1"},
        "phases": {"story_analysis": {"prompt_id": "phase", "version_id": "phase-v2"}},
    }
    submitted = PipelineConfigurable.model_validate(
        {
            "phases": ["story_analysis"],
            "project_id": "project-a",
            "app_id": "app-a",
            "environment_id": "env-a",
            "test_scope": scope,
            "prompt_selections": selections,
        }
    ).snapshot()

    class SubmittedRuns(FakeRuns):
        async def get(self, thread_id: str, run_id: str) -> JsonDict:
            assert (thread_id, run_id) == ("t-1", "run-1")
            configurable = {**submitted, "assistant_id": "pipeline", "langgraph_request_id": "r"}
            return {"run_id": run_id, "kwargs": {"config": {"configurable": configurable}}}

    runs = SubmittedRuns(
        runs=[{"run_id": "run-1", "status": "error", "metadata": {}, "created_at": "2026-10-22"}]
    )
    state = {"values": {"title": "Failed launch", "request": "Check peak"}, "tasks": []}
    threads = FakeThreads(
        {"t-1": state}, metadata={"t-1": {"project_id": "project-a", "app_id": "app-a"}}
    )
    with TestClient(make_app(FakeClient(threads, runs))) as client:
        response = client.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 200
    exported = response.json()
    assert exported["title"] == "Failed launch"
    assert exported["test_scope"]["primary_participant_id"] == "backend"
    assert (
        exported["configurable"]["prompt_selections"]["phases"]["story_analysis"]["version_id"]
        == "phase-v2"
    )
    assert "prompt_manifest" not in exported["configurable"]


def _submitted_setup_client(*, metadata: JsonDict, get_error: Exception | None = None):
    submitted = PipelineConfigurable.model_validate(
        {"phases": ["story_analysis"], "project_id": "project-a", "app_id": "app-a"}
    ).snapshot()

    class SubmittedRuns(FakeRuns):
        async def get(self, thread_id: str, run_id: str) -> JsonDict:
            if get_error is not None:
                raise get_error
            return {"run_id": run_id, "kwargs": {"config": {"configurable": submitted}}}

    runs = SubmittedRuns(
        runs=[{"run_id": "run-1", "status": "error", "metadata": {}, "created_at": "2026-10-22"}]
    )
    state = {"values": {"title": "Failed launch"}, "tasks": []}
    return FakeClient(FakeThreads({"t-1": state}, metadata={"t-1": metadata}), runs)


def test_copy_setup_fallback_still_enforces_thread_ownership() -> None:
    client = _submitted_setup_client(metadata={"project_id": "project-b", "app_id": "app-b"})
    with TestClient(make_app(client)) as http:
        response = http.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 409


def test_copy_setup_reports_a_provider_outage_as_unavailable() -> None:
    client = _submitted_setup_client(
        metadata={"project_id": "project-a", "app_id": "app-a"},
        get_error=httpx.ConnectError("runtime down"),
    )
    with TestClient(make_app(client)) as http:
        response = http.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 502


def test_copy_setup_does_not_fall_back_over_a_malformed_checkpoint() -> None:
    client = _submitted_setup_client(metadata={"project_id": "project-a", "app_id": "app-a"})
    client.threads.states["t-1"]["values"]["run_config"] = "corrupted"
    with TestClient(make_app(client)) as http:
        response = http.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 409


def test_pipeline_detail_carries_the_safe_failure_reason() -> None:
    class ErroredThreads(FakeThreads):
        async def get(self, thread_id: str) -> JsonDict:
            return {
                **await super().get(thread_id),
                "status": "error",
                "error": {
                    "error": "ValueError",
                    "message": "This test is missing its persisted prompt manifest",
                },
            }

    state = {"values": {"title": "Failed launch"}, "tasks": []}
    threads = ErroredThreads({"t-1": state}, metadata={"t-1": {"project_id": "project-a"}})
    with TestClient(make_app(FakeClient(threads, FakeRuns()))) as http:
        response = http.get("/v1/pipelines/t-1")
    assert response.status_code == 200
    assert response.json()["failure"]["kind"] == "prompt_manifest_missing"
    assert "persisted prompt manifest" not in response.json()["failure"]["message"]


def test_copy_setup_refusal_names_the_next_step() -> None:
    state = {"values": {"title": "Failed launch"}, "tasks": []}
    threads = FakeThreads({"t-1": state}, metadata={"t-1": {"project_id": "project-a"}})
    with TestClient(make_app(FakeClient(threads, FakeRuns()))) as client:
        response = client.get("/v1/pipelines/t-1/setup")
    assert response.status_code == 409
    assert "Start a new test" in response.json()["title"]


@pytest.mark.parametrize(
    "error,kind,error_type",
    [
        (
            {
                "error": "ValueError",
                "message": "This test is missing its persisted prompt manifest",
            },
            "prompt_manifest_missing",
            None,
        ),
        (
            {"error": "RuntimeError", "message": "token=abc leaked"},
            "unexpected_error",
            "RuntimeError",
        ),
        ('{"error": "KeyError", "message": "x"}', "unexpected_error", "KeyError"),
        ({"error": "bad type<script>", "message": "x"}, "unexpected_error", None),
        (None, "unexpected_error", None),
    ],
)
def test_run_failure_projection_never_exposes_raw_exception_text(error, kind, error_type) -> None:
    from apex.services.pipeline_read import public_run_failure

    failure = public_run_failure({"status": "error", "error": error})
    assert failure is not None
    assert failure["kind"] == kind
    assert failure["error_type"] == error_type
    assert "leaked" not in failure["message"]
    assert public_run_failure({"status": "idle", "error": error}) is None


@pytest.mark.parametrize(
    "case,expected",
    [("valid", 202), ("stale", 409), ("invalid", 422), ("viewer", 403), ("duplicate", 409)],
)
def test_human_completion_resume_validates_task_and_authenticated_actor(case, expected):
    from apex.domain.prompt_catalog import PHASE_SECTIONS

    phase = Phase.STORY_ANALYSIS
    payload = {
        "schema_version": 1,
        "kind": "human_completion",
        "phase": phase.value,
        "task_id": "story_analysis-a1-human",
        "revision": 1,
        "actions": ["complete", "abort"],
        "sections": list(PHASE_SECTIONS[phase]),
        "created_at": "2026-10-22T00:00:00Z",
        "artifact_ids": [],
        "analysis_selections": [],
        "requires_load_test_spec": False,
        "engine": "sim",
        "approved_target": None,
    }
    state = state_with_interrupt("human-1")
    state["tasks"][0]["interrupts"][0]["value"] = payload
    if case == "duplicate":
        state["tasks"] = []
    runs = FakeRuns()
    app = make_app(
        FakeClient(FakeThreads({"t-1": state}), runs),
        role=Role.VIEWER if case == "viewer" else Role.OPERATOR,
    )
    body = {
        "action": "complete",
        "task_id": payload["task_id"],
        "expected_revision": 999 if case == "stale" else 1,
        "output": {
            "deliverable": {
                "schema_version": 1,
                "phase": phase.value,
                "disposition": "complete",
                "summary": "Completed human work",
                "sections": {key: "Completed analysis" for key in PHASE_SECTIONS[phase]},
                "evidence": [],
                "assumptions": [],
                "gaps": [],
                "blockers": [],
                "workload": [],
            }
        },
    }
    if case == "invalid":
        body["output"] = {}
    with TestClient(app) as client:
        response = client.post("/v1/pipelines/t-1/gates/human-1/resume", json=body)
    assert response.status_code == expected, response.text
    if expected == 202:
        assert runs.create_calls[0]["config"]["configurable"][RUNTIME_ACTOR_ID_KEY] == "c1"
        assert runs.create_calls[0]["command"] == {"resume": {"human-1": body}}
    else:
        assert not runs.create_calls


@pytest.mark.parametrize("owner", ["langgraph", "hitl"])
async def test_owned_phase_rerun_preserves_launch_contract_and_replays_subset(owner):
    from apex.domain.pipeline import PHASE_ORDER

    snapshot = trusted_rerun_snapshot()
    snapshot.update(
        engine="sim",
        phases=[p.value for p in PHASE_ORDER],
        phase_execution={"schema_version": 1, "owners": {p.value: owner for p in PHASE_ORDER}},
    )
    original = PipelineConfigurable.model_validate(snapshot)
    threads = FakeThreads(
        {"t-1": rerun_state(original.snapshot())},
        {"t-1": {"project_id": "project-a", "app_id": "app-a"}},
    )
    runs = DurableRerunRuns()
    service = PipelineReadService(FakeClient(threads, runs))
    await service.rerun_pipeline(
        "t-1",
        phases=["reporting"],
        gates_mode="auto",
        idempotency_key="owned-rerun",
        principal_id="consumer-a",
    )
    raw = runs.create_calls[0]["config"]["configurable"]
    cfg = PipelineConfigurable.model_validate(raw)
    assert cfg.phase_execution == original.phase_execution
    assert cfg.selected_phases() == list(PHASE_ORDER)
    assert cfg.planned_phases() == [Phase.REPORTING]
    assert cfg.agent_phases() == ([Phase.REPORTING] if owner == "langgraph" else [])
    assert cfg.environment_target == original.environment_target
    assert cfg.prompt_overrides == original.prompt_overrides
    assert cfg.analysis == original.analysis
    replay_state = {"run_config": raw, "phases_plan": ["reporting"]}
    assert (
        PipelineConfigurable.from_state_for_phase(
            replay_state, {"configurable": raw}, Phase.REPORTING
        )
        == cfg
    )
    with pytest.raises(ValueError, match="phase plan"):
        PipelineConfigurable.from_state_for_phase(
            replay_state, {"configurable": raw}, Phase.EXECUTION
        )


def test_public_launch_cannot_supply_owned_rerun_window():
    from apex.routers.pipelines import StartPipelineRequest

    with pytest.raises(ValueError, match="server-owned"):
        StartPipelineRequest(title="Bypass full plan", configurable={"rerun_phases": ["reporting"]})


def test_owned_rerun_keeps_full_frozen_prompt_choices_for_subsequent_reruns():
    from apex.domain.pipeline import PHASE_ORDER

    def entry(key, namespace, **kwargs):
        return dict(
            prompt_id=key,
            version_id=f"{key}-v1",
            key=key,
            namespace=namespace,
            version=1,
            content="Frozen reviewed instructions",
            **kwargs,
        )

    system = entry("system", "system")
    phases = {p.value: entry(p.value, "phase", phase=p.value) for p in PHASE_ORDER}
    selections = {
        "system": {k: system[k] for k in ("prompt_id", "version_id")},
        "phases": {p: {k: e[k] for k in ("prompt_id", "version_id")} for p, e in phases.items()},
    }
    cfg = PipelineConfigurable.model_validate(
        {
            "phase_execution": {
                "schema_version": 1,
                "owners": {p.value: "langgraph" for p in PHASE_ORDER},
            },
            "rerun_phases": ["reporting"],
            "prompt_selections": selections,
            "prompt_manifest": {"system": system, "phases": phases},
        }
    )
    assert cfg.prompt_manifest is not None
    assert set(cfg.prompt_manifest.phases) == set(PHASE_ORDER)
    assert cfg.agent_phases() == [Phase.REPORTING]
    assert (
        PipelineConfigurable.from_state_for_phase(
            {"run_config": cfg.snapshot(), "phases_plan": ["reporting"]},
            {"configurable": cfg.snapshot()},
            Phase.REPORTING,
        ).prompt_manifest
        == cfg.prompt_manifest
    )
