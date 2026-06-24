"""Execution-phase engine spine: reserve/start/poll/collect against the sim engine.

All graphs compile with InMemorySaver. The connection resolver seam is pinned to
the static DEV_CONNECTIONS map (sim engine + in-memory artifact store) and the
engine_runs projection recorder is stubbed, so the suite needs no Postgres/MinIO.
Sim durations are tiny via the per-run "load_test" configurable override.
"""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import PHASE_ORDER, Phase, PhaseResult, PhaseStatus
from apex.graphs.pipeline import execution_phase
from apex.graphs.pipeline.graph import builder
from apex.graphs.pipeline.state import PipelineState
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver

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
    ) -> None:
        calls.append(
            {
                "thread_id": thread_id,
                "attempt": attempt,
                "engine": engine,
                "status": status,
                "external_run_id": external_run_id,
                "summary": summary,
                "project_id": project_id,
            }
        )

    monkeypatch.setattr(engine_runs, "record_engine_run_sync", record)
    return calls


class EngineSpy:
    """Delegating wrapper over a real sim engine that records method calls."""

    def __init__(self, inner: SimExecutionEngine, calls: list[str]) -> None:
        self._inner = inner
        self.calls = calls

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

    async def resolve(cfg: Any, engine_options: dict[str, Any]) -> EngineSpy:
        conn = ConnectionConfig(
            id="spy-engine",
            kind=PortKind.EXECUTION_ENGINE,
            provider="sim",
            name="Spy sim engine",
            options=dict(engine_options),
        )
        return EngineSpy(SimExecutionEngine(conn), calls)

    monkeypatch.setattr(execution_phase, "_resolve_engine", resolve)
    return calls


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
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "phases": phases or ["execution"],
        "gates": gates or {"execution": dict(AUTO)},
        "limits": {**FAST_LIMITS, **(limits or {})},
    }
    if load_test is not None:
        configurable["load_test"] = load_test
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
    assert handle["external_run_id"] in engine_artifacts[0]["uri"]

    reporting = result["phase_results"]["reporting"]["summary"]
    assert "KPIs:" in reporting and "tps_avg" in reporting and "passed" in reporting

    statuses = [c["status"] for c in projection_calls]
    assert statuses == ["provisioning", "running", "completed"]
    assert projection_calls[-1]["external_run_id"] == handle["external_run_id"]
    assert projection_calls[-1]["summary"] == summary


def test_engine_poll_custom_events_streamed() -> None:
    g = compiled()
    cfg = exec_config("exec-events")
    events = custom_events(g, seeded_inputs(0.15), cfg)
    polls = [e for e in events if e.get("type") == "engine_poll"]
    assert len(polls) >= 2  # initial tick + at least one poll cycle
    assert all(e["phase"] == "execution" for e in polls)
    assert all(e["schema_version"] == 1 for e in polls)
    assert polls[0]["status"] == "running"
    assert polls[-1]["status"] == "completed"  # terminal status is emitted
    assert {e["external_run_id"] for e in polls} == {polls[0]["external_run_id"]}
    running = [e for e in polls if e["status"] == "running"]
    assert all(set(e["live_stats"]) == {"vusers", "tps", "error_rate", "p95_ms"} for e in running)
    assert any(e["progress_pct"] > 0 for e in polls)


def test_idempotency_key_deterministic_and_provision_stable() -> None:
    g = compiled()
    cfg = exec_config("exec-idem")
    result = g.invoke(seeded_inputs(0.1), cfg)
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
    result = g.invoke(seeded_inputs(0.2), cfg)

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    # fail_at_pct flowed from the configurable into engine options (not the spec)
    assert entry["engine_options"] == {"fail_at_pct": 50.0}
    assert entry["load_test_spec"]["duration_s"] == 0.2
    assert entry["engine_poll_last"]["status"] == "failed"
    assert entry["test_summary"]["passed"] is False
    assert entry["errors"] == ["run failed before completion (injected failure)"]
    assert "teardown" in calls  # try/finally in engine_collect
    assert "abort" not in calls
    assert result["run_aborted"] is False


def test_engine_options_reject_connection_overrides_before_checkpoint(
    projection_calls: list[dict[str, Any]],
) -> None:
    g = compiled()
    cfg = exec_config("exec-bad-options", load_test={"base_url": "https://evil.invalid"})
    result = g.invoke(seeded_inputs(0.1), cfg)

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    assert "unsupported load_test engine option(s)" in entry["errors"][0]
    assert "base_url" in entry["errors"][0]
    assert "engine_options" not in entry
    assert "load_test_spec" not in entry
    assert projection_calls == []


def test_loadrunner_safe_engine_options_are_allowed() -> None:
    cfg = exec_config(
        "exec-loadrunner-options",
        load_test={"test_id": 42, "test_instance_id": 7, "abortive_stop": True},
    )
    configurable = dict(cfg.get("configurable") or {})
    cfg = cast(RunnableConfig, {**cfg, "configurable": {**configurable, "engine": "loadrunner"}})
    spec, options = execution_phase._build_spec(
        cast(PipelineState, seeded_inputs(0.1)), cfg, 1, "loadrunner"
    )

    assert spec.idempotency_key == "exec-loadrunner-options-execution-a1"
    assert options == {"test_id": 42, "test_instance_id": 7, "abortive_stop": True}


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
    result = g.invoke(seeded_inputs(5.0), cfg)
    assert "__interrupt__" not in result

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "failed"
    assert any("timed out" in error for error in entry["errors"])
    assert "abort" in calls
    assert "teardown" in calls
    assert "collect_artifacts" not in calls
    assert [c["status"] for c in projection_calls] == ["provisioning", "running", "aborted"]


def test_gated_output_review_opens_after_collect_with_summary() -> None:
    g = compiled()
    cfg = exec_config(
        "exec-gated", gates={"execution": {"prompt_review": "auto", "output_review": "gated"}}
    )
    result = g.invoke(seeded_inputs(0.1), cfg)
    payload = pending_interrupt(result)
    assert payload["kind"] == "phase_review"
    assert payload["phase"] == "execution"
    assert "Engine run" in payload["summary"] and "KPIs" in payload["summary"]
    assert "engine_results" in [a["kind"] for a in payload["artifacts"]]

    # The gate opened AFTER engine_collect: summary + handle already checkpointed.
    entry = subgraph_values(g, cfg)["phase_results"]["execution"]
    assert entry["status"] == PhaseStatus.AWAITING_OUTPUT_REVIEW
    assert entry["test_summary"]["passed"] is True
    assert entry["engine_poll_count"] >= 1

    result = g.invoke(Command(resume={"action": "approve"}), cfg)
    assert result["phase_results"]["execution"]["status"] == "succeeded"


def test_reserve_falls_back_to_default_spec_without_upstream_spec() -> None:
    """Standalone execution run: script_scenario succeeded but left no spec."""
    entry = PhaseResult(phase=Phase.SCRIPT_SCENARIO, status=PhaseStatus.SUCCEEDED).as_state()
    inputs = {"title": "Solo", "request": "r", "phase_results": {"script_scenario": entry}}
    cfg = exec_config("exec-fallback", load_test={"duration_s": 0.1})
    result = compiled().invoke(inputs, cfg)

    exec_entry = result["phase_results"]["execution"]
    assert exec_entry["status"] == "succeeded"
    spec = exec_entry["load_test_spec"]
    assert spec["idempotency_key"] == "exec-fallback-execution-a1"
    assert spec["vusers"] == 10
    assert spec["duration_s"] == 0.1
    assert spec["title"] == "Solo load test"
