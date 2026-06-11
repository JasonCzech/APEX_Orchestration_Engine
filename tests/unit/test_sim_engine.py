"""Sim engine lifecycle: provision idempotency, status transitions, artifacts, summary."""

import asyncio
import json
from collections.abc import Iterator

import pytest

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import EngineHandle
from apex.ports.execution_engine import EngineRunPhase

FAST_DURATION_S = 0.05


@pytest.fixture(autouse=True)
def _clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


def _engine(**options: object) -> SimExecutionEngine:
    conn = ConnectionConfig(
        id="conn-sim",
        kind=PortKind.EXECUTION_ENGINE,
        provider="sim",
        name="Sim engine",
        options=dict(options),
    )
    return SimExecutionEngine(conn)


def _spec(key: str = "key-1") -> LoadTestSpec:
    return LoadTestSpec(idempotency_key=key, title="demo load test", vusers=20)


async def test_validate_flags_bad_spec() -> None:
    engine = _engine()
    ok = await engine.validate(_spec())
    assert ok.ok and ok.issues == []

    bad = await engine.validate(LoadTestSpec(title="bad", vusers=0, duration_s=0, ramp_s=-1))
    assert not bad.ok
    assert len(bad.issues) == 3


async def test_provision_is_idempotent_on_external_run_id() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    first = await engine.provision(_spec("same-key"))
    second = await engine.provision(_spec("same-key"))
    assert first.external_run_id == second.external_run_id
    assert first.external_run_id is not None and first.external_run_id.startswith("sim-")

    other = await engine.provision(_spec("different-key"))
    assert other.external_run_id != first.external_run_id


async def test_provision_populates_handle() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    assert handle.engine == "sim"
    assert handle.connection_id == "conn-sim"
    assert handle.idempotency_key == "key-1"
    assert float(handle.extras["duration_s"]) == FAST_DURATION_S
    assert handle.extras["vusers"] == "20"


async def test_full_lifecycle_runs_to_completed() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    await engine.start(handle)

    running = await engine.get_status(handle)
    assert running.phase is EngineRunPhase.RUNNING
    assert 0.0 <= running.progress_pct < 100.0
    assert running.live_stats is not None
    assert running.live_stats.tps >= 0.0
    assert running.live_stats.p95_ms > 0.0

    await asyncio.sleep(FAST_DURATION_S * 2)
    done = await engine.get_status(handle)
    assert done.phase is EngineRunPhase.COMPLETED
    assert done.progress_pct == 100.0

    await engine.teardown(handle)


async def test_status_is_poll_safe_and_monotonic() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    progress: list[float] = []
    for _ in range(4):
        status = await engine.get_status(handle)
        progress.append(status.progress_pct)
        await asyncio.sleep(FAST_DURATION_S / 3)
    assert progress == sorted(progress)


async def test_collect_artifacts_stores_results_json() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    store = MemoryArtifactStore()
    handle = await engine.provision(_spec())
    await asyncio.sleep(FAST_DURATION_S * 2)

    refs = await engine.collect_artifacts(handle, store)
    assert len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "engine_results"
    assert ref["name"] == "results.json"
    assert ref["media_type"] == "application/json"
    assert ref["uri"] == f"memory://engine-runs/{handle.external_run_id}/results.json"

    payload = json.loads(await store.get(f"engine-runs/{handle.external_run_id}/results.json"))
    assert payload["engine"] == "sim"
    assert payload["passed"] is True
    assert set(payload["kpis"]) == {"tps_avg", "p95_ms", "error_rate", "vusers_peak"}


async def test_fetch_summary_passes_slas() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    summary = await engine.fetch_summary(handle)
    assert summary.engine == "sim"
    assert summary.passed is True
    assert summary.sla_breaches == []
    assert summary.kpis["vusers_peak"] == 20.0
    assert summary.kpis["tps_avg"] == pytest.approx(20 * 2.4)

    again = await engine.fetch_summary(handle)
    assert again.kpis == summary.kpis  # deterministic


async def test_failure_injection_at_pct() -> None:
    engine = _engine(duration_s=FAST_DURATION_S, fail_at_pct=50)
    handle = await engine.provision(_spec())
    await asyncio.sleep(FAST_DURATION_S)  # well past 50%

    status = await engine.get_status(handle)
    assert status.phase is EngineRunPhase.FAILED
    assert status.progress_pct == 50.0
    assert status.message is not None and "injected failure" in status.message

    summary = await engine.fetch_summary(handle)
    assert summary.passed is False
    assert summary.sla_breaches


async def test_abort_is_noop_in_m1() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    assert await engine.abort(handle, reason="operator request") is None
    # Stateless M1 sim: the run still completes on its simulated clock.
    await asyncio.sleep(FAST_DURATION_S * 2)
    assert (await engine.get_status(handle)).phase is EngineRunPhase.COMPLETED


async def test_unprovisioned_handle_raises() -> None:
    engine = _engine()
    bare = EngineHandle(engine="sim")
    with pytest.raises(ValueError, match="provision"):
        await engine.get_status(bare)
    with pytest.raises(ValueError, match="provision"):
        await engine.start(bare)


async def test_spec_duration_used_when_no_option_override() -> None:
    engine = _engine()  # no duration_s option
    handle = await engine.provision(LoadTestSpec(idempotency_key="k", title="t", duration_s=123.0))
    assert float(handle.extras["duration_s"]) == 123.0
