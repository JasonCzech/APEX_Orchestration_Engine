"""Cross-engine conformance suite (plan M5): the executable definition of the
ExecutionEnginePort adapter contract.

One engine-neutral LoadTestSpec runs against every registered execution engine
— "sim" live, "apex_load" and "loadrunner" against in-memory mock remotes (see
tests/conformance/harnesses.py) — and every numbered test asserts one
requirement of the adapter contract:

1. provision() is GET-OR-CREATE by spec.idempotency_key, across fresh adapter
   instances (process-restart simulation): one remote test, equal run ids.
2. start() is idempotent: double-start (same instance and post-restart) is
   tolerated and the remote starts load exactly once.
3. get_status() is cheap, read-only and poll-safe: monotonic progression to a
   terminal EngineRunPhase; live_stats None or normalized (error_rate 0..1).
4. abort() is idempotent and drives the run to a terminal 'aborted' phase
   (skip-with-reason where the provider documents abort as a no-op);
   teardown() never raises, even called twice on a finished run.
5. collect_artifacts() streams results into the provided ArtifactStorePort and
   returns ArtifactRef-shaped dicts whose bytes actually landed in the store.
6. fetch_summary() returns a validated TestResultSummary: engine matches the
   provider, KPI keys from the normalized set when present (providers that
   honestly cannot report KPIs must return the documented degraded shape),
   and a mocked SLA breach yields passed=False with non-empty sla_breaches.

A new engine adapter is conformant when `make_harness` gains a branch for it
and this whole module passes. A final sim-only smoke proves the execution
phase's engine spine consumes the very port surface verified above.

Every assertion message names its requirement via req().
"""

import asyncio
import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from apex.adapters.stubs import MemoryArtifactStore
from apex.domain import integrations
from apex.domain.integrations import LoadTestSpec, ValidationReport
from apex.domain.pipeline import ArtifactRef, EngineHandle
from apex.graphs.pipeline import execution_phase
from apex.graphs.pipeline.graph import builder
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineRunPhase,
    EngineRunStatus,
    ExecutionEnginePort,
)
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver
from tests.conformance.harnesses import (
    ARTIFACT_REF_KEYS,
    NORMALIZED_KPI_KEYS,
    PROVIDERS,
    EngineHarness,
    make_harness,
)

MAX_POLL_CYCLES = 10

_REQUIREMENT_TITLES = {
    1: "provision is get-or-create by idempotency_key",
    2: "start is idempotent",
    3: "get_status is poll-safe and terminal-correct with normalized live stats",
    4: "abort is idempotent and teardown never raises",
    5: "collect_artifacts streams ArtifactRef-shaped results into the store",
    6: "fetch_summary returns a normalized TestResultSummary with an SLA verdict",
}


def req(number: int, detail: str) -> str:
    return f"[contract requirement {number}: {_REQUIREMENT_TITLES[number]}] {detail}"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


@pytest.fixture(params=PROVIDERS)
def harness(request: pytest.FixtureRequest) -> Iterator[EngineHarness]:
    with make_harness(str(request.param)) as active:
        yield active


# ── drivers ───────────────────────────────────────────────────────────────────


async def provision_and_start(
    harness: EngineHarness, key_suffix: str
) -> tuple[LoadTestSpec, EngineHandle]:
    """validate -> provision -> start on a fresh adapter (the spine's sequence)."""
    spec = harness.make_spec(key_suffix)
    adapter = harness.fresh_adapter()
    report = await adapter.validate(spec)
    assert report.ok, (
        f"{harness.provider}: the shared conformance spec must validate cleanly; "
        f"issues: {report.issues}"
    )
    handle = await adapter.provision(spec)
    await adapter.start(handle)
    return spec, handle


async def drive_to_terminal(harness: EngineHarness, handle: EngineHandle) -> list[EngineRunStatus]:
    """Poll with a FRESH adapter per cycle (statelessness is part of the deal),
    advancing the remote between polls, until a terminal phase shows up."""
    statuses = [await harness.fresh_adapter().get_status(handle)]
    for _ in range(MAX_POLL_CYCLES):
        if statuses[-1].phase in TERMINAL_ENGINE_PHASES:
            return statuses
        harness.advance()
        statuses.append(await harness.fresh_adapter().get_status(handle))
    pytest.fail(
        req(
            3,
            f"{harness.provider}: no terminal EngineRunPhase within {MAX_POLL_CYCLES} "
            f"poll cycles; last phase was {statuses[-1].phase!r}",
        )
    )


# ── requirement 0: port shape + spec validation ───────────────────────────────


async def test_adapter_satisfies_the_port_and_validates_the_shared_spec(
    harness: EngineHarness,
) -> None:
    adapter = harness.fresh_adapter()
    assert isinstance(adapter, ExecutionEnginePort), (
        f"{harness.provider}: adapter must structurally satisfy ExecutionEnginePort"
    )
    report = await adapter.validate(harness.make_spec("validate"))
    assert isinstance(report, ValidationReport)
    assert report.ok, f"{harness.provider}: conformance spec rejected: {report.issues}"
    creates = harness.remote_create_count()
    assert creates in (None, 0), (
        f"{harness.provider}: validate() must not create remote resources (created {creates})"
    )


# ── requirement 1: provision is get-or-create ─────────────────────────────────


async def test_req1_provision_is_get_or_create_across_fresh_instances(
    harness: EngineHarness,
) -> None:
    spec = harness.make_spec("req1")

    first = harness.fresh_adapter()
    handle_1 = await first.provision(spec)
    await first.start(handle_1)

    # Process-restart simulation: a freshly constructed adapter shares zero
    # in-memory state; only spec.idempotency_key + the remote system remain.
    restarted = harness.fresh_adapter()
    handle_2 = await restarted.provision(spec)
    await restarted.start(handle_2)

    assert handle_1.external_run_id, req(
        1, f"{harness.provider}: started run must carry an external_run_id"
    )
    assert handle_1.external_run_id == handle_2.external_run_id, req(
        1,
        f"{harness.provider}: re-provisioning key {spec.idempotency_key!r} on a fresh "
        f"instance yielded {handle_2.external_run_id!r} != {handle_1.external_run_id!r}",
    )
    assert handle_2.engine == harness.provider and handle_2.idempotency_key == spec.idempotency_key

    creates = harness.remote_create_count()
    if creates is not None:
        assert creates == 1, req(
            1, f"{harness.provider}: exactly one remote test may exist, found {creates}"
        )


# ── requirement 2: start is idempotent ────────────────────────────────────────


async def test_req2_start_is_idempotent(harness: EngineHarness) -> None:
    spec, handle = await provision_and_start(harness, "req2")

    # Same-instance double start (crash between start and checkpoint commit).
    await harness.fresh_adapter().start(handle)

    # Post-restart double start: provision-then-start from scratch.
    restarted = harness.fresh_adapter()
    handle_again = await restarted.provision(spec)
    await restarted.start(handle_again)

    starts = harness.remote_start_count()
    if starts is not None:
        assert starts == 1, req(
            2,
            f"{harness.provider}: load must start exactly once no matter how often "
            f"start() runs; remote counted {starts}",
        )


# ── requirement 3: poll-safe status to a terminal phase ───────────────────────


async def test_req3_status_progresses_to_a_terminal_phase(harness: EngineHarness) -> None:
    _, handle = await provision_and_start(harness, "req3")

    # Read-only probe: two polls with no remote advance must agree on the phase.
    probe_a = await harness.fresh_adapter().get_status(handle)
    probe_b = await harness.fresh_adapter().get_status(handle)
    assert probe_a.phase is probe_b.phase, req(
        3,
        f"{harness.provider}: get_status mutated the run "
        f"({probe_a.phase!r} -> {probe_b.phase!r} without the remote advancing)",
    )

    statuses = await drive_to_terminal(harness, handle)
    phases = [status.phase for status in statuses]
    assert len(statuses) >= 2 and phases[0] not in TERMINAL_ENGINE_PHASES, req(
        3, f"{harness.provider}: expected a non-terminal poll before the terminal one: {phases}"
    )
    assert all(phase not in TERMINAL_ENGINE_PHASES for phase in phases[:-1]), req(
        3, f"{harness.provider}: terminal phase showed up mid-lifecycle: {phases}"
    )
    assert phases[-1] is EngineRunPhase.COMPLETED, req(
        3, f"{harness.provider}: happy-path run must end COMPLETED, got {phases[-1]!r}"
    )

    progress = [status.progress_pct for status in statuses]
    assert all(0.0 <= pct <= 100.0 for pct in progress), req(
        3, f"{harness.provider}: progress_pct out of [0, 100]: {progress}"
    )
    assert all(later >= earlier for earlier, later in itertools.pairwise(progress)), req(
        3, f"{harness.provider}: progress_pct must not move backwards: {progress}"
    )
    assert progress[-1] == 100.0, req(
        3, f"{harness.provider}: terminal status must report 100%, got {progress[-1]}"
    )

    for status in statuses:
        stats = status.live_stats
        if stats is None:
            continue  # the port allows phase-only status (e.g. loadrunner v1)
        assert 0.0 <= stats.error_rate <= 1.0, req(
            3,
            f"{harness.provider}: live_stats.error_rate must be a 0..1 fraction, "
            f"got {stats.error_rate}",
        )
        assert stats.vusers >= 0.0 and stats.tps >= 0.0 and stats.p95_ms >= 0.0, req(
            3, f"{harness.provider}: live_stats carries negative metrics: {stats!r}"
        )


# ── requirement 4: abort idempotent, teardown never raises ───────────────────


async def test_req4_abort_is_idempotent_and_teardown_never_raises(
    harness: EngineHarness,
) -> None:
    _, handle = await provision_and_start(harness, "req4")
    status = await harness.fresh_adapter().get_status(handle)
    assert status.phase not in TERMINAL_ENGINE_PHASES, req(
        4, f"{harness.provider}: abort must be exercised mid-run, but got {status.phase!r}"
    )

    await harness.fresh_adapter().abort(handle, reason="conformance: first abort")
    # Second abort — fresh instance, run already stopping/terminal — must no-op.
    await harness.fresh_adapter().abort(handle, reason="conformance: duplicate abort")

    # Teardown never raises, even twice, even after the run is gone/terminal.
    await harness.fresh_adapter().teardown(handle)
    await harness.fresh_adapter().teardown(handle)


async def test_req4_abort_drives_the_run_to_aborted(harness: EngineHarness) -> None:
    if not harness.remote_abort_supported:
        pytest.skip(f"{harness.provider}: {harness.abort_limitation}")
    _, handle = await provision_and_start(harness, "req4-terminal")
    await harness.fresh_adapter().abort(handle, reason="conformance: kill mid-run")
    statuses = await drive_to_terminal(harness, handle)
    assert statuses[-1].phase is EngineRunPhase.ABORTED, req(
        4,
        f"{harness.provider}: aborted run must end in phase 'aborted', got {statuses[-1].phase!r}",
    )


# ── requirement 5: artifacts land in the provided store ──────────────────────


async def test_req5_collect_artifacts_streams_into_the_store(harness: EngineHarness) -> None:
    _, handle = await provision_and_start(harness, "req5")
    await drive_to_terminal(harness, handle)

    store = MemoryArtifactStore()
    refs = await harness.fresh_adapter().collect_artifacts(handle, store)
    assert len(refs) >= 1, req(
        5, f"{harness.provider}: a completed run must yield at least one artifact ref"
    )
    for ref in refs:
        missing = ARTIFACT_REF_KEYS - set(ref)
        assert not missing, req(
            5, f"{harness.provider}: artifact ref missing keys {sorted(missing)}: {ref}"
        )
        assert all(str(ref[key]) for key in ARTIFACT_REF_KEYS), req(
            5, f"{harness.provider}: artifact ref has empty required fields: {ref}"
        )
        ArtifactRef.model_validate(ref)  # ArtifactRef-shaped, not just key-shaped
        uri = str(ref["uri"])
        assert uri.startswith("memory://"), req(
            5,
            f"{harness.provider}: artifact must be stored via the PROVIDED store "
            f"(expected a memory:// uri, got {uri!r})",
        )
        data = await store.get(uri.removeprefix("memory://"))
        assert data, req(5, f"{harness.provider}: no bytes landed in the store for {uri!r}")


# ── requirement 6: normalized summary + SLA verdict ───────────────────────────


async def test_req6_summary_is_normalized_on_a_passing_run(harness: EngineHarness) -> None:
    _, handle = await provision_and_start(harness, "req6-pass")
    await drive_to_terminal(harness, handle)

    summary = await harness.fresh_adapter().fetch_summary(handle)
    assert isinstance(summary, integrations.TestResultSummary)
    assert summary.engine == harness.provider, req(
        6, f"{harness.provider}: summary.engine is {summary.engine!r}"
    )
    assert summary.passed is True, req(
        6, f"{harness.provider}: clean run must pass; breaches={summary.sla_breaches}"
    )
    assert summary.sla_breaches == [], req(
        6, f"{harness.provider}: clean run reported breaches {summary.sla_breaches}"
    )
    unknown = set(summary.kpis) - NORMALIZED_KPI_KEYS
    assert not unknown, req(
        6, f"{harness.provider}: KPI keys outside the normalized set: {sorted(unknown)}"
    )
    if "error_rate" in summary.kpis:
        assert 0.0 <= summary.kpis["error_rate"] <= 1.0, req(
            6, f"{harness.provider}: kpis['error_rate'] must be a 0..1 fraction"
        )
    if harness.summary_reports_kpis:
        assert summary.kpis, req(
            6, f"{harness.provider}: expected normalized KPIs on a completed run"
        )
    else:
        # Documented degraded shape (e.g. loadrunner v1): empty KPIs + honest note.
        assert summary.kpis == {} and summary.notes, req(
            6, f"{harness.provider}: degraded KPI shape violated: {harness.kpi_limitation}"
        )


async def test_req6_sla_breach_fails_the_summary(harness: EngineHarness) -> None:
    harness.enable_sla_breach()
    _, handle = await provision_and_start(harness, "req6-breach")
    await drive_to_terminal(harness, handle)

    summary = await harness.fresh_adapter().fetch_summary(handle)
    assert summary.engine == harness.provider
    assert summary.passed is False, req(
        6, f"{harness.provider}: an SLA-breaching run must not pass"
    )
    assert summary.sla_breaches, req(
        6, f"{harness.provider}: failed run must carry at least one sla_breaches entry"
    )


# ── graph-level smoke (sim only: the one live engine) ─────────────────────────


def _record_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_sim_graph_smoke_execution_spine_consumes_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The execution phase (reserve -> start -> poll -> collect) end-to-end with
    InMemorySaver against the live sim engine: proof the spine drives exactly
    the port surface this suite just verified."""
    monkeypatch.setattr(execution_phase, "_make_resolver", lambda: ConnectionResolver())
    monkeypatch.setattr(engine_runs, "record_engine_run_sync", _record_noop)

    graph: CompiledStateGraph[Any, Any, Any, Any] = builder.compile(checkpointer=InMemorySaver())
    inputs = {
        "title": "Conformance",
        "request": "smoke the engine spine",
    }
    phases = [
        "story_analysis",
        "test_planning",
        "env_triage",
        "script_scenario",
        "execution",
    ]
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": "conformance-smoke",
            "phases": phases,
            "gates": {
                phase: {"prompt_review": "auto", "output_review": "auto"} for phase in phases
            },
            "limits": {"poll_interval_s": 0.02, "poll_timeout_s": 30.0},
            "load_test": {"duration_s": 0.2, "vusers": 4},
        },
        "recursion_limit": 150,
    }
    result = graph.invoke(inputs, cfg)
    assert "__interrupt__" not in result

    entry = result["phase_results"]["execution"]
    assert entry["status"] == "succeeded"
    assert entry["load_test_spec"]["idempotency_key"] == "conformance-smoke-execution-a1"
    assert entry["engine_poll_count"] >= 1
    assert entry["engine_poll_last"]["status"] == EngineRunPhase.COMPLETED.value

    handle = result["engine_handle"]
    assert handle["engine"] == "sim"
    assert str(handle["external_run_id"]).startswith("sim-")
    assert entry["test_summary"]["passed"] is True
    assert set(entry["test_summary"]["kpis"]) <= NORMALIZED_KPI_KEYS

    engine_artifacts = [ref for ref in result["artifacts"] if ref["kind"] == "engine_results"]
    assert engine_artifacts, "spine must persist the engine's artifacts"
    uri = str(engine_artifacts[0]["uri"])
    assert uri.startswith("apex-artifact:///")
    data = asyncio.run(MemoryArtifactStore().get(str(engine_artifacts[0]["key"])))
    assert data, "engine artifact bytes must land in the resolved artifact store"
