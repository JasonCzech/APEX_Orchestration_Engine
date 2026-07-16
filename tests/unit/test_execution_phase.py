"""Execution-phase engine spine: reserve/start/poll/collect against the sim engine.

All graphs compile with InMemorySaver. The connection resolver seam is pinned to
the static DEV_CONNECTIONS map (sim engine + in-memory artifact store) and the
engine_runs projection recorder is stubbed, so the suite needs no Postgres/MinIO.
Sim durations are tiny via the per-run "load_test" configurable override.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.integrations import (
    LoadTestSpec,
    ValidationReport,
)
from apex.domain.integrations import (
    TestResultSummary as EngineTestResultSummary,
)
from apex.domain.pipeline import (
    PHASE_ORDER,
    ArtifactRef,
    EngineConnectionAffinityMissingError,
    EngineHandle,
    Phase,
    PhaseResult,
    PhaseStatus,
)
from apex.graphs.pipeline import execution_phase, phase_subgraph
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.graph import builder
from apex.graphs.pipeline.state import PipelineState, merge_phase_results
from apex.ports.artifact_store import (
    StoredArtifact,
    canonical_artifact_uri,
    engine_artifact_namespace,
)
from apex.ports.execution_engine import EngineRunPhase, EngineRunStatus
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver, ResolvedAdapter

AUTO = {"prompt_review": "auto", "output_review": "auto"}
FAST_LIMITS = {"poll_interval_s": 0.02, "poll_timeout_s": 30.0}


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


@pytest.fixture(autouse=True)
def _static_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin resolution to DEV_CONNECTIONS (sim engine, memory store): no DB probing."""
    monkeypatch.setattr(execution_phase, "_make_resolver", lambda: ConnectionResolver())
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", lambda: ConnectionResolver())


@pytest.fixture(autouse=True)
def projection_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture engine_runs projection upserts instead of touching Postgres."""
    calls: list[dict[str, Any]] = []

    def record(
        thread_id: str,
        attempt: int,
        engine: str,
        handle: dict[str, Any],
        status: str,
        *,
        external_run_id: str | None = None,
        summary: dict[str, Any] | None = None,
        project_id: str | None = None,
        app_id: str | None = None,
        artifact_namespace: str | None = None,
        artifact_connection_id: str | None = None,
        artifact_connection_version: datetime | None = None,
        connection_id: str | None = None,
        connection_version: datetime | None = None,
        completion_kind: str | None = None,
        required: bool = False,
    ) -> None:
        calls.append(
            {
                "thread_id": thread_id,
                "attempt": attempt,
                "engine": engine,
                "handle": handle,
                "status": status,
                "external_run_id": external_run_id,
                "summary": summary,
                "project_id": project_id,
                "app_id": app_id,
                "artifact_namespace": artifact_namespace,
                "artifact_connection_id": artifact_connection_id,
                "artifact_connection_version": artifact_connection_version,
                "connection_id": connection_id,
                "connection_version": connection_version,
                "completion_kind": completion_kind,
                "required": required,
            }
        )

    def prepare_provision(
        thread_id: str,
        attempt: int,
        engine: str,
        handle: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        calls.append(
            {
                "thread_id": thread_id,
                "attempt": attempt,
                "engine": engine,
                "handle": handle,
                "status": EngineRunPhase.PROVISIONING.value,
                "external_run_id": None,
                "summary": None,
                "project_id": kwargs.get("project_id"),
                "app_id": kwargs.get("app_id"),
                "artifact_namespace": kwargs.get("artifact_namespace"),
                "artifact_connection_id": None,
                "connection_id": kwargs.get("connection_id"),
                "connection_version": kwargs.get("connection_version"),
                "required": True,
            }
        )
        return None

    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record)
    monkeypatch.setattr(engine_runs, "prepare_engine_provision_sync", prepare_provision)
    monkeypatch.setattr(engine_runs, "prepare_engine_start_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        engine_runs, "recover_engine_completion_sync", lambda *_args, **_kwargs: None
    )
    return calls


class EngineSpy:
    """Delegating wrapper over a real sim engine that records method calls."""

    def __init__(self, inner: SimExecutionEngine, calls: list[str]) -> None:
        self._inner = inner
        self.calls = calls
        self._apex_resolved_connection_id: str | None = None
        self._apex_resolved_connection_version: datetime | None = None

    async def validate(self, spec: Any) -> Any:
        self.calls.append("validate")
        return await self._inner.validate(spec)

    async def provision(self, spec: Any) -> Any:
        self.calls.append("provision")
        return await self._inner.provision(spec)

    async def start(self, handle: Any) -> None:
        self.calls.append("start")
        return await self._inner.start(handle)

    async def get_status(self, handle: Any) -> Any:
        self.calls.append("get_status")
        return await self._inner.get_status(handle)

    async def abort(self, handle: Any, *, reason: str) -> None:
        self.calls.append("abort")
        return await self._inner.abort(handle, reason=reason)

    async def collect_artifacts(self, handle: Any, store: Any) -> Any:
        self.calls.append("collect_artifacts")
        return await self._inner.collect_artifacts(handle, store)

    async def fetch_summary(self, handle: Any) -> Any:
        self.calls.append("fetch_summary")
        return await self._inner.fetch_summary(handle)

    async def teardown(self, handle: Any) -> None:
        self.calls.append("teardown")
        return await self._inner.teardown(handle)


def install_engine_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the engine-resolution seam to spy-wrapped sim engines.

    The wrapped engine is still built from the node-supplied engine_options, so
    the configurable "load_test" option flow (e.g. fail_at_pct) stays under test.
    """
    calls: list[str] = []

    async def resolve(
        cfg: Any,
        engine_options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> EngineSpy:
        conn = ConnectionConfig(
            id=connection_id or "spy-engine",
            kind=PortKind.EXECUTION_ENGINE,
            provider="sim",
            name="Spy sim engine",
            options=dict(engine_options),
        )
        spy = EngineSpy(SimExecutionEngine(conn), calls)
        spy._apex_resolved_connection_id = conn.id
        return spy

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    return calls


def test_runtime_resolution_does_not_invoke_polymorphic_connection_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks: list[str] = []

    class HostileId(str):
        def __eq__(self, _other: object) -> bool:
            hooks.append("eq")
            return True

        def __ne__(self, _other: object) -> bool:
            hooks.append("ne")
            return False

    async def resolve(*_args: Any, **_kwargs: Any) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=object(),
            connection_id=HostileId("engine-a"),
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="engine-a",
        idempotency_key="runtime-affinity",
    )

    with pytest.raises(RuntimeError, match="invalid connection id"):
        asyncio.run(
            execution_phase._resolve_engine_for_runtime_io(
                PipelineConfigurable(engine="sim"),
                {},
                {},
                handle,
                connection_id="engine-a",
                connection_version=None,
            )
        )

    assert hooks == []


# ── helpers ───────────────────────────────────────────────────────────────────


def compiled() -> CompiledStateGraph[Any, Any, Any, Any]:
    return builder.compile(checkpointer=InMemorySaver())


def exec_config(
    thread_id: str,
    *,
    load_test: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
    phases: list[str] | None = None,
) -> RunnableConfig:
    selected = phases or [
        "story_analysis",
        "test_planning",
        "script_scenario",
        "execution",
    ]
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "phases": selected,
        "gates": {
            **{phase: dict(AUTO) for phase in selected},
            **(gates or {}),
        },
        "limits": {**FAST_LIMITS, **(limits or {})},
        "load_test": {"duration_s": 0.1, **(load_test or {})},
    }
    # Sizing rule: ceil(poll_timeout/poll_interval) + spine + headroom; tiny test
    # intervals would blow LangGraph's default of 25 (see execution_phase docstring).
    return {"configurable": configurable, "recursion_limit": 150}


def seeded_inputs(duration_s: float = 0.25, **spec_extra: Any) -> dict[str, Any]:
    """Run inputs with a succeeded script_scenario entry carrying a LoadTestSpec."""
    entry = PhaseResult(
        phase=Phase.SCRIPT_SCENARIO, status=PhaseStatus.SUCCEEDED, attempt=1
    ).as_state()
    entry["load_test_spec"] = {
        "idempotency_key": "seeded-key-to-be-overridden",
        "title": "seeded load test",
        "vusers": 5,
        "ramp_s": 0.1,
        "duration_s": duration_s,
        **spec_extra,
    }
    return {
        "title": "Demo",
        "request": "load test the checkout flow",
        "phase_results": {"script_scenario": entry},
    }


def public_inputs() -> dict[str, str]:
    return {"title": "Demo", "request": "load test the checkout flow"}


def pending_interrupt(result: dict[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__")
    assert interrupts, f"expected a pending interrupt, got keys {sorted(result)}"
    return interrupts[0].value


def subgraph_values(
    g: CompiledStateGraph[Any, Any, Any, Any], cfg: RunnableConfig
) -> dict[str, Any]:
    task_state = g.get_state(cfg, subgraphs=True).tasks[0].state
    assert isinstance(task_state, StateSnapshot)
    return task_state.values


def custom_events(
    g: CompiledStateGraph[Any, Any, Any, Any], inputs: dict[str, Any], cfg: RunnableConfig
) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], event)
        for _ns, event in g.stream(inputs, cfg, stream_mode="custom", subgraphs=True)
    ]


def checkpoint_command_update(
    state: PipelineState,
    entry: dict[str, Any],
    command: Command[str],
) -> None:
    """Apply one direct-node command as LangGraph's checkpoint reducer would."""

    assert command.update is not None
    entry.update(command.update["phase_results"]["execution"])
    if "artifacts" in command.update:
        state["artifacts"] = command.update["artifacts"]


def index_checkpointed_collection(
    state: PipelineState,
    entry: dict[str, Any],
    collected: Command[str],
    config: RunnableConfig,
) -> Command[str]:
    assert collected.goto == "engine_collection_index"
    checkpoint_command_update(state, entry, collected)
    indexed = execution_phase.engine_collection_index(state, config)
    assert indexed.goto == "engine_collection_settle"
    checkpoint_command_update(state, entry, indexed)
    return indexed


# ── tests ─────────────────────────────────────────────────────────────────────


def test_e2e_all_auto_full_pipeline_runs_engine_and_reports(
    projection_calls: list[dict[str, Any]],
) -> None:
    """Full 7-phase run: script_scenario emits the spec, execution drives the sim
    engine through provision/poll/collect, reporting mentions the KPIs."""
    g = compiled()
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": "exec-e2e",
            "gates": {phase.value: dict(AUTO) for phase in PHASE_ORDER},
            "limits": dict(FAST_LIMITS),
            "load_test": {"duration_s": 0.2, "vusers": 4},
        },
        "recursion_limit": 150,
    }
    result = g.invoke({"title": "Demo", "request": "Load test the checkout flow"}, cfg)
    assert "__interrupt__" not in result

    spec = result["phase_results"]["script_scenario"]["load_test_spec"]
    assert spec["idempotency_key"] == "exec-e2e-execution-a1"
    assert spec["duration_s"] == 0.2
    assert spec["vusers"] == 4
    assert spec["script_refs"] == []

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "succeeded"
    assert entry["attempt"] == 1
    assert entry["load_test_spec"]["idempotency_key"] == "exec-e2e-execution-a1"
    assert entry["engine_poll_count"] >= 1
    assert entry["engine_poll_last"]["status"] == "completed"

    handle = result["engine_handle"]
    assert handle["engine"] == "sim"
    assert handle["external_run_id"].startswith("sim-")
    assert handle["idempotency_key"] == "exec-e2e-execution-a1"
    assert entry["engine_handle"] == handle

    summary = entry["test_summary"]
    assert summary["passed"] is True
    assert set(summary["kpis"]) == {"tps_avg", "p95_ms", "error_rate", "vusers_peak"}
    assert summary["kpis"]["vusers_peak"] == 4.0
    assert "Engine run" in entry["summary"] and "KPIs" in entry["summary"]

    engine_artifacts = [a for a in result["artifacts"] if a["kind"] == "engine_results"]
    assert len(engine_artifacts) == 1
    assert engine_artifacts[0]["id"] == "execution-a1-engine-artifact-0"
    assert engine_artifacts[0]["id"] in entry["artifact_ids"]
    assert engine_artifacts[0]["key"].startswith(
        engine_artifact_namespace(handle["idempotency_key"])
    )

    reporting = result["phase_results"]["reporting"]["summary"]
    assert "KPIs:" in reporting and "tps_avg" in reporting and "passed" in reporting

    statuses = [c["status"] for c in projection_calls]
    assert statuses == [
        "provisioning",
        "ready",
        "running",
        "collecting",  # nonterminal execution/artifact-store lease during collection
        "completed",
    ]
    assert projection_calls[-2]["artifact_connection_id"] == "dev-artifact-store-memory"
    assert projection_calls[-2]["connection_id"] == "dev-engine-sim"
    assert projection_calls[-2]["required"] is True
    assert projection_calls[-1]["external_run_id"] == handle["external_run_id"]
    assert projection_calls[-1]["summary"] == summary


def test_engine_artifacts_are_batch_indexed_before_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: list[dict[str, Any]] = []

    async def record(references: Any, **affinity: Any) -> None:
        batches.append({"references": list(references), **affinity})

    monkeypatch.setattr(execution_phase, "record_artifact_references", record)

    result = compiled().invoke(public_inputs(), exec_config("exec-artifact-index"))

    engine_artifacts = [
        artifact for artifact in result["artifacts"] if artifact["kind"] == "engine_results"
    ]
    assert len(engine_artifacts) == 1
    assert len(batches) == 1
    batch = batches[0]
    assert batch["connection_id"] == "dev-artifact-store-memory"
    assert batch["thread_id"] == "exec-artifact-index"
    assert batch["project_id"] is None
    assert batch["app_id"] is None
    assert [(ref.artifact_key, ref.kind) for ref in batch["references"]] == [
        (engine_artifacts[0]["key"], "engine_results")
    ]


def test_failed_engine_artifact_index_blocks_after_checkpointed_exact_retries(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    index_calls = 0
    provider_calls = install_engine_spy(monkeypatch)

    async def fail_index(*args: Any, **kwargs: Any) -> None:
        nonlocal index_calls
        index_calls += 1
        raise RuntimeError("artifact index unavailable")

    monkeypatch.setattr(execution_phase, "record_artifact_references", fail_index)
    g = compiled()
    cfg = exec_config("exec-artifact-index-fails")

    result = g.invoke(public_inputs(), cfg)
    assert result["__interrupt__"]

    assert index_calls == execution_phase.MAX_ENGINE_COLLECTION_ATTEMPTS
    assert provider_calls.count("collect_artifacts") == 1
    assert provider_calls.count("fetch_summary") == 1
    assert [call["status"] for call in projection_calls].count("collecting") == 1
    snapshot = subgraph_values(g, cfg)
    entry = snapshot["phase_results"]["execution"]
    assert entry["engine_collection_required"] is True
    assert entry["engine_collection_blocked"] is True
    assert entry["engine_collection_failures"] == execution_phase.MAX_ENGINE_COLLECTION_ATTEMPTS
    assert not any(
        artifact.get("kind") in {"engine_results", "engine_report"}
        for artifact in snapshot.get("artifacts", [])
    )
    # Do not delete after an ambiguous commit outcome. The deterministic object is
    # retained for the next exact-affinity batch retry, but this failed superstep
    # never checkpoints a graph-visible ArtifactRef.
    key = f"{engine_artifact_namespace('exec-artifact-index-fails-execution-a1')}/artifact-0000"
    assert asyncio.run(MemoryArtifactStore().get(key))


def test_ambiguous_engine_artifact_index_retry_never_recollects_or_rewrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = install_engine_spy(monkeypatch)
    batches: list[list[tuple[str, str]]] = []

    async def ambiguous_then_succeed(references: Any, **_affinity: Any) -> None:
        batch = [(reference.artifact_key, reference.kind) for reference in references]
        batches.append(batch)
        if len(batches) == 1:
            # Model a committed ownership transaction whose acknowledgement was
            # lost. The next node attempt must replay this exact staged batch.
            raise RuntimeError("ownership commit acknowledgement lost")

    monkeypatch.setattr(
        execution_phase,
        "record_artifact_references",
        ambiguous_then_succeed,
    )
    thread_id = "exec-artifact-index-ambiguous"

    result = compiled().invoke(public_inputs(), exec_config(thread_id))

    assert "__interrupt__" not in result
    assert len(batches) == 2
    assert batches[0] == batches[1]
    assert provider_calls.count("collect_artifacts") == 1
    assert provider_calls.count("fetch_summary") == 1
    assert provider_calls.count("teardown") == 1
    [(key, kind)] = batches[0]
    assert kind == "engine_results"
    assert asyncio.run(MemoryArtifactStore().get(key))


def test_collection_retry_defers_destructive_teardown_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    provider_results_available = True

    class RetryCollector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            nonlocal provider_results_available
            calls.append("collect")
            if not provider_results_available:
                raise RuntimeError("provider results were erased by teardown")
            if calls.count("collect") == 1:
                raise RuntimeError("transient result download failure")
            return []

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(engine="loadrunner", passed=True)

        async def teardown(self, handle: Any) -> None:
            nonlocal provider_results_available
            calls.append("teardown")
            provider_results_available = False

    async def resolve(*args: Any, **kwargs: Any) -> RetryCollector:
        return RetryCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="lre-a",
        external_run_id="lre-42",
        idempotency_key="retry-collect-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_poll_last": {"status": "completed"},
        "artifact_store_connection_id": "dev-artifact-store-memory",
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )

    first = execution_phase.engine_collect(state, exec_config("retry-collect"))
    assert first.goto == "engine_collect"
    assert calls == ["collect"]
    assert provider_results_available is True
    assert first.update is not None
    entry.update(first.update["phase_results"]["execution"])

    config = exec_config("retry-collect")
    second = execution_phase.engine_collect(state, config)
    indexed = index_checkpointed_collection(state, entry, second, config)
    assert indexed.goto == "engine_collection_settle"
    assert calls == ["collect", "collect", "summary"]
    assert provider_results_available is True

    settled = execution_phase.engine_collection_settle(state, config)
    assert settled.goto == "open_output_gate"
    assert calls == ["collect", "collect", "summary", "teardown"]
    assert provider_results_available is False


def test_collection_store_view_blocks_out_of_namespace_plugin_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim_key = "shared/victim.json"
    asyncio.run(MemoryArtifactStore().put(victim_key, b"original", content_type="application/json"))
    calls: list[str] = []

    class EscapingCollector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            calls.append("collect")
            await store.put(victim_key, b"overwritten", content_type="application/json")
            return []

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(engine="sim", passed=True)

    async def resolve(*_args: Any, **_kwargs: Any) -> EscapingCollector:
        return EscapingCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="namespace-escape-remote",
        idempotency_key="namespace-escape-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("namespace-escape"))

    assert command.goto == "engine_collect"
    assert command.update is not None
    error = command.update["phase_results"]["execution"]["engine_collection_last_error"]
    assert "must remain beneath" in error
    assert calls == ["collect"]
    assert asyncio.run(MemoryArtifactStore().get(victim_key)) == b"original"


def test_collection_store_view_denies_buffered_provider_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="read-denied-remote",
        idempotency_key="read-denied-execution-a1",
    )
    existing_key = f"{engine_artifact_namespace(handle.idempotency_key)}/existing.bin"
    asyncio.run(
        MemoryArtifactStore().put(
            existing_key,
            b"existing provider output",
            content_type="application/octet-stream",
        )
    )
    calls: list[str] = []

    class ReadingCollector:
        async def collect_artifacts(self, provider_handle: Any, store: Any) -> list[Any]:
            calls.append("collect")
            await store.get(existing_key)
            return []

        async def fetch_summary(self, provider_handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(engine="sim", passed=True)

    async def resolve(*_args: Any, **_kwargs: Any) -> ReadingCollector:
        return ReadingCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("read-denied"))

    assert command.goto == "engine_collect"
    assert command.update is not None
    error = command.update["phase_results"]["execution"]["engine_collection_last_error"]
    assert "write-only" in error
    assert calls == ["collect"]


@pytest.mark.parametrize(
    ("returned_uri", "returned_media_type", "expected_error"),
    [
        ("javascript:alert(1)", "application/json", "URI does not match"),
        (None, "text/html", "media type does not match"),
    ],
)
def test_collection_rejects_refs_that_do_not_match_scoped_store_writes(
    monkeypatch: pytest.MonkeyPatch,
    returned_uri: str | None,
    returned_media_type: str,
    expected_error: str,
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="mismatched-ref-remote",
        idempotency_key="mismatched-ref-execution-a1",
    )
    artifact_key = f"{engine_artifact_namespace(handle.idempotency_key)}/results.json"
    calls: list[str] = []

    class MismatchedRefCollector:
        async def collect_artifacts(self, provider_handle: Any, store: Any) -> list[Any]:
            calls.append("collect")
            stored = await store.put(
                artifact_key,
                b"{}",
                content_type="application/json",
            )
            return [
                {
                    "kind": "engine_results",
                    "name": "results",
                    "uri": returned_uri or stored.uri,
                    "key": stored.key,
                    "media_type": returned_media_type,
                }
            ]

        async def fetch_summary(self, provider_handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(engine="sim", passed=True)

    async def resolve(*_args: Any, **_kwargs: Any) -> MismatchedRefCollector:
        return MismatchedRefCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("mismatched-ref"))

    assert command.goto == "engine_collect"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert expected_error in entry["engine_collection_last_error"]
    assert "engine_artifacts" not in entry
    assert "test_summary" not in entry
    assert calls == ["collect"]


def test_collection_isolates_summary_handle_from_collector_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="trusted-collection-run",
        idempotency_key="trusted-collection-execution-a1",
        extras={"provider_marker": "trusted"},
    )
    summary_handles: list[EngineHandle] = []

    class MutatingCollector:
        async def collect_artifacts(self, provider_handle: EngineHandle, store: Any) -> list[Any]:
            provider_handle.engine = "attacker-engine"
            provider_handle.connection_id = "attacker-connection"
            provider_handle.external_run_id = "attacker-run"
            provider_handle.idempotency_key = "attacker-key"
            provider_handle.extras["provider_marker"] = "attacker"
            return []

        async def fetch_summary(self, provider_handle: EngineHandle) -> EngineTestResultSummary:
            summary_handles.append(provider_handle.model_copy(deep=True))
            return EngineTestResultSummary(engine="sim", passed=True)

    async def resolve(*_args: Any, **_kwargs: Any) -> MutatingCollector:
        return MutatingCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("trusted-collection"))

    assert command.goto == "engine_collection_index"
    assert len(summary_handles) == 1
    assert summary_handles[0] == handle


def test_collection_rejects_summary_from_a_different_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="summary-provider-run",
        idempotency_key="summary-provider-execution-a1",
    )

    class WrongProviderSummaryCollector:
        async def collect_artifacts(self, provider_handle: Any, store: Any) -> list[Any]:
            return []

        async def fetch_summary(self, provider_handle: Any) -> EngineTestResultSummary:
            return EngineTestResultSummary(engine="loadrunner", passed=True)

    async def resolve(*_args: Any, **_kwargs: Any) -> WrongProviderSummaryCollector:
        return WrongProviderSummaryCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("summary-provider"))

    assert command.goto == "engine_collect"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert "summary provider does not match" in entry["engine_collection_last_error"]
    assert "test_summary" not in entry


def _staged_completed_collection_state(thread_id: str) -> tuple[PipelineState, dict[str, Any]]:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id=f"{thread_id}-remote",
        idempotency_key=f"{thread_id}-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_collection_staged": True,
        "engine_collection_projected_phase": EngineRunPhase.COMPLETED.value,
        "engine_collection_final_status": None,
        "engine_collection_next": "open_output_gate",
        "test_summary": EngineTestResultSummary(engine="sim", passed=True).model_dump(mode="json"),
        "artifact_store_connection_id": "dev-artifact-store-memory",
    }
    return (
        cast(
            PipelineState,
            {
                "engine_handle": handle.model_dump(mode="json"),
                "phase_results": {"execution": entry},
            },
        ),
        entry,
    )


def test_collection_settle_retries_teardown_before_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teardown_calls = 0
    projection_statuses: list[str] = []

    class TransientTeardownFailure:
        async def teardown(self, handle: Any) -> None:
            nonlocal teardown_calls
            teardown_calls += 1
            if teardown_calls < execution_phase.MAX_ENGINE_SETTLE_ATTEMPTS:
                raise OSError("provider cleanup unavailable")

    async def resolve(*_args: Any, **_kwargs: Any) -> TransientTeardownFailure:
        return TransientTeardownFailure()

    def record_projection(
        _thread_id: str,
        _attempt: int,
        _engine: str,
        _handle: dict[str, Any],
        status: str,
        **_kwargs: Any,
    ) -> None:
        projection_statuses.append(status)

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record_projection)
    state, entry = _staged_completed_collection_state("settle-retry")
    config = exec_config("settle-retry")

    for expected_failure in range(1, execution_phase.MAX_ENGINE_SETTLE_ATTEMPTS):
        command = execution_phase.engine_collection_settle(state, config)
        assert command.goto == "engine_collection_settle"
        assert command.update is not None
        update = command.update["phase_results"]["execution"]
        assert update["engine_collection_settle_required"] is True
        assert update["engine_collection_settle_failures"] == expected_failure
        assert update["status"] == PhaseStatus.RUNNING.value
        entry.update(update)
        assert projection_statuses == []

    settled = execution_phase.engine_collection_settle(state, config)

    assert settled.goto == "open_output_gate"
    assert settled.update is not None
    settled_entry = settled.update["phase_results"]["execution"]
    assert settled_entry["engine_collection_staged"] is False
    assert settled_entry["engine_collection_settle_required"] is False
    assert settled_entry["engine_collection_settle_failures"] == 0
    assert teardown_calls == execution_phase.MAX_ENGINE_SETTLE_ATTEMPTS
    assert projection_statuses == [EngineRunPhase.COMPLETED.value]


def test_collection_settle_blocks_with_staged_results_and_lease_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection_statuses: list[str] = []

    class PermanentTeardownFailure:
        async def teardown(self, handle: Any) -> None:
            raise OSError("provider cleanup unavailable")

    async def resolve(*_args: Any, **_kwargs: Any) -> PermanentTeardownFailure:
        return PermanentTeardownFailure()

    def record_projection(
        _thread_id: str,
        _attempt: int,
        _engine: str,
        _handle: dict[str, Any],
        status: str,
        **_kwargs: Any,
    ) -> None:
        projection_statuses.append(status)

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record_projection)
    state, entry = _staged_completed_collection_state("settle-blocked")
    config = exec_config("settle-blocked")

    command: Command[str] | None = None
    for _ in range(execution_phase.MAX_ENGINE_SETTLE_ATTEMPTS):
        command = execution_phase.engine_collection_settle(state, config)
        assert command.update is not None
        entry.update(command.update["phase_results"]["execution"])

    assert command is not None
    assert command.goto == "engine_collection_settle_blocked"
    assert entry["engine_collection_staged"] is True
    assert entry["engine_collection_settle_required"] is True
    assert entry["engine_collection_settle_blocked"] is True
    assert projection_statuses == []
    assert execution_phase.route_execution_entry(state) == "engine_collection_settle_resume"

    resumed = execution_phase.engine_collection_settle_resume(state, config)

    assert resumed.goto == "engine_collection_settle"
    assert resumed.update is not None
    resumed_entry = resumed.update["phase_results"]["execution"]
    assert resumed_entry["engine_collection_settle_failures"] == 0
    assert resumed_entry["engine_collection_settle_blocked"] is False


def test_aborted_engine_collection_sets_top_level_run_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AbortedCollector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            return []

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            return EngineTestResultSummary(engine="sim", passed=False)

        async def teardown(self, handle: Any) -> None:
            return None

    async def resolve(*_args: Any, **_kwargs: Any) -> AbortedCollector:
        return AbortedCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-aborted",
        idempotency_key="aborted-collect-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": "aborted"},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    config = exec_config("aborted-collect")
    phase_results = state.get("phase_results")
    assert phase_results is not None
    entry = phase_results["execution"]
    collected = execution_phase.engine_collect(state, config)
    staged = index_checkpointed_collection(state, entry, collected, config)
    assert staged.update is not None
    staged_entry = staged.update["phase_results"]["execution"]
    assert staged_entry["status"] == "running"
    assert staged_entry["engine_collection_final_status"] == "aborted"
    assert "run_aborted" not in staged.update
    settled = execution_phase.engine_collection_settle(state, config)
    assert settled.goto == "finalize"
    assert settled.update is not None
    entry.update(settled.update["phase_results"]["execution"])

    finalized = phase_subgraph._make_finalize(Phase.EXECUTION)(state, config)
    assert finalized["run_aborted"] is True
    assert finalized["phase_results"]["execution"]["status"] == "aborted"
    assert finalized["phase_results"]["execution"]["engine_collection_settled"] is False


def test_collection_checkpoints_artifact_store_affinity_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_resolution_calls: list[str | None] = []
    provider_calls: list[str] = []
    default_connection_id = "artifact-store-a"

    class Collector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            provider_calls.append("collect")
            return []

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            provider_calls.append("summary")
            return EngineTestResultSummary(engine="loadrunner", passed=True)

        async def teardown(self, handle: Any) -> None:
            provider_calls.append("teardown")

    async def resolve_engine(*args: Any, **kwargs: Any) -> Collector:
        return Collector()

    async def resolve_store(
        cfg: PipelineConfigurable, *, connection_id: str | None = None
    ) -> ResolvedAdapter:
        del cfg
        store_resolution_calls.append(connection_id)
        return ResolvedAdapter(
            adapter=MemoryArtifactStore(),
            connection_id=connection_id or default_connection_id,
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve_engine)
    monkeypatch.setattr(execution_phase, "_resolve_artifact_store", resolve_store)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="lre-a",
        external_run_id="lre-42",
        idempotency_key="pin-collect-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_poll_last": {"status": "completed"},
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )

    reservation = execution_phase.engine_collect(state, exec_config("pin-collect"))

    assert reservation.goto == "engine_collect"
    assert provider_calls == []
    assert reservation.update is not None
    reservation_entry = reservation.update["phase_results"]["execution"]
    assert reservation_entry["artifact_store_connection_id"] == "artifact-store-a"
    entry.update(reservation_entry)

    # Simulate a project/global default changing after the affinity checkpoint.
    default_connection_id = "artifact-store-b"
    config = exec_config("pin-collect")
    collected = execution_phase.engine_collect(state, config)

    assert collected.goto == "engine_collection_index"
    assert store_resolution_calls == [None, "artifact-store-a"]
    assert provider_calls == ["collect", "summary"]
    indexed = index_checkpointed_collection(state, entry, collected, config)
    assert indexed.goto == "engine_collection_settle"

    settled = execution_phase.engine_collection_settle(state, config)
    assert settled.goto == "open_output_gate"
    assert provider_calls == ["collect", "summary", "teardown"]


@pytest.mark.parametrize(
    (
        "provider_phase",
        "summary_passed",
        "projected_phase",
        "next_node",
        "final_status",
    ),
    [
        (
            EngineRunPhase.COMPLETED,
            True,
            EngineRunPhase.COMPLETED,
            "open_output_gate",
            PhaseStatus.SUCCEEDED,
        ),
        (
            EngineRunPhase.FAILED,
            False,
            EngineRunPhase.FAILED,
            "finalize",
            PhaseStatus.FAILED,
        ),
        (
            EngineRunPhase.ABORTED,
            False,
            EngineRunPhase.ABORTED,
            "finalize",
            PhaseStatus.ABORTED,
        ),
    ],
)
def test_collection_terminal_projection_response_loss_replays_from_staged_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    provider_phase: EngineRunPhase,
    summary_passed: bool,
    projected_phase: EngineRunPhase,
    next_node: str,
    final_status: PhaseStatus,
) -> None:
    calls: list[str] = []
    projection_statuses: list[str] = []
    terminal_projection_attempts = 0

    class CrashWindowCollector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            calls.append("collect")
            key = f"{engine_artifact_namespace(handle.idempotency_key)}/results.json"
            stored = await store.put(key, b"{}", content_type="application/json")
            return [
                {
                    "kind": "engine_results",
                    "name": "results.json",
                    "uri": stored.uri,
                    "key": stored.key,
                    "media_type": "application/json",
                }
            ]

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(
                engine="sim",
                passed=summary_passed,
                sla_breaches=[] if summary_passed else ["SLA failed"],
            )

        async def teardown(self, handle: Any) -> None:
            calls.append("teardown")

    async def resolve(*_args: Any, **_kwargs: Any) -> CrashWindowCollector:
        return CrashWindowCollector()

    def record_projection(
        _thread_id: str,
        _attempt: int,
        _engine: str,
        _handle: dict[str, Any],
        status: str,
        **_kwargs: Any,
    ) -> None:
        nonlocal terminal_projection_attempts
        projection_statuses.append(status)
        if status == projected_phase.value:
            terminal_projection_attempts += 1
            if terminal_projection_attempts == 1:
                # Model a DB commit whose acknowledgement is lost. The graph node
                # raises before its own checkpoint even though the projection won.
                raise RuntimeError("terminal projection response lost after commit")

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record_projection)
    thread_id = f"settle-replay-{provider_phase.value}"
    config = exec_config(thread_id)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id=f"sim-{provider_phase.value}",
        idempotency_key=f"{thread_id}-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_poll_last": {"status": provider_phase.value},
        "artifact_store_connection_id": "dev-artifact-store-memory",
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )

    collected = execution_phase.engine_collect(state, config)
    staged = index_checkpointed_collection(state, entry, collected, config)
    assert staged.goto == "engine_collection_settle"
    assert staged.update is not None
    staged_entry = staged.update["phase_results"]["execution"]
    assert staged_entry["status"] == PhaseStatus.RUNNING.value
    assert staged_entry["test_summary"]["passed"] is summary_passed
    assert staged_entry["artifact_ids"] == ["execution-a1-engine-artifact-0"]
    assert calls == ["collect", "summary"]
    assert execution_phase.route_execution_entry(state) == "engine_collection_settle"

    with pytest.raises(RuntimeError, match="response lost after commit"):
        execution_phase.engine_collection_settle(state, config)
    assert calls == ["collect", "summary", "teardown"]
    assert execution_phase.route_execution_entry(state) == "engine_collection_settle"

    settled = execution_phase.engine_collection_settle(state, config)
    assert settled.goto == next_node
    assert settled.update is not None
    assert calls == ["collect", "summary", "teardown", "teardown"]
    assert projection_statuses == [
        EngineRunPhase.COLLECTING.value,
        projected_phase.value,
        projected_phase.value,
    ]
    entry.update(settled.update["phase_results"]["execution"])
    assert entry["engine_collection_staged"] is False
    assert entry["engine_collection_settled"] is True
    assert execution_phase.route_execution_entry(state) == next_node

    if next_node == "open_output_gate":
        opened = phase_subgraph._make_open_output_gate(Phase.EXECUTION)(state, config)
        assert opened == {}
        gate = phase_subgraph._make_output_gate(Phase.EXECUTION)(state, config)
        assert gate.goto == "finalize"

    finalized = phase_subgraph._make_finalize(Phase.EXECUTION)(state, config)
    final_entry = finalized["phase_results"]["execution"]
    assert final_entry["status"] == final_status.value
    assert final_entry["engine_collection_settled"] is False
    assert final_entry["engine_collection_final_status"] is None
    assert final_entry["engine_collection_next"] is None
    assert finalized.get("run_aborted", False) is (final_status is PhaseStatus.ABORTED)


def test_collection_settle_recovers_exact_terminal_witness_after_connection_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_version = datetime.now().astimezone()
    artifact_version = datetime.now().astimezone()
    calls: list[str] = []

    class Adapter:
        async def teardown(self, provider_handle: EngineHandle) -> None:
            calls.append("teardown")

    async def resolve(*_args: Any, **_kwargs: Any) -> Adapter:
        calls.append("resolve")
        return Adapter()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(
        engine_runs, "recover_engine_completion_sync", lambda *_args, **_kwargs: None
    )

    def committed_projection(*_args: Any, **kwargs: Any) -> None:
        calls.append("terminal-commit")
        assert kwargs["connection_version"] == execution_version
        assert kwargs["artifact_connection_version"] == artifact_version
        assert kwargs["completion_kind"] == engine_runs.COMPLETION_COLLECTION_TEARDOWN
        raise RuntimeError("terminal commit acknowledgement lost")

    monkeypatch.setattr(engine_runs, "record_engine_run_sync", committed_projection)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="engine-a",
        external_run_id="lre-42",
        idempotency_key="settle-witness-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_connection_id": "engine-a",
                    "engine_connection_version": execution_version.isoformat(),
                    "engine_connection_persisted": True,
                    "engine_connection_affinity_staged": True,
                    "artifact_store_connection_id": "artifact-a",
                    "artifact_store_connection_version": artifact_version.isoformat(),
                    "artifact_store_connection_persisted": True,
                    "engine_collection_staged": True,
                    "engine_collection_projected_phase": EngineRunPhase.COMPLETED.value,
                    "engine_collection_final_status": None,
                    "engine_collection_next": "open_output_gate",
                    "test_summary": EngineTestResultSummary(
                        engine="loadrunner", passed=True
                    ).model_dump(mode="json"),
                }
            },
        },
    )
    config = exec_config("settle-witness")

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        execution_phase.engine_collection_settle(state, config)
    assert calls == ["resolve", "teardown", "terminal-commit"]

    def recover(*_args: Any, **kwargs: Any) -> str:
        calls.append("recover-terminal")
        assert kwargs["connection_version"] == execution_version
        assert kwargs["artifact_connection_version"] == artifact_version
        assert kwargs["expected_statuses"] == frozenset({"completed"})
        return "completed"

    async def deleted_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("released execution connection must not be resolved on replay")

    monkeypatch.setattr(engine_runs, "recover_engine_completion_sync", recover)
    monkeypatch.setattr(execution_phase, "_resolve_engine", deleted_connection)

    recovered = execution_phase.engine_collection_settle(state, config)

    assert recovered.goto == "open_output_gate"
    assert calls[-1] == "recover-terminal"


def test_cleanup_recovers_exact_terminal_witness_after_connection_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_version = datetime.now().astimezone()
    calls: list[str] = []

    class Adapter:
        async def abort(self, provider_handle: EngineHandle, *, reason: str) -> None:
            calls.append("abort")

        async def get_status(self, provider_handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.ABORTED)

        async def teardown(self, provider_handle: EngineHandle) -> None:
            calls.append("teardown")

    async def resolve(*_args: Any, **_kwargs: Any) -> Adapter:
        calls.append("resolve")
        return Adapter()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(
        engine_runs, "recover_engine_completion_sync", lambda *_args, **_kwargs: None
    )

    def committed_projection(*_args: Any, **kwargs: Any) -> None:
        calls.append("terminal-commit")
        assert kwargs["connection_version"] == execution_version
        assert kwargs["completion_kind"] == engine_runs.COMPLETION_CLEANUP_TEARDOWN
        raise RuntimeError("cleanup terminal commit acknowledgement lost")

    monkeypatch.setattr(engine_runs, "record_engine_run_sync", committed_projection)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="engine-a",
        external_run_id="lre-42",
        idempotency_key="cleanup-witness-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_connection_id": "engine-a",
                    "engine_connection_version": execution_version.isoformat(),
                    "engine_connection_persisted": True,
                    "engine_connection_affinity_staged": True,
                    "engine_cleanup_required": True,
                    "engine_cleanup_reason": "timeout",
                    "engine_cleanup_final_error": "timeout",
                }
            },
        },
    )
    config = exec_config("cleanup-witness")

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        execution_phase.engine_cleanup(state, config)
    assert calls == ["resolve", "abort", "status", "teardown", "terminal-commit"]

    def recover(*_args: Any, **kwargs: Any) -> str:
        calls.append("recover-terminal")
        assert kwargs["connection_version"] == execution_version
        assert kwargs["expected_statuses"] == frozenset(execution_phase.TERMINAL_ENGINE_PHASES)
        return "aborted"

    async def deleted_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("released execution connection must not be resolved on replay")

    monkeypatch.setattr(engine_runs, "recover_engine_completion_sync", recover)
    monkeypatch.setattr(execution_phase, "_resolve_engine", deleted_connection)

    recovered = execution_phase.engine_cleanup(state, config)

    assert recovered.goto == "finalize"
    assert calls[-1] == "recover-terminal"


@pytest.mark.parametrize(
    "ref",
    [
        {
            "kind": "transcript",
            "name": "results.json",
            "uri": "memory://engine-runs/x/results.json",
            "key": "engine-runs/x/results.json",
        },
        {
            "kind": "engine_results",
            "name": "results.json",
            "uri": "memory://other/results.json",
            "key": "other/results.json",
        },
    ],
)
def test_engine_artifact_validation_rejects_wrong_kind_or_namespace(
    ref: dict[str, Any],
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-validation",
        idempotency_key="validation-execution-a1",
    )

    with pytest.raises(ValueError):
        execution_phase._validated_engine_artifacts([ref], handle)


def test_engine_artifact_validation_caps_reference_count() -> None:
    handle = EngineHandle(
        engine="sim",
        idempotency_key="artifact-count-execution-a1",
    )

    with pytest.raises(ValueError, match="artifact refs; limit"):
        execution_phase._validated_engine_artifacts(
            [{}] * (execution_phase.MAX_ENGINE_ARTIFACT_REFS + 1),
            handle,
        )


@pytest.mark.parametrize("server_field", ["id", "artifact_connection_id", "created_at"])
@pytest.mark.parametrize("as_model", [False, True])
def test_engine_artifact_validation_discards_provider_owned_server_fields(
    server_field: str,
    as_model: bool,
) -> None:
    handle = EngineHandle(
        engine="sim",
        idempotency_key="artifact-server-fields-execution-a1",
    )
    key = f"{engine_artifact_namespace(handle.idempotency_key)}/result.json"
    canary = f"Authorization: Bearer provider-{server_field}-secret"
    raw: dict[str, Any] = {
        "kind": "engine_results",
        "name": "results",
        "uri": canonical_artifact_uri(key),
        "key": key,
        "media_type": "application/json",
        server_field: canary,
    }
    provider_ref: Any = ArtifactRef.model_construct(**raw) if as_model else raw

    normalized = execution_phase._validated_engine_artifacts([provider_ref], handle)

    assert server_field not in normalized[0]
    assert canary not in repr(normalized)


def test_engine_collection_index_rejects_noncanonical_staged_uri_before_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_calls = 0

    async def record(*_args: Any, **_kwargs: Any) -> None:
        nonlocal index_calls
        index_calls += 1

    monkeypatch.setattr(execution_phase, "record_artifact_references", record)
    thread_id = "staged-uri-tamper"
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-staged-uri-tamper",
        idempotency_key=f"{thread_id}-execution-a1",
    )
    key = f"{engine_artifact_namespace(handle.idempotency_key)}/result.json"
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
        "artifact_store_connection_id": "dev-artifact-store-memory",
        "engine_collection_pending_connection_id": "dev-artifact-store-memory",
        "engine_collection_pending_refs": [
            {
                "kind": "engine_results",
                "name": "results",
                "uri": "https://provider.test/capability?token=opaque",
                "key": key,
                "media_type": "application/json",
                "summary": None,
            }
        ],
        "engine_collection_pending_summary": EngineTestResultSummary(
            engine="sim", passed=True
        ).model_dump(mode="json"),
        "engine_collection_index_required": True,
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )

    command = execution_phase.engine_collection_index(state, exec_config(thread_id))

    assert command.goto == "engine_collection_index"
    assert command.update is not None
    retry = command.update["phase_results"]["execution"]
    assert retry["engine_collection_index_failures"] == 1
    assert index_calls == 0
    assert "provider.test" not in retry["engine_collection_index_last_error"]


@pytest.mark.parametrize(
    "acknowledgement",
    [
        lambda key: StoredArtifact(key=key, uri=f"memory://{key}", size=8),
        lambda key: StoredArtifact(key=key, uri="memory://other/result.json", size=7),
        lambda key: {"key": key, "uri": "javascript:alert(1)", "size": 7},
        lambda key: {
            "key": key,
            "uri": f"memory://{key}",
            "size": 7,
            "provider_token": "engine-artifact-secret-canary",
        },
    ],
)
async def test_engine_artifact_store_rejects_inconsistent_provider_ack(
    acknowledgement: Any,
) -> None:
    namespace = engine_artifact_namespace("artifact-ack-validation")
    key = f"{namespace}/result.json"
    storage_key = f"{namespace}/artifact-0000"

    deleted: list[str] = []

    class Store:
        async def put(self, stored_key: str, *_args: Any, **_kwargs: Any) -> Any:
            return acknowledgement(stored_key)

        async def delete(self, deleted_key: str) -> None:
            deleted.append(deleted_key)

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    with pytest.raises(RuntimeError, match="invalid object metadata") as raised:
        await view.put(key, b"payload", content_type="application/json")

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "engine-artifact-secret-canary" not in str(raised.value)
    assert view.written == {}
    assert deleted == [storage_key]


async def test_engine_artifact_store_returns_only_canonical_uri() -> None:
    namespace = engine_artifact_namespace("artifact-canonical-uri")
    key = f"{namespace}/result.json"
    storage_key = f"{namespace}/artifact-0000"

    class Store:
        async def put(self, stored_key: str, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            return StoredArtifact(
                key=stored_key,
                uri=f"s3://private-bucket/{stored_key}",
                size=7,
            )

        async def delete(self, _key: str) -> None:
            return None

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    stored = await view.put(key, b"payload", content_type="application/json")

    assert stored.key == storage_key
    assert stored.uri == f"apex-artifact:///{storage_key}"
    assert view.written[storage_key][0].uri == stored.uri


async def test_engine_artifact_put_cancellation_waits_for_definitive_cleanup() -> None:
    namespace = engine_artifact_namespace("artifact-put-cancel")
    key = f"{namespace}/result.json"
    storage_key = f"{namespace}/artifact-0000"
    put_started = asyncio.Event()
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()
    objects: set[str] = set()

    class Store:
        async def put(self, stored_key: str, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            objects.add(stored_key)
            put_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def delete(self, stored_key: str) -> None:
            delete_started.set()
            await allow_delete.wait()
            objects.discard(stored_key)

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    task = asyncio.create_task(view.put(key, b"payload", content_type="application/json"))
    await put_started.wait()
    task.cancel()
    await delete_started.wait()
    task.cancel()  # repeated cancellation must not abandon the child cleanup
    await asyncio.sleep(0)
    assert not task.done()
    assert storage_key in objects
    allow_delete.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert storage_key not in objects


async def test_engine_artifact_cancelled_cleanup_reports_fixed_failure() -> None:
    namespace = engine_artifact_namespace("artifact-cancel-cleanup-failure")
    key = f"{namespace}/result.json"
    storage_key = f"{namespace}/artifact-0000"
    put_started = asyncio.Event()
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()
    objects: set[str] = set()

    class Store:
        async def put(self, stored_key: str, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            objects.add(stored_key)
            put_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def delete(self, _stored_key: str) -> None:
            delete_started.set()
            await allow_delete.wait()
            raise OSError("password=provider-cleanup-secret")

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    task = asyncio.create_task(view.put(key, b"payload", content_type="application/json"))
    await put_started.wait()
    task.cancel()
    await delete_started.wait()
    task.cancel()
    allow_delete.set()

    with pytest.raises(RuntimeError, match="could not compensate") as raised:
        await task
    assert raised.value.__cause__ is None
    assert "provider-cleanup-secret" not in str(raised.value)
    # Compensation failure is explicit; the ambiguous object is never reported
    # as a successful provider write or silently abandoned under cancellation.
    assert storage_key in objects
    assert view.written == {}


async def test_engine_artifact_stream_cancellation_compensates_ambiguous_write() -> None:
    namespace = engine_artifact_namespace("artifact-stream-cancel")
    key = f"{namespace}/result.bin"
    storage_key = f"{namespace}/artifact-0000"
    stream_started = asyncio.Event()
    objects: set[str] = set()

    class Store:
        async def put_stream(self, stored_key: str, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            objects.add(stored_key)
            stream_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def delete(self, stored_key: str) -> None:
            objects.discard(stored_key)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"payload"

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    task = asyncio.create_task(
        view.put_stream(
            key,
            chunks(),
            content_type="application/octet-stream",
            max_bytes=32,
        )
    )
    await stream_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert storage_key not in objects


async def test_collection_resource_closes_survive_repeated_cancellation() -> None:
    first_started = asyncio.Event()
    allow_first_close = asyncio.Event()
    closed: list[str] = []

    class FirstResource:
        async def aclose(self) -> None:
            first_started.set()
            await allow_first_close.wait()
            closed.append("first")

    class SecondResource:
        async def aclose(self) -> None:
            closed.append("second")

    task = asyncio.create_task(
        execution_phase._close_resources_definitively(
            FirstResource(),
            SecondResource(),
        )
    )
    await first_started.wait()
    await asyncio.sleep(0)
    assert "second" in closed

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_first_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["second", "first"]


def test_engine_summary_cancellation_cleans_collected_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "summary-cancel-cleanup"
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-summary-cancel",
        idempotency_key=f"{thread_id}-execution-a1",
    )
    key = f"{engine_artifact_namespace(handle.idempotency_key)}/result.json"
    storage_key = f"{engine_artifact_namespace(handle.idempotency_key)}/artifact-0000"
    store = MemoryArtifactStore()

    class Collector:
        async def collect_artifacts(self, _handle: Any, scoped_store: Any) -> list[Any]:
            stored = await scoped_store.put(key, b"{}", content_type="application/json")
            return [
                {
                    "kind": "engine_results",
                    "name": "results",
                    "uri": stored.uri,
                    "key": stored.key,
                    "media_type": "application/json",
                }
            ]

        async def fetch_summary(self, _handle: Any) -> Any:
            raise asyncio.CancelledError

    async def resolve_engine(*_args: Any, **_kwargs: Any) -> Collector:
        return Collector()

    async def resolve_store(
        _cfg: PipelineConfigurable, *, connection_id: str | None = None
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=store,
            connection_id=connection_id or "dev-artifact-store-memory",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve_engine)
    monkeypatch.setattr(execution_phase, "_resolve_artifact_store", resolve_store)
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )

    with pytest.raises(asyncio.CancelledError):
        execution_phase.engine_collect(state, exec_config(thread_id))
    with pytest.raises(KeyError):
        asyncio.run(store.get(storage_key))


async def test_engine_artifact_store_enforces_write_count_before_provider_call() -> None:
    namespace = engine_artifact_namespace("artifact-write-count")
    calls: list[str] = []

    class Store:
        async def put(self, key: str, data: bytes, **_kwargs: Any) -> StoredArtifact:
            calls.append(key)
            return StoredArtifact(key=key, uri=f"memory://{key}", size=len(data))

        async def delete(self, _key: str) -> None:
            return None

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    for index in range(execution_phase.MAX_ENGINE_ARTIFACT_WRITES):
        await view.put(
            f"{namespace}/result-{index}.json",
            b"x",
            content_type="application/json",
        )

    with pytest.raises(ValueError, match="write count"):
        await view.put(
            f"{namespace}/overflow.json",
            b"x",
            content_type="application/json",
        )
    assert len(calls) == execution_phase.MAX_ENGINE_ARTIFACT_WRITES
    assert calls == [
        f"{namespace}/artifact-{index:04d}"
        for index in range(execution_phase.MAX_ENGINE_ARTIFACT_WRITES)
    ]


async def test_engine_artifact_store_reserves_stream_hard_limits_before_provider_call() -> None:
    namespace = engine_artifact_namespace("artifact-stream-hard-limits")
    calls = 0

    class Store:
        async def put_stream(self, *_args: Any, **_kwargs: Any) -> StoredArtifact:
            nonlocal calls
            calls += 1
            raise RuntimeError("ambiguous provider failure")

        async def delete(self, _key: str) -> None:
            return None

    async def empty() -> AsyncIterator[bytes]:
        if False:  # pragma: no cover - establishes the async-generator shape
            yield b""

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    for index in range(2):
        with pytest.raises(RuntimeError, match="ambiguous provider failure"):
            await view.put_stream(
                f"{namespace}/part-{index}.bin",
                empty(),
                content_type="application/octet-stream",
                max_bytes=execution_phase.MAX_ENGINE_ARTIFACT_BYTES_PER_OBJECT,
            )
    with pytest.raises(ValueError, match="aggregate byte limit"):
        await view.put_stream(
            f"{namespace}/overflow.bin",
            empty(),
            content_type="application/octet-stream",
            max_bytes=1,
        )
    with pytest.raises(ValueError, match="per-object byte limit"):
        await view.put_stream(
            f"{namespace}/oversized.bin",
            empty(),
            content_type="application/octet-stream",
            max_bytes=execution_phase.MAX_ENGINE_ARTIFACT_BYTES_PER_OBJECT + 1,
        )
    assert calls == 2


async def test_engine_artifact_store_caps_regular_put_and_cleans_unreturned_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_phase, "MAX_ENGINE_ARTIFACT_BYTES_PER_OBJECT", 4)
    monkeypatch.setattr(execution_phase, "MAX_ENGINE_ARTIFACT_BYTES_TOTAL", 8)
    namespace = engine_artifact_namespace("artifact-regular-budget")
    key = f"{namespace}/result.bin"
    storage_key = f"{namespace}/artifact-0000"
    calls = 0
    store = MemoryArtifactStore()
    view = execution_phase._EngineArtifactStoreView(store, namespace)

    with pytest.raises(ValueError, match="per-object byte limit"):
        await view.put(key, b"12345", content_type="application/octet-stream")
    with pytest.raises(KeyError):
        await store.get(key)

    stored = await view.put(key, b"1234", content_type="application/octet-stream")
    calls += 1
    assert stored.size == 4
    assert await store.get(storage_key) == b"1234"
    await view.cleanup_except(set())
    with pytest.raises(KeyError):
        await store.get(storage_key)
    assert calls == 1


async def test_engine_artifact_replay_converges_changed_keys_and_count_after_crash() -> None:
    """Discarding a view models death before its graph-node update is checkpointed."""

    namespace = engine_artifact_namespace("artifact-crash-replay")
    store = MemoryArtifactStore()
    first_process = execution_phase._EngineArtifactStoreView(store, namespace)

    first_zero = await first_process.put(
        f"{namespace}/provider-uuid-first-a.json",
        b"stale-zero",
        content_type="application/json",
    )
    first_one = await first_process.put(
        f"{namespace}/provider-uuid-first-b.json",
        b"stale-one",
        content_type="application/json",
    )
    assert first_zero.key == f"{namespace}/artifact-0000"
    assert first_one.key == f"{namespace}/artifact-0001"

    # The process disappears here: neither cleanup nor a graph checkpoint can run.
    retry_process = execution_phase._EngineArtifactStoreView(store, namespace)
    current = await retry_process.put(
        f"{namespace}/different-provider-uuid.json",
        b"current",
        content_type="application/json",
    )
    await retry_process.cleanup_except({current.key})

    assert current.key == first_zero.key
    assert await store.get(current.key) == b"current"
    with pytest.raises(KeyError):
        await store.get(first_one.key)


@pytest.mark.parametrize(
    ("consume", "size_delta", "message"),
    [
        ("all", 1, "invalid object metadata"),
        ("one", 0, "did not consume the complete"),
        ("none", 0, "did not consume the complete"),
    ],
)
async def test_engine_artifact_stream_requires_full_consumption_and_exact_size(
    consume: str,
    size_delta: int,
    message: str,
) -> None:
    namespace = engine_artifact_namespace("artifact-stream-validation")
    key = f"{namespace}/stream.bin"
    storage_key = f"{namespace}/artifact-0000"

    deleted: list[str] = []

    class Store:
        async def put_stream(
            self,
            stored_key: str,
            data: Any,
            **_kwargs: Any,
        ) -> StoredArtifact:
            size = 0
            if consume == "all":
                async for chunk in data:
                    size += len(chunk)
            elif consume == "one":
                async for chunk in data:
                    size += len(chunk)
                    break
            return StoredArtifact(
                key=stored_key,
                uri=f"memory://{stored_key}",
                size=size + size_delta,
            )

        async def delete(self, deleted_key: str) -> None:
            deleted.append(deleted_key)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"de"

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    with pytest.raises(RuntimeError, match=message):
        await view.put_stream(
            key,
            chunks(),
            content_type="application/octet-stream",
            max_bytes=10,
        )

    assert view.written == {}
    assert deleted == [storage_key]


def test_provider_models_reject_nonfinite_and_oversized_checkpoint_data() -> None:
    with pytest.raises(ValueError):
        LoadTestSpec(title="load\x00test")
    with pytest.raises(ValueError):
        EngineRunStatus(
            phase=EngineRunPhase.RUNNING,
            progress_pct=float("nan"),
        )
    with pytest.raises(ValueError):
        EngineTestResultSummary(
            engine="sim",
            passed=True,
            kpis={"bad": float("inf")},
        )
    with pytest.raises(ValueError):
        EngineHandle(
            engine="sim",
            extras={f"field-{index}": "x" for index in range(33)},
        )
    with pytest.raises(ValueError):
        EngineHandle(engine="sim", external_run_id="provider\x00run")
    secret = "engine-handle-secret-canary"
    for extras in (
        {"provider_token": secret},
        {"provider_data": f"Authorization: Bearer {secret}"},
        {"provider_data": f"https://provider.test/run?X-Amz-Signature={secret}"},
    ):
        with pytest.raises(ValueError) as excinfo:
            EngineHandle(engine="sim", extras=extras)
        assert secret not in str(excinfo.value)
    with pytest.raises(ValueError):
        EngineRunStatus(phase=EngineRunPhase.RUNNING, message="provider\x00message")
    with pytest.raises(ValueError):
        EngineTestResultSummary(engine="sim", passed=False, notes="provider\x00notes")


def test_provider_boundaries_never_invoke_arbitrary_model_dump() -> None:
    class SerializerBomb:
        calls = 0

        def model_dump(self, *_args: Any, **_kwargs: Any) -> Any:
            type(self).calls += 1
            raise AssertionError("provider serializer must not run")

    handle = EngineHandle(engine="sim", idempotency_key="serializer-bomb-execution-a1")
    checks = (
        lambda value: execution_phase._validated_engine_status(value),
        lambda value: execution_phase._validated_engine_report(value),
        lambda value: execution_phase._validated_engine_summary(value),
        lambda value: execution_phase._validated_engine_handle(value),
        lambda value: execution_phase._validated_engine_artifacts([value], handle),
    )

    for check in checks:
        with pytest.raises(ValueError):
            check(SerializerBomb())
    assert SerializerBomb.calls == 0


@pytest.mark.parametrize(
    ("validator", "value"),
    [
        (
            execution_phase._validated_engine_status,
            {"phase": "bare-provider-status-canary"},
        ),
        (
            execution_phase._validated_engine_summary,
            {
                "engine": "sim",
                "passed": "bare-provider-summary-canary",
            },
        ),
        (
            execution_phase._validated_engine_report,
            {"ok": "bare-provider-report-canary"},
        ),
    ],
)
def test_provider_model_validation_does_not_retain_raw_values(
    validator: Any,
    value: dict[str, Any],
) -> None:
    canary = next(
        item
        for item in value.values()
        if isinstance(item, str) and item.startswith("bare-provider-")
    )

    with pytest.raises(ValueError) as raised:
        validator(value)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)


def test_provider_boundaries_never_iterate_container_subclasses() -> None:
    class MappingBomb(dict[str, Any]):
        calls = 0

        def __iter__(self) -> Any:
            type(self).calls += 1
            raise AssertionError("provider mapping iterator must not run")

        def __getitem__(self, key: str) -> Any:
            type(self).calls += 1
            raise AssertionError("provider mapping item access must not run")

    handle = EngineHandle(engine="sim", idempotency_key="mapping-bomb-execution-a1")
    checks = (
        lambda value: execution_phase._validated_engine_status(value),
        lambda value: execution_phase._validated_engine_report(value),
        lambda value: execution_phase._validated_engine_summary(value),
        lambda value: execution_phase._validated_engine_handle(value),
        lambda value: execution_phase._validated_engine_artifacts([value], handle),
    )

    for check in checks:
        with pytest.raises(ValueError):
            check(MappingBomb())
    assert MappingBomb.calls == 0


def test_engine_status_validation_never_reads_untrusted_class_descriptor() -> None:
    calls: list[str] = []

    class ClassBomb:
        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                calls.append("class")
                raise AssertionError("provider value __class__ must not be read")
            return object.__getattribute__(self, name)

    with pytest.raises(ValueError, match="engine status is invalid"):
        execution_phase._validated_engine_status({"phase": ClassBomb()})
    assert calls == []


def test_provider_boundaries_reject_manufactured_oversized_models_before_copy() -> None:
    with pytest.raises(ValueError, match="engine status is invalid"):
        execution_phase._validated_engine_status(
            EngineRunStatus.model_construct(
                phase=EngineRunPhase.RUNNING,
                progress_pct=0,
                message="x" * 4_097,
            )
        )
    with pytest.raises(ValueError, match="validation report is invalid"):
        execution_phase._validated_engine_report(
            ValidationReport.model_construct(ok=False, issues=["x"] * 129)
        )
    with pytest.raises(ValueError, match="engine summary is invalid"):
        execution_phase._validated_engine_summary(
            EngineTestResultSummary.model_construct(
                engine="sim",
                passed=True,
                kpis={f"metric-{index}": 1 for index in range(65)},
                sla_breaches=[],
                notes=None,
            )
        )
    with pytest.raises(ValueError, match="engine handle is invalid"):
        execution_phase._validated_engine_handle(
            EngineHandle.model_construct(
                engine="sim",
                connection_id=None,
                external_run_id=None,
                idempotency_key="manufactured-handle-execution-a1",
                extras={f"field-{index}": "x" for index in range(33)},
            )
        )
    handle = EngineHandle(engine="sim", idempotency_key="manufactured-ref-execution-a1")
    key = f"{engine_artifact_namespace(handle.idempotency_key)}/result.json"
    with pytest.raises(ValueError, match="engine artifact ref 0 is invalid"):
        execution_phase._validated_engine_artifacts(
            [
                ArtifactRef.model_construct(
                    kind="engine_results",
                    name="x" * 513,
                    uri=canonical_artifact_uri(key),
                    key=key,
                    media_type="application/json",
                    summary=None,
                )
            ],
            handle,
        )


def test_poll_sample_redacts_provider_diagnostics_before_checkpoint() -> None:
    secret = "poll-message-secret-canary"
    sample = execution_phase._poll_sample(
        EngineRunStatus(
            phase=EngineRunPhase.RUNNING,
            message=(
                f"Authorization: Bearer {secret}; "
                f"https://provider.test/result?X-Amz-Signature={secret}"
            ),
        )
    )

    assert secret not in sample["message"]
    assert "[REDACTED]" in sample["message"]


@pytest.mark.parametrize(
    "value",
    [
        {"external_run_id": "Authorization: Bearer engine-secret-canary"},
        {"connection_id": "https://engine.test/?sig=engine-secret-canary"},
    ],
)
def test_engine_handle_boundary_rejects_credential_identity_without_reflection(
    value: dict[str, str],
) -> None:
    secret = "engine-secret-canary"
    raw = {
        "engine": "sim",
        "idempotency_key": "safe-execution-a1",
        **value,
    }

    with pytest.raises(ValueError, match="credential material") as raised:
        execution_phase._validated_engine_handle(raw)

    assert secret not in str(raised.value)


def test_execution_rejects_inconsistent_top_level_and_phase_handles() -> None:
    phase_handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="intended-run",
        idempotency_key="handle-consistency-execution-a1",
    ).model_dump(mode="json")
    redirected = {
        **phase_handle,
        "external_run_id": "different-provider-run",
    }
    phase_entry = {
        "attempt": 1,
        "engine_handle": phase_handle,
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": redirected,
            "phase_results": {"execution": phase_entry},
        },
    )

    with pytest.raises(ValueError, match="handles are inconsistent"):
        execution_phase._handle_from(state, phase_entry)


@pytest.mark.parametrize(
    "summary",
    [
        {"notes": "Authorization: Bearer summary-secret-canary"},
        {"sla_breaches": ["password=summary-secret-canary"]},
    ],
)
def test_engine_summary_boundary_rejects_credentials_without_reflection(
    summary: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        execution_phase._validated_engine_summary(
            {"engine": "sim", "passed": False, **summary},
            expected_engine="sim",
        )

    assert "summary-secret-canary" not in str(raised.value)


def test_engine_artifact_boundary_rejects_signed_uri_without_reflection() -> None:
    secret = "artifact-uri-secret-canary"
    handle = EngineHandle(engine="sim", idempotency_key="artifact-secret-execution-a1")
    namespace = engine_artifact_namespace(handle.idempotency_key)
    ref = {
        "kind": "engine_report",
        "name": "report.json",
        "uri": f"https://objects.test/report.json?sig={secret}",
        "key": f"{namespace}/report.json",
    }

    with pytest.raises(ValueError, match="credential material") as raised:
        execution_phase._validated_engine_artifacts([ref], handle)

    assert secret not in str(raised.value)


@pytest.mark.parametrize("compensation_succeeds", [True, False])
def test_unsafe_provision_handle_is_compensated_or_retried_without_durable_leak(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
    compensation_succeeds: bool,
) -> None:
    secret = "unsafe-provision-handle-secret-canary"
    calls: list[str] = []
    events: list[dict[str, Any]] = []

    class UnsafeProvisioner:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            del spec
            return ValidationReport(ok=True)

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            calls.append("provision")
            # Simulate an untrusted plugin bypassing normal model construction;
            # the runtime boundary must still compensate the unsafe handle.
            return EngineHandle.model_construct(
                engine="provider-controlled",
                connection_id="provider-controlled",
                external_run_id=f"Authorization: Bearer {secret}",
                idempotency_key=spec.idempotency_key,
                extras={},
            )

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            assert secret in str(handle.external_run_id)
            assert reason == "provider returned an unsafe durable handle"
            calls.append("abort")
            if not compensation_succeeds:
                raise RuntimeError(f"abort failed for password={secret}")

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            assert secret in str(handle.external_run_id)
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.ABORTED)

        async def teardown(self, handle: EngineHandle) -> None:
            assert secret in str(handle.external_run_id)
            calls.append("teardown")

    adapter = UnsafeProvisioner()

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        assert connection_id == "engine-a"
        return ResolvedAdapter(
            adapter=adapter,
            connection_id="engine-a",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(execution_phase, "emit_event", events.append)
    spec = LoadTestSpec(
        idempotency_key="unsafe-provision-execution-a1",
        title="unsafe provision",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "load_test_spec": spec.model_dump(mode="json"),
                    "engine_options": {},
                    "engine_connection_id": "engine-a",
                    "engine_connection_version": None,
                    "engine_connection_persisted": False,
                    "engine_connection_affinity_staged": True,
                }
            }
        },
    )

    command = execution_phase.engine_provision(
        state,
        exec_config("unsafe-provision"),
    )

    if compensation_succeeds:
        assert command.goto == "finalize"
        assert calls == ["provision", "abort", "status", "teardown"]
        assert [call["status"] for call in projection_calls] == [
            EngineRunPhase.PROVISIONING.value,
            EngineRunPhase.FAILED.value,
        ]
    else:
        assert command.goto == "engine_provision"
        assert calls == ["provision", "abort"]
        assert [call["status"] for call in projection_calls] == [
            EngineRunPhase.PROVISIONING.value,
        ]
    assert secret not in repr(command)
    assert secret not in repr(projection_calls)
    assert secret not in repr(events)


def test_engine_poll_custom_events_streamed() -> None:
    g = compiled()
    cfg = exec_config("exec-events")
    events = custom_events(g, public_inputs(), cfg)
    polls = [e for e in events if e.get("type") == "engine_poll"]
    # A provider is allowed to finish before the first status observation. The
    # initial read is still a counted/emitted poll; an intermediate RUNNING sample
    # is not a portable execution-engine contract.
    assert len(polls) >= 1
    assert all(e["phase"] == "execution" for e in polls)
    assert all(e["schema_version"] == 1 for e in polls)
    assert polls[0]["status"] in {"running", "completed"}
    assert polls[-1]["status"] == "completed"  # terminal status is emitted
    assert {e["external_run_id"] for e in polls} == {polls[0]["external_run_id"]}
    running = [e for e in polls if e["status"] == "running"]
    assert all(set(e["live_stats"]) == {"vusers", "tps", "error_rate", "p95_ms"} for e in running)
    assert any(e["progress_pct"] > 0 for e in polls)


def test_initial_terminal_status_is_counted_as_first_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AlreadyCompletedEngine:
        async def get_status(self, _handle: EngineHandle) -> EngineRunStatus:
            return EngineRunStatus(
                phase=EngineRunPhase.COMPLETED,
                progress_pct=100.0,
                message="completed before first observation",
            )

    async def resolve(*_args: Any, **_kwargs: Any) -> AlreadyCompletedEngine:
        return AlreadyCompletedEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-already-completed",
        idempotency_key="initial-terminal-execution-a1",
    )
    handle_json = handle.model_dump(mode="json")
    state = cast(
        PipelineState,
        {
            "engine_handle": handle_json,
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle_json,
                    "engine_options": {},
                }
            },
        },
    )

    observed = execution_phase.engine_status(state, exec_config("initial-terminal"))

    assert observed.goto == "engine_collect"
    assert observed.update is not None
    entry = observed.update["phase_results"]["execution"]
    assert entry["engine_poll_count"] == 1
    assert entry["engine_poll_last"]["status"] == EngineRunPhase.COMPLETED.value


def test_idempotency_key_deterministic_and_provision_stable() -> None:
    g = compiled()
    cfg = exec_config("exec-idem")
    result = g.invoke(public_inputs(), cfg)
    entry = result["phase_results"]["execution"]
    assert entry["load_test_spec"]["idempotency_key"] == "exec-idem-execution-a1"
    run_id = result["engine_handle"]["external_run_id"]

    # Re-executing provision with the checkpointed spec yields the same run id
    # (get-or-create by key): crash recovery cannot double-start load.
    engine = SimExecutionEngine(None)
    spec = LoadTestSpec.model_validate(entry["load_test_spec"])
    assert asyncio.run(engine.provision(spec)).external_run_id == run_id
    assert asyncio.run(engine.provision(spec)).external_run_id == run_id

    # A fresh run on the same thread bumps the attempt -> new key -> new engine run.
    result2 = g.invoke({"title": "Demo"}, cfg)
    entry2 = result2["phase_results"]["execution"]
    assert entry2["attempt"] == 2
    assert entry2["load_test_spec"]["idempotency_key"] == "exec-idem-execution-a2"
    assert result2["engine_handle"]["external_run_id"] != run_id


def test_failure_injection_fails_phase_and_still_tears_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_engine_spy(monkeypatch)
    g = compiled()
    cfg = exec_config("exec-fail", load_test={"fail_at_pct": 50.0, "duration_s": 0.2})
    result = g.invoke(public_inputs(), cfg)

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    # fail_at_pct flowed from the configurable into engine options (not the spec)
    assert entry["engine_options"] == {"fail_at_pct": 50.0}
    assert entry["load_test_spec"]["duration_s"] == 0.2
    assert entry["engine_poll_last"]["status"] == "failed"
    assert entry["test_summary"]["passed"] is False
    assert entry["errors"] == ["run failed before completion (injected failure)"]
    assert "teardown" in calls  # checkpoint-gated collection settle completed
    assert "abort" not in calls
    assert result["run_aborted"] is False


def test_engine_options_reject_connection_overrides_before_checkpoint(
    projection_calls: list[dict[str, Any]],
) -> None:
    g = compiled()
    cfg = exec_config("exec-bad-options", load_test={"base_url": "https://evil.invalid"})
    result = g.invoke(public_inputs(), cfg)

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    assert "unsupported load_test engine option(s)" in entry["errors"][0]
    assert "base_url" in entry["errors"][0]
    assert "engine_options" not in entry
    assert "load_test_spec" not in entry
    assert projection_calls == []


def test_direct_target_environment_override_is_rejected_before_checkpoint(
    projection_calls: list[dict[str, Any]],
) -> None:
    g = compiled()
    cfg = exec_config(
        "exec-forged-target",
        load_test={"target_environment": "http://169.254.169.254/latest/meta-data"},
    )
    result = g.invoke(public_inputs(), cfg)

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    assert "unsupported load_test engine option(s)" in entry["errors"][0]
    assert "target_environment" in entry["errors"][0]
    assert "load_test_spec" not in entry
    assert projection_calls == []


def test_apex_load_inline_script_cannot_bypass_catalog_target() -> None:
    cfg = exec_config("exec-inline-bypass")
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(
        RunnableConfig,
        {**cfg, "configurable": {**configurable, "engine": "apex_load"}},
    )
    state = cast(
        PipelineState,
        seeded_inputs(
            0.1,
            script_refs=['{"config":{"base_url":"http://169.254.169.254/latest/meta-data"}}'],
        ),
    )

    spec, _options = execution_phase._build_spec(
        state,
        cfg,
        1,
        "apex_load",
        target_environment="https://approved.example.test",
    )
    assert spec.script_refs == []
    assert spec.target_environment == "https://approved.example.test"


def test_engine_reserve_checkpoints_server_resolved_catalog_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_cfg: Any) -> str:
        return "https://approved.example.test"

    monkeypatch.setattr(execution_phase, "_resolve_catalog_target", resolve)
    cfg = exec_config("exec-approved-target")
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(
        RunnableConfig,
        {
            **cfg,
            "configurable": {
                **configurable,
                "engine": "apex_load",
                "environment_id": "env-a",
                "environment_target": "http://forged.invalid",
                "project_id": "p1",
                "app_id": "app-a",
            },
        },
    )

    command = execution_phase.engine_reserve(
        cast(PipelineState, seeded_inputs(0.1, target_environment="http://seeded.invalid")),
        cfg,
    )

    assert isinstance(command.update, dict)
    entry = command.update["phase_results"]["execution"]
    assert entry["load_test_spec"]["target_environment"] == "https://approved.example.test"
    assert entry["load_test_spec"]["script_refs"] == []


def test_stamped_environment_target_rejects_gated_run_drift() -> None:
    cfg = PipelineConfigurable(
        environment_id="env-a",
        app_id="app-a",
        environment_target="https://8.8.8.8/original",
        environment_target_version=3,
    )

    with pytest.raises(ValueError, match="changed after run creation"):
        execution_phase._verified_stamped_target(
            cfg,
            "https://8.8.4.4/replacement",
            4,
            current_app_id="app-a",
        )


def test_assistant_only_environment_config_requires_run_authorization_stamp() -> None:
    cfg = PipelineConfigurable(environment_id="env-a")

    with pytest.raises(ValueError, match="not authorized and stamped"):
        execution_phase._verified_stamped_target(
            cfg,
            "https://8.8.8.8/load",
            1,
            current_app_id="app-a",
        )


@pytest.mark.parametrize("run_app_id", [None, "app-b"])
def test_stamped_environment_target_requires_exact_application_scope(
    run_app_id: str | None,
) -> None:
    cfg = PipelineConfigurable(
        environment_id="env-a",
        app_id=run_app_id,
        environment_target="https://8.8.8.8/load",
        environment_target_version=1,
    )

    with pytest.raises(ValueError, match="application scope"):
        execution_phase._verified_stamped_target(
            cfg,
            "https://8.8.8.8/load",
            1,
            current_app_id="app-a",
        )


def test_trusted_persisted_loadrunner_workload_options_remain_resumable() -> None:
    cfg = exec_config(
        "exec-loadrunner-options",
        load_test={"test_id": 42, "test_instance_id": 7, "abortive_stop": True},
    )
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(RunnableConfig, {**cfg, "configurable": {**configurable, "engine": "loadrunner"}})
    _spec, engine_options = execution_phase._build_spec(
        cast(PipelineState, seeded_inputs(0.1)), cfg, 1, "loadrunner"
    )
    assert engine_options == {"test_id": 42, "test_instance_id": 7, "abortive_stop": True}


def test_trusted_legacy_script_refs_remain_resumable() -> None:
    cfg = exec_config(
        "exec-legacy-script",
        load_test={"script_refs": ["script-existing"]},
    )
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(RunnableConfig, {**cfg, "configurable": {**configurable, "engine": "loadrunner"}})

    spec, engine_options = execution_phase._build_spec(
        cast(PipelineState, seeded_inputs(0.1)), cfg, 1, "loadrunner"
    )

    assert spec.script_refs == ["script-existing"]
    assert engine_options == {}


def test_structured_connection_metadata_is_reserved_before_provision(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    version = datetime.fromisoformat("2026-06-01T12:00:00+00:00")

    async def resolve(
        cfg: Any,
        engine_options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        conn = ConnectionConfig(
            id=connection_id or "stored-engine",
            kind=PortKind.EXECUTION_ENGINE,
            provider="sim",
            name="Stored sim engine",
            options=dict(engine_options),
        )
        return ResolvedAdapter(
            adapter=SimExecutionEngine(conn),
            connection_id=conn.id,
            connection_version=version,
            persisted=True,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)

    result = compiled().invoke(public_inputs(), exec_config("exec-structured-lease"))

    assert result["phase_results"]["execution"]["status"] == "succeeded"
    reservation = projection_calls[0]
    assert reservation["status"] == "provisioning"
    assert reservation["connection_id"] == "stored-engine"
    assert reservation["connection_version"] == version
    assert reservation["required"] is True


@pytest.mark.parametrize("first_outcome", ["transport", "malformed", "http_5xx"])
def test_ambiguous_provision_retries_same_key_and_pinned_connection(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
    first_outcome: str,
) -> None:
    resolution_ids: list[str | None] = []
    provision_keys: list[str] = []
    remote_run_id = "remote-created-before-response-loss"

    class AmbiguousProvisioner:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            return ValidationReport(ok=True)

        async def provision(self, spec: LoadTestSpec) -> Any:
            provision_keys.append(spec.idempotency_key)
            if len(provision_keys) == 1:
                if first_outcome == "transport":
                    raise OSError("provision response lost after remote create")
                if first_outcome == "http_5xx":
                    raise RuntimeError("provider returned HTTP 503 after remote create")
                return object()
            return EngineHandle(
                engine="sim",
                connection_id="provider-controlled",
                external_run_id=remote_run_id,
                idempotency_key="provider-controlled",
            )

    adapter = AmbiguousProvisioner()

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        resolution_ids.append(connection_id)
        return ResolvedAdapter(
            adapter=adapter,
            connection_id="engine-a",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    thread_id = f"provision-reconcile-{first_outcome}"
    config = exec_config(thread_id)
    spec = LoadTestSpec(
        idempotency_key=f"{thread_id}-execution-a1",
        title="ambiguous provision",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
    }
    state = cast(PipelineState, {"phase_results": {"execution": entry}})

    affinity = execution_phase.engine_provision(state, config)
    assert affinity.goto == "engine_provision"
    assert affinity.update is not None
    affinity_entry = affinity.update["phase_results"]["execution"]
    assert affinity_entry["engine_connection_id"] == "engine-a"
    assert affinity_entry["engine_connection_affinity_staged"] is True
    assert provision_keys == []
    entry.update(affinity_entry)
    assert execution_phase.route_execution_entry(state) == "engine_provision_resume"

    ambiguous = execution_phase.engine_provision(state, config)
    assert ambiguous.goto == "engine_provision"
    assert ambiguous.update is not None
    ambiguous_entry = ambiguous.update["phase_results"]["execution"]
    assert ambiguous_entry["status"] == PhaseStatus.RUNNING.value
    assert ambiguous_entry["engine_provision_required"] is True
    assert ambiguous_entry["engine_provision_failures"] == 1
    assert "failed (1/3)" in ambiguous_entry["engine_provision_last_error"]
    entry.update(ambiguous_entry)

    reconciled = execution_phase.engine_provision(state, config)
    assert reconciled.goto == "engine_start"
    assert reconciled.update is not None
    reconciled_entry = reconciled.update["phase_results"]["execution"]
    assert reconciled_entry["engine_handle"]["external_run_id"] == remote_run_id
    assert reconciled_entry["engine_handle"]["connection_id"] == "engine-a"
    assert reconciled_entry["engine_handle"]["idempotency_key"] == spec.idempotency_key
    assert reconciled_entry["engine_provision_required"] is False
    assert reconciled_entry["engine_provision_failures"] == 0
    assert provision_keys == [spec.idempotency_key, spec.idempotency_key]
    assert resolution_ids == [None, "engine-a", "engine-a"]
    assert [call["status"] for call in projection_calls] == [
        "provisioning",
        "provisioning",
        "ready",
    ]


def test_provision_crash_reconciles_from_affinity_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    provision_keys: list[str] = []

    class CrashAfterCreate:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            return ValidationReport(ok=True)

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            provision_keys.append(spec.idempotency_key)
            if len(provision_keys) == 1:
                raise asyncio.CancelledError("worker stopped after remote create")
            return EngineHandle(
                engine="sim",
                connection_id="engine-a",
                external_run_id="reconciled-run",
                idempotency_key=spec.idempotency_key,
            )

    adapter = CrashAfterCreate()

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=adapter,
            connection_id=connection_id or "engine-a",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    spec = LoadTestSpec(
        idempotency_key="provision-crash-execution-a1",
        title="crash recovery",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
    }
    state = cast(PipelineState, {"phase_results": {"execution": entry}})
    config = exec_config("provision-crash")
    affinity = execution_phase.engine_provision(state, config)
    assert affinity.update is not None
    entry.update(affinity.update["phase_results"]["execution"])

    with pytest.raises(asyncio.CancelledError, match="worker stopped"):
        execution_phase.engine_provision(state, config)
    assert entry["engine_provision_failures"] == 0
    assert execution_phase.route_execution_entry(state) == "engine_provision_resume"

    reconciled = execution_phase.engine_provision(state, config)
    assert reconciled.goto == "engine_start"
    assert reconciled.update is not None
    assert (
        reconciled.update["phase_results"]["execution"]["engine_handle"]["external_run_id"]
        == "reconciled-run"
    )
    assert provision_keys == [spec.idempotency_key, spec.idempotency_key]
    assert [call["status"] for call in projection_calls] == [
        "provisioning",
        "provisioning",
        "ready",
    ]


def test_provision_handle_projection_response_loss_reconciles_same_remote_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision_keys: list[str] = []
    projection_handles: list[str | None] = []
    lost_handle_ack = False

    class IdempotentProvisioner:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            return ValidationReport(ok=True)

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            provision_keys.append(spec.idempotency_key)
            return EngineHandle(
                engine="sim",
                connection_id="engine-a",
                external_run_id="same-remote-run",
                idempotency_key=spec.idempotency_key,
            )

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=IdempotentProvisioner(),
            connection_id=connection_id or "engine-a",
            connection_version=None,
            persisted=False,
        )

    def record_projection(
        _thread_id: str,
        _attempt: int,
        _engine: str,
        handle: dict[str, Any],
        _status: str,
        **_kwargs: Any,
    ) -> None:
        nonlocal lost_handle_ack
        external_run_id = handle.get("external_run_id")
        projection_handles.append(external_run_id)
        if external_run_id and not lost_handle_ack:
            lost_handle_ack = True
            raise RuntimeError("handle projection response lost after commit")

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record_projection)
    spec = LoadTestSpec(
        idempotency_key="handle-projection-loss-execution-a1",
        title="handle projection recovery",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
    }
    state = cast(PipelineState, {"phase_results": {"execution": entry}})
    config = exec_config("handle-projection-loss")
    affinity = execution_phase.engine_provision(state, config)
    assert affinity.update is not None
    entry.update(affinity.update["phase_results"]["execution"])

    lost = execution_phase.engine_provision(state, config)
    assert lost.goto == "engine_provision"
    assert lost.update is not None
    entry.update(lost.update["phase_results"]["execution"])
    recovered = execution_phase.engine_provision(state, config)

    assert recovered.goto == "engine_start"
    assert recovered.update is not None
    assert (
        recovered.update["phase_results"]["execution"]["engine_handle"]["external_run_id"]
        == "same-remote-run"
    )
    assert provision_keys == [spec.idempotency_key, spec.idempotency_key]
    assert projection_handles == ["same-remote-run", "same-remote-run"]


def test_ambiguous_provision_retries_are_bounded_without_terminalizing(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    class UnavailableProvisioner:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            return ValidationReport(ok=True)

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            raise OSError("provision response remains unavailable")

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=UnavailableProvisioner(),
            connection_id=connection_id or "engine-a",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    spec = LoadTestSpec(
        idempotency_key="provision-blocked-execution-a1",
        title="blocked provision",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
    }
    state = cast(PipelineState, {"phase_results": {"execution": entry}})
    config = exec_config("provision-blocked")

    affinity = execution_phase.engine_provision(state, config)
    assert affinity.update is not None
    entry.update(affinity.update["phase_results"]["execution"])
    for failure in range(1, execution_phase.MAX_ENGINE_PROVISION_ATTEMPTS + 1):
        command = execution_phase.engine_provision(state, config)
        assert command.update is not None
        command_entry = command.update["phase_results"]["execution"]
        assert command_entry["engine_provision_failures"] == failure
        expected = (
            "engine_provision_blocked"
            if failure == execution_phase.MAX_ENGINE_PROVISION_ATTEMPTS
            else "engine_provision"
        )
        assert command.goto == expected
        entry.update(command_entry)

    assert entry["status"] == PhaseStatus.RUNNING.value
    assert entry["engine_provision_required"] is True
    assert entry["engine_provision_blocked"] is True
    assert execution_phase.route_execution_entry(state) == "engine_provision_resume"
    assert [call["status"] for call in projection_calls] == ["provisioning"] * 3


def test_definitive_provider_validation_failure_terminalizes_without_provision(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    provision_calls = 0

    class InvalidSpecProvider:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            return ValidationReport(ok=False, issues=["provider rejected the workload"])

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            nonlocal provision_calls
            provision_calls += 1
            raise AssertionError("definitive validation must prevent provision")

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=InvalidSpecProvider(),
            connection_id=connection_id or "engine-a",
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    spec = LoadTestSpec(
        idempotency_key="invalid-provider-spec-execution-a1",
        title="invalid provider spec",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
    }
    state = cast(PipelineState, {"phase_results": {"execution": entry}})
    config = exec_config("invalid-provider-spec")

    affinity = execution_phase.engine_provision(state, config)
    assert affinity.update is not None
    entry.update(affinity.update["phase_results"]["execution"])
    rejected = execution_phase.engine_provision(state, config)

    assert rejected.goto == "finalize"
    assert rejected.update is not None
    rejected_entry = rejected.update["phase_results"]["execution"]
    assert rejected_entry["status"] == PhaseStatus.FAILED.value
    assert rejected_entry["engine_provision_required"] is False
    assert "provider rejected the workload" in rejected_entry["errors"][0]
    assert provision_calls == 0
    assert [call["status"] for call in projection_calls] == ["provisioning", "failed"]


def test_rejected_required_projection_prevents_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []
    projection_statuses: list[str] = []

    class NeverCalledEngine:
        _apex_resolved_connection_id = "real-engine"
        _apex_resolved_connection_version = None

        async def validate(self, spec: LoadTestSpec) -> Any:
            provider_calls.append("validate")
            raise AssertionError("provider validation must not run")

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            provider_calls.append("provision")
            raise AssertionError("provider provision must not run")

    async def resolve(*_args: Any, **_kwargs: Any) -> NeverCalledEngine:
        return NeverCalledEngine()

    def reject_projection(
        _thread_id: str,
        _attempt: int,
        _engine: str,
        _handle: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        projection_statuses.append(EngineRunPhase.PROVISIONING.value)
        raise engine_runs.EngineRunReservationRejectedError(
            "required engine-run reservation was rejected by terminal attempt"
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "prepare_engine_provision_sync", reject_projection)
    spec = LoadTestSpec(
        idempotency_key="terminal-conflict-execution-a1",
        title="terminal conflict",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "load_test_spec": spec.model_dump(mode="json"),
                    "engine_options": {},
                    "engine_connection_id": "real-engine",
                    "engine_connection_version": None,
                    "engine_connection_persisted": False,
                    "engine_connection_affinity_staged": True,
                }
            }
        },
    )

    with pytest.raises(
        engine_runs.EngineRunReservationRejectedError,
        match="rejected by terminal attempt",
    ):
        execution_phase.engine_provision(state, exec_config("terminal-conflict"))

    assert projection_statuses == ["provisioning"]
    assert provider_calls == []


def test_provision_replay_recovers_committed_full_handle_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    provider_calls: list[str] = []
    recovery_calls = 0

    class NeverCalledEngine:
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            provider_calls.append("validate")
            raise AssertionError("recovered provision must not validate again")

        async def provision(self, spec: LoadTestSpec) -> EngineHandle:
            provider_calls.append("provision")
            raise AssertionError("recovered provision must not create again")

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=NeverCalledEngine(),
            connection_id=connection_id or "engine-a",
            connection_version=None,
            persisted=False,
        )

    def recover(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal recovery_calls
        recovery_calls += 1
        return {
            "engine": "attacker-engine",
            "connection_id": "attacker-connection",
            "external_run_id": "committed-remote-run",
            "idempotency_key": "attacker-key",
            "extras": {"provider_marker": "bounded"},
        }

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "prepare_engine_provision_sync", recover)
    spec = LoadTestSpec(
        idempotency_key="provision-recovery-execution-a1",
        title="recover provision",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "load_test_spec": spec.model_dump(mode="json"),
                    "engine_options": {},
                    "engine_connection_id": "engine-a",
                    "engine_connection_version": None,
                    "engine_connection_persisted": False,
                    "engine_connection_affinity_staged": True,
                }
            }
        },
    )
    config = exec_config("provision-recovery")

    first = execution_phase.engine_provision(state, config)
    second = execution_phase.engine_provision(state, config)

    for command in (first, second):
        assert command.goto == "engine_start"
        assert command.update is not None
        recovered = command.update["engine_handle"]
        assert recovered == {
            "engine": "sim",
            "connection_id": "engine-a",
            "external_run_id": "committed-remote-run",
            "idempotency_key": spec.idempotency_key,
            "extras": {"provider_marker": "bounded"},
        }
    assert recovery_calls == 2
    assert provider_calls == []
    assert projection_calls == []


def test_provisioned_handle_uses_trusted_resolver_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ConnectionConfig(
        id="trusted-engine",
        kind=PortKind.EXECUTION_ENGINE,
        provider="sim",
        name="Trusted sim engine",
    )
    inner = SimExecutionEngine(connection)
    provision_specs: list[LoadTestSpec] = []
    retained_provider_handles: list[EngineHandle] = []

    class LyingEngine(EngineSpy):
        async def validate(self, spec: LoadTestSpec) -> ValidationReport:
            spec.idempotency_key = "attacker-validation-key"
            spec.target_environment = "https://attacker.invalid"
            return ValidationReport(ok=True)

        async def provision(self, spec: Any) -> EngineHandle:
            provision_specs.append(LoadTestSpec.model_validate(spec.model_dump(mode="python")))
            spec.idempotency_key = "attacker-provision-key"
            spec.target_environment = "https://attacker.invalid"
            handle = await self._inner.provision(spec)
            returned = handle.model_copy(
                update={
                    "engine": "attacker-engine",
                    "connection_id": "attacker-connection",
                    "idempotency_key": "attacker-namespace",
                }
            )
            retained_provider_handles.append(returned)
            return returned

    async def resolve(
        _cfg: Any,
        _options: dict[str, Any],
        *,
        connection_id: str | None = None,
    ) -> ResolvedAdapter:
        assert connection_id in {None, connection.id}
        return ResolvedAdapter(
            adapter=LyingEngine(inner, []),
            connection_id=connection.id,
            connection_version=None,
            persisted=False,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)

    result = compiled().invoke(public_inputs(), exec_config("exec-handle-affinity"))
    handle = result["phase_results"]["execution"]["engine_handle"]

    assert handle["engine"] == "sim"
    assert handle["connection_id"] == "trusted-engine"
    assert handle["idempotency_key"].startswith("exec-handle-affinity-execution-a1")
    assert len(provision_specs) == 1
    assert provision_specs[0].idempotency_key == "exec-handle-affinity-execution-a1"
    assert provision_specs[0].target_environment is None
    retained_provider_handles[0].external_run_id = "mutated-after-return"
    retained_provider_handles[0].extras["late"] = "mutation"
    assert handle["external_run_id"] != "mutated-after-return"
    assert "late" not in handle["extras"]


def test_engine_resolution_verifies_selector_and_overlays_stored_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    class Resolver:
        async def resolve_with_connection_id(self, kind: Any, **kwargs: Any) -> tuple[Any, str]:
            calls.append({"kind": kind, **kwargs})
            return sentinel, "lre-a"

    monkeypatch.setattr(execution_phase, "_make_resolver", Resolver)
    cfg = execution_phase.PipelineConfigurable(
        project_id="project-a",
        engine="loadrunner",
        connections={"execution_engine": "lre-a"},
    )
    resolved = asyncio.run(execution_phase._resolve_engine(cfg, {"test_id": 42}))

    assert resolved is sentinel
    assert calls == [
        {
            "kind": PortKind.EXECUTION_ENGINE,
            "connection_id": "lre-a",
            "project_id": "project-a",
            "expected_provider": "loadrunner",
            "options_overlay": {"test_id": 42},
        }
    ]


async def test_throwaway_resolution_closes_leaf_and_resolver_owned_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    resolvers: list[Any] = []

    class Leaf:
        async def aclose(self) -> None:
            closed.append(f"leaf-{len(closed)}")

    class Resolver:
        def __init__(self) -> None:
            self.ordinal = len(resolvers)
            resolvers.append(self)

        async def resolve_with_metadata(self, kind: PortKind, **_kwargs: Any) -> ResolvedAdapter:
            return ResolvedAdapter(
                adapter=Leaf(),
                connection_id=f"connection-{self.ordinal}-{kind.value}",
                connection_version=None,
                persisted=False,
            )

        async def close(self) -> None:
            closed.append(f"resolver-{self.ordinal}")

    monkeypatch.setattr(execution_phase, "_make_resolver", Resolver)
    cfg = PipelineConfigurable(project_id="project-a", engine="sim")

    engine = await execution_phase._resolve_engine(cfg, {})
    store = await execution_phase._resolve_artifact_store(cfg)
    await execution_phase._close_resources_definitively(engine, store)

    assert len(resolvers) == 2
    assert sum(item.startswith("leaf-") for item in closed) == 2
    assert {item for item in closed if item.startswith("resolver-")} == {
        "resolver-0",
        "resolver-1",
    }


async def test_failed_adapter_build_closes_resolver_owned_nested_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Resolver:
        async def resolve_with_metadata(self, *_args: Any, **_kwargs: Any) -> ResolvedAdapter:
            raise RuntimeError("provider build failed after nested secret resolution")

        async def close(self) -> None:
            closed.append("resolver")

    monkeypatch.setattr(execution_phase, "_make_resolver", Resolver)

    with pytest.raises(RuntimeError, match="provider build failed"):
        await execution_phase._resolve_engine(PipelineConfigurable(engine="sim"), {})

    assert closed == ["resolver"]


async def test_cancelled_resolution_waits_for_resolver_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_started = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    closed: list[str] = []

    class Resolver:
        async def resolve_with_metadata(self, *_args: Any, **_kwargs: Any) -> ResolvedAdapter:
            resolve_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            closed.append("resolver")

    monkeypatch.setattr(execution_phase, "_make_resolver", Resolver)
    task = asyncio.create_task(
        execution_phase._resolve_engine(PipelineConfigurable(engine="sim"), {})
    )
    await resolve_started.wait()
    task.cancel()
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == ["resolver"]


def test_start_checkpoints_handle_mutations_before_initial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_status_handle: list[EngineHandle] = []

    class MutatingEngine:
        async def start(self, handle: EngineHandle) -> None:
            handle.engine = "attacker-engine"
            handle.connection_id = "attacker-connection"
            handle.idempotency_key = "attacker-key"
            handle.external_run_id = "lre-1042"
            handle.extras["run_id"] = "1042"

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            seen_status_handle.append(handle)
            assert handle.extras["run_id"] == "1042"
            return EngineRunStatus(phase=EngineRunPhase.RUNNING)

    engine = MutatingEngine()

    async def resolve(*args: Any, **kwargs: Any) -> MutatingEngine:
        return engine

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="lre-a",
        idempotency_key="thread-a-execution-a1",
        extras={"test_id": "88"},
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_options": {},
                }
            },
            "engine_handle": handle.model_dump(mode="json"),
        },
    )
    cfg = exec_config("durable-handle")
    started = execution_phase.engine_start(state, cfg)
    assert isinstance(started.update, dict)
    checkpointed = started.update["engine_handle"]
    assert checkpointed["external_run_id"] == "lre-1042"
    assert checkpointed["extras"]["run_id"] == "1042"
    assert checkpointed["engine"] == "loadrunner"
    assert checkpointed["connection_id"] == "lre-a"
    assert checkpointed["idempotency_key"] == "thread-a-execution-a1"

    base_execution = (state.get("phase_results") or {})["execution"]
    status_state = cast(
        PipelineState,
        {
            **state,
            "engine_handle": checkpointed,
            "phase_results": {
                "execution": {
                    **base_execution,
                    **started.update["phase_results"]["execution"],
                }
            },
        },
    )
    status = execution_phase.engine_status(status_state, cfg)
    assert status.goto == "engine_poll"
    assert seen_status_handle[0].external_run_id == "lre-1042"


def test_start_exception_rejects_invalid_provider_handle_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidMutatingEngine:
        async def start(self, handle: EngineHandle) -> None:
            handle.external_run_id = "invalid\x00run"
            handle.extras["run_id"] = "x" * 10_000
            raise OSError("start acknowledgement lost")

    async def resolve(*_args: Any, **_kwargs: Any) -> InvalidMutatingEngine:
        return InvalidMutatingEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="trusted-reservation",
        idempotency_key="invalid-start-execution-a1",
    )
    trusted = handle.model_dump(mode="json")
    state = cast(
        PipelineState,
        {
            "engine_handle": trusted,
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": trusted,
                    "engine_options": {},
                }
            },
        },
    )

    command = execution_phase.engine_start(state, exec_config("invalid-start"))

    assert command.goto == "engine_cleanup"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert entry["engine_handle"] == trusted
    assert "invalid start handle" in entry["engine_cleanup_reason"]
    assert "\x00" not in entry["engine_cleanup_reason"]


def test_credential_bearing_start_handle_is_compensated_without_durable_leak(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    canary = "unsafe-start-handle-secret-canary"
    calls: list[str] = []

    class UnsafeMutatingEngine:
        async def start(self, handle: EngineHandle) -> None:
            calls.append("start")
            handle.external_run_id = f"Authorization: Bearer {canary}"

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            assert canary in str(handle.external_run_id)
            assert reason == "provider returned an unsafe durable handle"
            calls.append("abort")

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            assert canary in str(handle.external_run_id)
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.ABORTED)

        async def teardown(self, handle: EngineHandle) -> None:
            assert canary in str(handle.external_run_id)
            calls.append("teardown")

    async def resolve(*_args: Any, **_kwargs: Any) -> UnsafeMutatingEngine:
        return UnsafeMutatingEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="trusted-reservation",
        idempotency_key="unsafe-start-execution-a1",
    )
    trusted = handle.model_dump(mode="json")
    state = cast(
        PipelineState,
        {
            "engine_handle": trusted,
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": trusted,
                    "engine_options": {},
                }
            },
        },
    )

    command = execution_phase.engine_start(state, exec_config("unsafe-start"))

    assert command.goto == "finalize"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert entry["status"] == PhaseStatus.FAILED.value
    assert entry["engine_handle"] == trusted
    assert entry["engine_cleanup_required"] is False
    assert calls == ["start", "abort", "status", "teardown"]
    assert projection_calls[-1]["status"] == EngineRunPhase.FAILED.value
    assert canary not in repr(command.update)


def test_start_terminal_projection_race_is_rejected_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []

    class StartSpy:
        async def start(self, handle: EngineHandle) -> None:
            provider_calls.append("start")

    async def resolve(*_args: Any, **_kwargs: Any) -> StartSpy:
        return StartSpy()

    def reject_projection(*_args: Any, **_kwargs: Any) -> None:
        raise engine_runs.EngineRunReservationRejectedError(
            "terminal attempt already owns the projection"
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "prepare_engine_start_sync", reject_projection)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="terminal-race-remote",
        idempotency_key="terminal-race-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_connection_id": handle.connection_id,
                    "engine_connection_version": None,
                    "engine_connection_persisted": False,
                    "engine_connection_affinity_staged": True,
                }
            },
        },
    )

    with pytest.raises(
        engine_runs.EngineRunReservationRejectedError,
        match="terminal attempt already owns",
    ):
        execution_phase.engine_start(state, exec_config("terminal-race"))

    assert provider_calls == []


def test_start_recovers_committed_running_handle_without_reissuing_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []

    class StartSpy:
        async def start(self, handle: EngineHandle) -> None:
            provider_calls.append("start")

    async def resolve(*_args: Any, **_kwargs: Any) -> StartSpy:
        return StartSpy()

    def recover(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "engine": "attacker-engine",
            "connection_id": "attacker-connection",
            "external_run_id": "recovered-provider-run",
            "idempotency_key": "attacker-key",
            "extras": {"run_id": "1042"},
        }

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    monkeypatch.setattr(engine_runs, "prepare_engine_start_sync", recover)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="lre-a",
        external_run_id=None,
        idempotency_key="start-recovery-execution-a1",
        extras={"test_id": "88"},
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_connection_id": handle.connection_id,
                    "engine_connection_version": None,
                    "engine_connection_persisted": False,
                    "engine_connection_affinity_staged": True,
                }
            },
        },
    )

    command = execution_phase.engine_start(state, exec_config("start-recovery"))

    assert command.goto == "engine_status"
    assert command.update is not None
    recovered = command.update["engine_handle"]
    assert recovered == {
        "engine": "loadrunner",
        "connection_id": "lre-a",
        "external_run_id": "recovered-provider-run",
        "idempotency_key": "start-recovery-execution-a1",
        "extras": {"run_id": "1042"},
    }
    assert provider_calls == []


@pytest.mark.parametrize(
    "node",
    ["start", "status", "poll", "cleanup", "collect", "settle"],
)
def test_locked_legacy_recovery_nodes_reject_missing_execution_affinity_before_io(
    monkeypatch: pytest.MonkeyPatch,
    node: str,
) -> None:
    monkeypatch.setattr(
        execution_phase,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    resolution_calls: list[str] = []

    async def forbidden_engine_resolution(*_args: Any, **_kwargs: Any) -> Any:
        resolution_calls.append("execution")
        raise AssertionError("execution provider must not be resolved")

    async def forbidden_artifact_resolution(*_args: Any, **_kwargs: Any) -> Any:
        resolution_calls.append("artifact")
        raise AssertionError("artifact provider must not be resolved")

    monkeypatch.setattr(execution_phase, "_resolve_engine", forbidden_engine_resolution)
    monkeypatch.setattr(
        execution_phase,
        "_resolve_artifact_store",
        forbidden_artifact_resolution,
    )
    handle = EngineHandle(
        engine="sim",
        # A row id without its checkpointed runtime generation is still unsafe:
        # the row may now point at another provider endpoint.
        connection_id="legacy-engine-connection",
        external_run_id="42",
        idempotency_key="legacy-affinity-execution-a1",
    )
    handle_json = handle.model_dump(mode="json")
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.RUNNING.value,
        "engine_handle": handle_json,
        "engine_options": {},
        "engine_poll_last": {"status": EngineRunPhase.COMPLETED.value},
        "engine_cleanup_required": True,
        "engine_cleanup_reason": "operator cleanup",
        "engine_cleanup_final_error": "cleanup required",
        "artifact_store_connection_id": "dev-artifact-store-memory",
        "artifact_store_connection_persisted": False,
        "engine_collection_staged": True,
        "engine_collection_projected_phase": EngineRunPhase.COMPLETED.value,
        "engine_collection_final_status": None,
        "engine_collection_next": "open_output_gate",
        "test_summary": EngineTestResultSummary(engine="sim", passed=True).model_dump(mode="json"),
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle_json,
            "phase_results": {"execution": entry},
        },
    )
    config = exec_config(f"legacy-affinity-{node}")

    with pytest.raises(EngineConnectionAffinityMissingError, match="out of band"):
        if node == "start":
            execution_phase.engine_start(state, config)
        elif node == "status":
            execution_phase.engine_status(state, config)
        elif node == "poll":
            execution_phase.engine_poll(state, config)
        elif node == "cleanup":
            execution_phase.engine_cleanup(state, config)
        elif node == "collect":
            execution_phase.engine_collect(state, config)
        else:
            execution_phase.engine_collection_settle(state, config)

    assert resolution_calls == []


def test_locked_runtime_rejects_staged_unversioned_affinities_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_phase,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=True),
    )
    handle = EngineHandle(
        engine="sim",
        connection_id="engine-connection",
        external_run_id="42",
        idempotency_key="locked-unversioned-execution-a1",
    )
    entry = {
        "engine_connection_affinity_staged": True,
        "engine_connection_id": "engine-connection",
        "engine_connection_persisted": False,
        "engine_connection_version": None,
        "artifact_store_connection_persisted": False,
        "artifact_store_connection_version": None,
    }

    with pytest.raises(EngineConnectionAffinityMissingError, match="out of band"):
        execution_phase._connection_reservation_affinity(entry, handle)
    with pytest.raises(RuntimeError, match="no durable connection generation"):
        execution_phase._artifact_reservation_affinity(entry, "artifact-connection")


@pytest.mark.parametrize("artifact", [False, True])
def test_checkpointed_connection_versions_never_retain_credentials_on_error(
    artifact: bool,
) -> None:
    canary = "connection-version-secret-canary"
    raw_version = f"api_key={canary}"

    if artifact:

        def check() -> object:
            return execution_phase._artifact_reservation_affinity(
                {
                    "artifact_store_connection_persisted": True,
                    "artifact_store_connection_version": raw_version,
                },
                "artifact-store-a",
            )
    else:
        handle = EngineHandle(
            engine="sim",
            connection_id="engine-a",
            external_run_id="run-a",
            idempotency_key="affinity-secret-test",
        )

        def check() -> object:
            return execution_phase._connection_reservation_affinity(
                {
                    "engine_connection_affinity_staged": True,
                    "engine_connection_id": "engine-a",
                    "engine_connection_persisted": True,
                    "engine_connection_version": raw_version,
                },
                handle,
            )

    with pytest.raises(RuntimeError) as raised:
        check()

    current: BaseException | None = raised.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert canary not in repr(current)
        current = current.__cause__ or current.__context__


def test_execution_resume_router_rejects_unselected_poisoned_phase() -> None:
    durable = PipelineConfigurable(phases=[Phase.STORY_ANALYSIS])
    state = cast(
        PipelineState,
        {
            "run_config": durable.snapshot(),
            "phases_plan": [Phase.EXECUTION.value],
            "phase_results": {"execution": {"attempt": 1}},
        },
    )

    with pytest.raises(ValueError, match="phase plan does not match"):
        execution_phase.route_execution_entry(
            state,
            {
                "configurable": {
                    "thread_id": "unselected-execution-phase",
                    **durable.snapshot(),
                }
            },
        )


def test_unlocked_legacy_status_keeps_static_default_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_phase,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=False),
    )
    calls: list[str] = []

    class StaticEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.RUNNING)

    async def resolve(*_args: Any, **_kwargs: Any) -> StaticEngine:
        calls.append("resolve")
        return StaticEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id=None,
        external_run_id="sim-legacy",
        idempotency_key="legacy-dev-execution-a1",
    )
    handle_json = handle.model_dump(mode="json")
    state = cast(
        PipelineState,
        {
            "engine_handle": handle_json,
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle_json,
                    "engine_options": {},
                }
            },
        },
    )

    command = execution_phase.engine_status(state, exec_config("legacy-dev-status"))

    assert command.goto == "engine_poll"
    assert calls == ["resolve", "status"]


def test_status_rejects_connection_generation_drift_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_version = datetime(2026, 7, 1, tzinfo=UTC)
    changed_version = expected_version + timedelta(seconds=1)
    calls: list[str] = []

    class DriftedEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.RUNNING)

        async def aclose(self) -> None:
            calls.append("close")

    async def resolve(*_args: Any, **_kwargs: Any) -> ResolvedAdapter:
        return ResolvedAdapter(
            adapter=DriftedEngine(),
            connection_id="engine-connection",
            connection_version=changed_version,
            persisted=True,
        )

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="engine-connection",
        external_run_id="sim-generation-fence",
        idempotency_key="generation-fence-execution-a1",
    )
    handle_json = handle.model_dump(mode="json")
    state = cast(
        PipelineState,
        {
            "engine_handle": handle_json,
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle_json,
                    "engine_options": {},
                    "engine_connection_id": handle.connection_id,
                    "engine_connection_version": expected_version.isoformat(),
                    "engine_connection_persisted": True,
                    "engine_connection_affinity_staged": True,
                }
            },
        },
    )

    command = execution_phase.engine_status(state, exec_config("generation-fence-status"))

    assert command.goto == "engine_poll"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert "changed after its checkpointed affinity reservation" in entry["engine_poll_error_last"]
    assert calls == ["close"]


def test_initial_status_transient_error_resumes_bounded_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FlakyStatusEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            if len(calls) == 1:
                raise OSError("temporary status disconnect")
            return EngineRunStatus(phase=EngineRunPhase.RUNNING)

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            calls.append("abort")

        async def teardown(self, handle: EngineHandle) -> None:
            calls.append("teardown")

    engine = FlakyStatusEngine()

    async def resolve(*args: Any, **kwargs: Any) -> FlakyStatusEngine:
        return engine

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-transient",
        idempotency_key="transient-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_options": {},
                }
            },
        },
    )
    cfg = exec_config("transient-status", limits={"poll_interval_s": 0.01})

    initial = execution_phase.engine_status(state, cfg)
    assert initial.goto == "engine_poll"
    assert isinstance(initial.update, dict)
    initial_entry = initial.update["phase_results"]["execution"]
    assert initial_entry["engine_poll_errors"] == 1
    assert "temporary status disconnect" in initial_entry["engine_poll_error_last"]
    assert calls == ["status"]

    base_execution = (state.get("phase_results") or {})["execution"]
    poll_state = cast(
        PipelineState,
        {
            **state,
            "phase_results": {
                "execution": {
                    **base_execution,
                    **initial_entry,
                }
            },
        },
    )
    recovered = execution_phase.engine_poll(poll_state, cfg)
    assert recovered.goto == "engine_poll"
    assert isinstance(recovered.update, dict)
    assert recovered.update["phase_results"]["execution"]["engine_poll_errors"] == 0
    assert calls == ["status", "status"]
    assert "abort" not in calls
    assert "teardown" not in calls


def test_poll_failure_cleanup_is_checkpointed_and_retried_until_abort_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    calls: list[str] = []

    class RecoveringCleanupEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            if calls.count("abort") >= 2:
                return EngineRunStatus(phase=EngineRunPhase.ABORTED)
            raise OSError("provider status API unavailable")

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            calls.append("abort")
            if calls.count("abort") == 1:
                raise OSError("provider abort API unavailable")

        async def teardown(self, handle: EngineHandle) -> None:
            calls.append("teardown")

    engine = RecoveringCleanupEngine()

    async def resolve(*args: Any, **kwargs: Any) -> RecoveringCleanupEngine:
        return engine

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-cleanup-retry",
        idempotency_key="cleanup-retry-execution-a1",
    )
    base_entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.RUNNING.value,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_options": {},
        "engine_poll_errors": execution_phase.MAX_CONSECUTIVE_POLL_ERRORS - 1,
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": base_entry},
        },
    )
    cfg = exec_config("cleanup-retry", limits={"poll_interval_s": 0.01})

    scheduled = execution_phase.engine_poll(state, cfg)
    assert scheduled.goto == "engine_cleanup"
    assert isinstance(scheduled.update, dict)
    scheduled_entry = scheduled.update["phase_results"]["execution"]
    assert scheduled_entry["engine_cleanup_required"] is True
    assert "errors" not in scheduled_entry
    assert projection_calls == []

    cleanup_entry = {**base_entry, **scheduled_entry}
    cleanup_state = cast(
        PipelineState,
        {
            **state,
            "phase_results": {"execution": cleanup_entry},
        },
    )
    retry = execution_phase.engine_cleanup(cleanup_state, cfg)
    assert retry.goto == "engine_cleanup"
    assert isinstance(retry.update, dict)
    retry_entry = retry.update["phase_results"]["execution"]
    assert retry_entry["status"] == PhaseStatus.RUNNING.value
    assert retry_entry["engine_cleanup_required"] is True
    assert retry_entry["engine_cleanup_failures"] == 1
    assert "abort API unavailable" in retry_entry["engine_cleanup_last_error"]
    assert "teardown" not in calls
    assert projection_calls == []

    completed_state = cast(
        PipelineState,
        {
            **cleanup_state,
            "phase_results": {"execution": {**cleanup_entry, **retry_entry}},
        },
    )
    completed = execution_phase.engine_cleanup(completed_state, cfg)
    assert completed.goto == "finalize"
    assert isinstance(completed.update, dict)
    completed_entry = completed.update["phase_results"]["execution"]
    assert completed_entry["status"] == PhaseStatus.FAILED.value
    assert completed_entry["engine_cleanup_required"] is False
    assert "abort confirmed" in completed_entry["errors"][0]
    assert calls == ["status", "abort", "abort", "status", "teardown"]
    assert [call["status"] for call in projection_calls] == ["aborted"]


def test_cleanup_retries_teardown_before_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    calls: list[str] = []

    class RecoveringTeardownEngine:
        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            calls.append("abort")

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            calls.append("status")
            return EngineRunStatus(phase=EngineRunPhase.ABORTED)

        async def teardown(self, handle: EngineHandle) -> None:
            calls.append("teardown")
            if calls.count("teardown") == 1:
                raise OSError("provider teardown unavailable")

    async def resolve(*_args: Any, **_kwargs: Any) -> RecoveringTeardownEngine:
        return RecoveringTeardownEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="cleanup-teardown-remote",
        idempotency_key="cleanup-teardown-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.RUNNING.value,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_cleanup_required": True,
        "engine_cleanup_reason": "poll timeout",
        "engine_cleanup_final_error": "poll timeout",
        "engine_cleanup_failures": 0,
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )
    config = exec_config("cleanup-teardown", limits={"poll_interval_s": 0.01})

    retry = execution_phase.engine_cleanup(state, config)

    assert retry.goto == "engine_cleanup"
    assert retry.update is not None
    retry_entry = retry.update["phase_results"]["execution"]
    assert retry_entry["engine_cleanup_required"] is True
    assert retry_entry["engine_cleanup_failures"] == 1
    assert "teardown unavailable" in retry_entry["engine_cleanup_last_error"]
    assert projection_calls == []
    entry.update(retry_entry)

    completed = execution_phase.engine_cleanup(state, config)

    assert completed.goto == "finalize"
    assert completed.update is not None
    assert completed.update["phase_results"]["execution"]["engine_cleanup_required"] is False
    assert calls == ["abort", "status", "teardown", "abort", "status", "teardown"]
    assert [call["status"] for call in projection_calls] == [EngineRunPhase.ABORTED.value]


def test_cleanup_exhaustion_checkpoints_a_publicly_resumable_retry_gate(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    class UnavailableCleanupEngine:
        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            del handle, reason
            raise OSError("provider abort unavailable")

    async def resolve(*_args: Any, **_kwargs: Any) -> UnavailableCleanupEngine:
        return UnavailableCleanupEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="cleanup-blocked-remote",
        idempotency_key="cleanup-blocked-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "status": PhaseStatus.RUNNING.value,
        "engine_handle": handle.model_dump(mode="json"),
        "engine_options": {},
        "engine_cleanup_required": True,
        "engine_cleanup_reason": "poll timeout",
        "engine_cleanup_final_error": "poll timeout",
        # poll_timeout / poll_interval is two; cleanup's minimum budget is three.
        "engine_cleanup_failures": 2,
    }
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )
    config = exec_config(
        "cleanup-blocked",
        limits={"poll_interval_s": 0.01, "poll_timeout_s": 0.02},
    )

    blocked = execution_phase.engine_cleanup(state, config)

    assert blocked.goto == "engine_cleanup_blocked"
    assert blocked.update is not None
    blocked_entry = blocked.update["phase_results"]["execution"]
    assert blocked_entry["engine_cleanup_required"] is True
    assert blocked_entry["engine_cleanup_blocked"] is True
    assert blocked_entry["engine_cleanup_failures"] == 3
    assert projection_calls == []

    payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(execution_phase, "interrupt", lambda payload: payloads.append(payload))
    entry.update(blocked_entry)
    resumed = execution_phase.engine_cleanup_blocked(state, config)

    assert payloads == [
        {
            "schema_version": execution_phase.EVENT_SCHEMA_VERSION,
            "kind": "engine_cleanup_retry",
            "phase": "execution",
            "attempt": 1,
            "thread_id": "cleanup-blocked",
            "actions": ["retry"],
            "error": "OSError: provider abort unavailable",
            "message": (
                "Engine abort confirmation exhausted its retry budget. The exact "
                "provider handle remains durable; resume to retry abort and teardown."
            ),
        }
    ]
    assert resumed.goto == "engine_cleanup"
    assert resumed.update is not None
    resumed_entry = resumed.update["phase_results"]["execution"]
    assert resumed_entry["engine_cleanup_blocked"] is False
    assert resumed_entry["engine_cleanup_failures"] == 0


def test_cleanup_preserves_trusted_handle_identity_across_provider_mutation(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    seen_status: list[EngineHandle] = []
    seen_teardown: list[EngineHandle] = []

    class MutatingAbortEngine:
        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            handle.engine = "attacker-engine"
            handle.connection_id = "attacker-connection"
            handle.external_run_id = "attacker-run"
            handle.idempotency_key = "attacker-key"
            handle.extras["aborted"] = "true"

        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            seen_status.append(handle.model_copy(deep=True))
            assert handle.extras["aborted"] == "true"
            handle.engine = "status-attacker-engine"
            handle.connection_id = "status-attacker-connection"
            handle.external_run_id = "status-attacker-run"
            handle.idempotency_key = "status-attacker-key"
            handle.extras["aborted"] = "status-attacker"
            return EngineRunStatus(phase=EngineRunPhase.ABORTED)

        async def teardown(self, handle: EngineHandle) -> None:
            seen_teardown.append(handle.model_copy(deep=True))

    async def resolve(*_args: Any, **_kwargs: Any) -> MutatingAbortEngine:
        return MutatingAbortEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="trusted-run",
        idempotency_key="trusted-cleanup-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_cleanup_required": True,
                    "engine_cleanup_reason": "operator cleanup",
                }
            },
        },
    )

    command = execution_phase.engine_cleanup(state, exec_config("trusted-cleanup"))

    assert command.goto == "finalize"
    assert len(seen_status) == len(seen_teardown) == 1
    for observed in (*seen_status, *seen_teardown):
        assert observed.engine == handle.engine
        assert observed.connection_id == handle.connection_id
        assert observed.external_run_id == handle.external_run_id
        assert observed.idempotency_key == handle.idempotency_key
        assert observed.extras["aborted"] == "true"
    assert len(projection_calls) == 1
    projected = projection_calls[0]
    assert projected["engine"] == handle.engine
    assert projected["handle"] == handle.model_dump(mode="json")
    assert projected["external_run_id"] == handle.external_run_id


def test_start_failure_checkpoints_cleanup_before_attempting_abort(
    monkeypatch: pytest.MonkeyPatch,
    projection_calls: list[dict[str, Any]],
) -> None:
    calls: list[str] = []

    class AmbiguousStartEngine:
        async def start(self, handle: EngineHandle) -> None:
            calls.append("start")
            handle.external_run_id = "remote-created-before-disconnect"
            raise OSError("start response lost")

        async def abort(self, handle: EngineHandle, *, reason: str) -> None:
            calls.append("abort")

    engine = AmbiguousStartEngine()

    async def resolve(*args: Any, **kwargs: Any) -> AmbiguousStartEngine:
        return engine

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="reserved-run",
        idempotency_key="start-cleanup-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_options": {},
                }
            },
        },
    )

    scheduled = execution_phase.engine_start(state, exec_config("start-cleanup"))

    assert scheduled.goto == "engine_cleanup"
    assert isinstance(scheduled.update, dict)
    entry = scheduled.update["phase_results"]["execution"]
    assert entry["engine_cleanup_required"] is True
    assert entry["engine_handle"]["external_run_id"] == "remote-created-before-disconnect"
    assert "errors" not in entry
    assert calls == ["start"]
    assert projection_calls == []


def test_cleanup_required_checkpoint_reenters_cleanup_before_prepare() -> None:
    cleanup_state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "status": PhaseStatus.RUNNING.value,
                    "engine_cleanup_required": True,
                }
            }
        },
    )

    assert execution_phase.route_execution_entry(cleanup_state) == "engine_cleanup"
    assert execution_phase.route_execution_entry(cast(PipelineState, {})) == "prepare"


def test_failed_run_collection_errors_checkpoint_retry_without_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FailedCollector:
        async def collect_artifacts(self, handle: Any, store: Any) -> Any:
            calls.append("collect")
            raise KeyError("no results")

        async def fetch_summary(self, handle: Any) -> Any:
            calls.append("summary")
            raise RuntimeError("summary unavailable")

        async def teardown(self, handle: Any) -> None:
            calls.append("teardown")

    async def resolve(*args: Any, **kwargs: Any) -> FailedCollector:
        return FailedCollector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="loadrunner",
        connection_id="lre-a",
        external_run_id="lre-42",
        idempotency_key="thread-a-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": "failed", "message": "collation failed"},
                    "artifact_store_connection_id": "dev-artifact-store-memory",
                }
            },
        },
    )
    command = execution_phase.engine_collect(state, exec_config("failed-collect"))
    assert command.goto == "engine_collect"
    assert isinstance(command.update, dict)
    entry = command.update["phase_results"]["execution"]
    assert entry["status"] == "running"
    assert entry["engine_collection_required"] is True
    assert entry["engine_collection_failures"] == 1
    assert "no results" in entry["engine_collection_last_error"]
    assert calls == ["collect"]


@pytest.mark.parametrize(
    ("poll_last", "expected_error"),
    [
        (None, "terminal engine status is missing or malformed"),
        ({"status": "running"}, "requires a confirmed terminal engine status"),
    ],
)
def test_collection_fails_closed_without_confirmed_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
    poll_last: dict[str, Any] | None,
    expected_error: str,
) -> None:
    calls: list[str] = []

    class Collector:
        async def collect_artifacts(self, handle: Any, store: Any) -> list[Any]:
            calls.append("collect")
            return []

        async def fetch_summary(self, handle: Any) -> EngineTestResultSummary:
            calls.append("summary")
            return EngineTestResultSummary(engine="sim", passed=True)

        async def teardown(self, handle: Any) -> None:
            calls.append("teardown")

    async def resolve(*args: Any, **kwargs: Any) -> Collector:
        return Collector()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-state-check",
        idempotency_key="state-check-execution-a1",
    )
    entry: dict[str, Any] = {
        "attempt": 1,
        "engine_handle": handle.model_dump(mode="json"),
    }
    if poll_last is not None:
        entry["engine_poll_last"] = poll_last
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {"execution": entry},
        },
    )

    command = execution_phase.engine_collect(state, exec_config("collect-state-check"))

    assert command.goto == "engine_cleanup"
    assert isinstance(command.update, dict)
    result = command.update["phase_results"]["execution"]
    assert result["status"] == "running"
    assert result["engine_cleanup_required"] is True
    assert expected_error in result["engine_cleanup_reason"]
    assert calls == []


def test_collection_never_stringifies_malformed_checkpoint_status() -> None:
    class HostileStatus:
        called = False

        def __str__(self) -> str:
            self.called = True
            raise AssertionError("checkpoint status must not be stringified")

        def __repr__(self) -> str:
            self.called = True
            raise AssertionError("checkpoint status must not be reflected")

    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-malformed-status",
        idempotency_key="malformed-status-execution-a1",
    )
    status = HostileStatus()
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_last": {"status": status},
                }
            },
        },
    )

    command = execution_phase.engine_collect(state, exec_config("malformed-status"))

    assert command.goto == "engine_cleanup"
    assert status.called is False


def test_elapsed_time_ignores_string_subclasses_without_invoking_them() -> None:
    class HostileTimestamp(str):
        def __bool__(self) -> bool:
            raise AssertionError("timestamp truthiness must not execute")

    assert execution_phase._elapsed_s(HostileTimestamp("2026-01-01T00:00:00+00:00")) is None


def test_checkpoint_spec_rejects_coercion_hooks_and_credentials_before_replay() -> None:
    calls: list[str] = []

    class FloatBomb:
        def __float__(self) -> float:
            calls.append("float")
            raise AssertionError("checkpoint numeric coercion must not execute")

    base = {
        "idempotency_key": "checkpoint-spec-execution-a1",
        "title": "checkpoint spec",
        "vusers": 1,
        "ramp_s": 0.0,
        "duration_s": 1.0,
    }
    with pytest.raises(ValueError, match="load-test spec"):
        execution_phase._validated_checkpoint_spec({**base, "ramp_s": FloatBomb()})
    with pytest.raises(ValueError, match="credential material") as raised:
        execution_phase._validated_checkpoint_spec(
            {**base, "title": "Authorization: Bearer spec-secret-canary"}
        )

    assert calls == []
    assert "spec-secret-canary" not in str(raised.value)


def test_execution_route_rejects_malformed_flags_without_truthiness_hooks() -> None:
    calls: list[str] = []

    class BoolBomb:
        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("checkpoint flag truthiness must not execute")

    for value in (BoolBomb(), "false"):
        state = cast(
            PipelineState,
            {"phase_results": {"execution": {"engine_cleanup_required": value}}},
        )
        with pytest.raises(ValueError, match="flag"):
            execution_phase.route_execution_entry(state)
    assert calls == []


def test_collection_settle_validates_continuation_before_comparison() -> None:
    calls: list[str] = []

    class EqualityBomb:
        def __eq__(self, _other: Any) -> bool:
            calls.append("eq")
            raise AssertionError("checkpoint continuation must not be compared")

    state, entry = _staged_completed_collection_state("settle-continuation-boundary")
    entry["engine_collection_next"] = EqualityBomb()

    with pytest.raises(RuntimeError, match="continuation"):
        execution_phase.engine_collection_settle(
            state,
            exec_config("settle-continuation-boundary"),
        )
    assert calls == []


def test_checkpoint_started_timestamp_is_replaced_without_hooks_or_credentials() -> None:
    calls: list[str] = []

    class TimestampBomb(str):
        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("checkpoint timestamp truthiness must not execute")

    hostile = execution_phase._checkpoint_timestamp_or_now(
        TimestampBomb("2026-01-01T00:00:00+00:00")
    )
    credential = execution_phase._checkpoint_timestamp_or_now(
        "Authorization: Bearer timestamp-secret-canary"
    )

    assert calls == []
    assert hostile != "2026-01-01T00:00:00+00:00"
    assert "timestamp-secret-canary" not in credential
    datetime.fromisoformat(hostile)
    datetime.fromisoformat(credential)


def test_replayed_engine_options_enforce_provider_allowlist_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fail_resolve(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("invalid option replay must not resolve a provider")

    monkeypatch.setattr(execution_phase, "_resolve_engine", fail_resolve)
    cfg = exec_config("option-scope-replay")
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(RunnableConfig, {**cfg, "configurable": {**configurable, "engine": "loadrunner"}})
    spec = LoadTestSpec(
        idempotency_key="option-scope-replay-execution-a1",
        title="option replay",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "load_test_spec": spec.model_dump(mode="json"),
                    "engine_options": {"project": "other-provider-scope"},
                }
            }
        },
    )

    with pytest.raises(ValueError, match="unsupported load_test engine option"):
        execution_phase.engine_provision(state, cfg)
    assert called is False


def test_invalid_checkpoint_connection_id_is_cleared_before_retry_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fail_resolve(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("unsafe connection id must not reach resolution")

    monkeypatch.setattr(execution_phase, "_resolve_engine", fail_resolve)
    spec = LoadTestSpec(
        idempotency_key="unsafe-connection-execution-a1",
        title="unsafe connection",
        vusers=1,
        ramp_s=0,
        duration_s=1,
    )
    old_entry: dict[str, Any] = {
        "attempt": 1,
        "load_test_spec": spec.model_dump(mode="json"),
        "engine_options": {},
        "engine_connection_id": "password=connection-secret-canary",
        "engine_connection_version": "private_key=version-secret-canary",
        "engine_connection_persisted": True,
        "engine_connection_affinity_staged": True,
    }
    phase_results = {"execution": old_entry}
    state = cast(PipelineState, {"phase_results": phase_results})

    command = execution_phase.engine_provision(
        state,
        exec_config("unsafe-connection"),
    )
    assert command.update is not None
    merged = merge_phase_results(
        phase_results,
        command.update["phase_results"],
    )["execution"]

    assert called is False
    assert merged["engine_connection_id"] is None
    assert merged["engine_connection_version"] is None
    assert merged["engine_connection_persisted"] is False
    assert merged["engine_connection_affinity_staged"] is False
    assert "connection-secret-canary" not in repr(command)
    assert "version-secret-canary" not in repr(command)


def test_collection_retry_boundary_validates_flags_and_clears_stale_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    payloads: list[dict[str, Any]] = []

    class BoolBomb:
        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("checkpoint diagnostic truthiness must not execute")

    monkeypatch.setattr(execution_phase, "interrupt", lambda payload: payloads.append(payload))
    phase_entry: dict[str, Any] = {
        "attempt": 1,
        "engine_collection_index_required": False,
        "engine_collection_last_error": BoolBomb(),
        "engine_collection_index_failures": 2,
        "engine_collection_index_last_error": "password=stale-secret-canary",
    }
    state = cast(
        PipelineState,
        {"phase_results": {"execution": phase_entry}},
    )

    command = execution_phase.engine_collection_blocked(
        state,
        exec_config("collection-retry-boundary"),
    )

    assert calls == []
    assert payloads[0]["error"] == "engine collection unavailable"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert entry["engine_collection_index_failures"] == 0
    assert entry["engine_collection_index_last_error"] is None
    assert "stale-secret-canary" not in repr(command)

    phase_entry["engine_collection_index_required"] = "false"
    with pytest.raises(ValueError, match="flag"):
        execution_phase.engine_collection_resume(
            state,
            exec_config("collection-retry-boundary"),
        )


def test_poll_timeout_aborts_engine_and_fails_phase(
    monkeypatch: pytest.MonkeyPatch, projection_calls: list[dict[str, Any]]
) -> None:
    calls = install_engine_spy(monkeypatch)
    g = compiled()
    cfg = exec_config(
        "exec-timeout",
        limits={"poll_interval_s": 0.02, "poll_timeout_s": 1e-6},
        load_test={"duration_s": 5.0},
    )
    result = g.invoke(public_inputs(), cfg)
    assert "__interrupt__" not in result

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    assert any("timed out" in error for error in entry["errors"])
    assert "abort" in calls
    assert "teardown" in calls
    assert "collect_artifacts" not in calls
    assert [c["status"] for c in projection_calls] == [
        "provisioning",
        "ready",
        "running",
        "aborted",
    ]


def test_poll_cycle_budget_routes_missing_timestamp_to_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForeverRunning:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            return EngineRunStatus(phase=EngineRunPhase.RUNNING)

    async def resolve(*args: Any, **kwargs: Any) -> ForeverRunning:
        return ForeverRunning()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-cycle-cap",
        idempotency_key="cycle-cap-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                    "engine_poll_count": 2,
                }
            },
        },
    )

    command = execution_phase.engine_poll(
        state,
        exec_config(
            "cycle-cap",
            limits={"poll_interval_s": 0.01, "poll_timeout_s": 0.03},
        ),
    )

    assert command.goto == "engine_cleanup"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert entry["engine_poll_count"] == 3
    assert entry["engine_cleanup_required"] is True
    assert "poll-cycle budget 3 exhausted" in entry["engine_cleanup_final_error"]


def test_elapsed_s_normalizes_legacy_naive_timestamp() -> None:
    elapsed = execution_phase._elapsed_s("2026-01-01T00:00:00")

    assert elapsed is not None
    assert elapsed >= 0


def test_poll_revalidates_nonfinite_adapter_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidStatusEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            return EngineRunStatus.model_construct(
                phase=EngineRunPhase.RUNNING,
                progress_pct=float("nan"),
            )

    async def resolve(*args: Any, **kwargs: Any) -> InvalidStatusEngine:
        return InvalidStatusEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-invalid-status",
        idempotency_key="invalid-status-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                }
            },
        },
    )

    command = execution_phase.engine_poll(state, exec_config("invalid-status"))

    assert command.goto == "engine_poll"
    assert command.update is not None
    entry = command.update["phase_results"]["execution"]
    assert entry["engine_poll_errors"] == 1
    assert "engine status is invalid" in entry["engine_poll_error_last"]


def test_initial_status_diagnostic_is_nul_safe_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoisyStatusEngine:
        async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
            raise OSError(
                "password=checkpoint-secret; Authorization: Bearer checkpoint-token; "
                + "x\x00" * 10_000
            )

    async def resolve(*_args: Any, **_kwargs: Any) -> NoisyStatusEngine:
        return NoisyStatusEngine()

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    handle = EngineHandle(
        engine="sim",
        connection_id="dev-engine-sim",
        external_run_id="sim-noisy-status",
        idempotency_key="noisy-status-execution-a1",
    )
    state = cast(
        PipelineState,
        {
            "engine_handle": handle.model_dump(mode="json"),
            "phase_results": {
                "execution": {
                    "attempt": 1,
                    "engine_handle": handle.model_dump(mode="json"),
                }
            },
        },
    )

    command = execution_phase.engine_status(state, exec_config("noisy-status"))

    assert command.update is not None
    diagnostic = command.update["phase_results"]["execution"]["engine_poll_error_last"]
    assert len(diagnostic) <= 4_096
    assert "\x00" not in diagnostic
    assert "\\0" in diagnostic
    assert "checkpoint-secret" not in diagnostic
    assert "checkpoint-token" not in diagnostic
    assert "[REDACTED]" in diagnostic


def test_gated_output_review_opens_after_collect_with_summary() -> None:
    g = compiled()
    cfg = exec_config(
        "exec-gated", gates={"execution": {"prompt_review": "auto", "output_review": "gated"}}
    )
    result = g.invoke(public_inputs(), cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "phase_review"
    assert payload["phase"] == "execution"
    assert "Engine run" in payload["summary"] and "KPIs" in payload["summary"]
    assert "engine_results" in [a["kind"] for a in payload["artifacts"]]

    # The gate opened only after the collection checkpoint and replay-safe settle.
    entry = subgraph_values(g, cfg)["phase_results"]["execution"]
    assert entry["status"] == PhaseStatus.AWAITING_OUTPUT_REVIEW
    assert entry["test_summary"]["passed"] is True
    assert entry["engine_poll_count"] >= 1
    assert entry["engine_collection_staged"] is False
    assert entry["engine_collection_settled"] is False
    assert entry["engine_collection_final_status"] is None
    assert entry["engine_collection_next"] is None

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert result["phase_results"]["execution"]["status"] == "succeeded"


def test_execution_output_revision_does_not_restart_external_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_engine_spy(monkeypatch)
    g = compiled()
    cfg = exec_config(
        "exec-revise",
        gates={"execution": {"prompt_review": "auto", "output_review": "gated"}},
    )
    result = g.invoke(public_inputs(), cfg)
    assert pending_interrupt(result)["kind"] == "phase_review"
    before = list(calls)

    result = g.invoke(
        Command(resume={"action": "revise", "instructions": "clarify the SLA verdict"}),
        cfg,
    )
    payload = pending_interrupt(result)
    assert "analysis revised per: clarify the SLA verdict" in payload["summary"]
    assert calls == before

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert result["phase_results"]["execution"]["status"] == "succeeded"
    assert calls == before


def test_reserve_falls_back_to_default_spec_without_upstream_spec() -> None:
    """Standalone execution run: script_scenario succeeded but left no spec."""
    entry = PhaseResult(phase=Phase.SCRIPT_SCENARIO, status=PhaseStatus.SUCCEEDED).as_state()
    state = cast(
        PipelineState,
        {"title": "Solo", "request": "r", "phase_results": {"script_scenario": entry}},
    )
    cfg = exec_config("exec-fallback", load_test={"duration_s": 0.1})
    command = execution_phase.engine_reserve(state, cfg)

    assert command.goto == "engine_provision"
    assert isinstance(command.update, dict)
    exec_entry = command.update["phase_results"]["execution"]
    spec = exec_entry["load_test_spec"]
    assert spec["idempotency_key"] == "exec-fallback-execution-a1"
    assert spec["vusers"] == 10
    assert spec["duration_s"] == 0.1
    assert spec["title"] == "Solo load test"
