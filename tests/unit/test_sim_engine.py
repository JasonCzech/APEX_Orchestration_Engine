"""Sim engine lifecycle: provision idempotency, status transitions, artifacts, summary."""

import asyncio
import json
import time
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import EngineHandle
from apex.ports.artifact_store import engine_artifact_key
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

    # Domain admission rejects this shape; retain the adapter check as defense in
    # depth for legacy/deserialized objects that bypass normal model validation.
    bad_spec = _spec().model_copy(
        update={"title": "bad", "vusers": 0, "duration_s": 0, "ramp_s": -1}
    )
    bad = await engine.validate(bad_spec)
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
    key = engine_artifact_key(handle.idempotency_key, "results.json")
    assert ref["uri"] == f"memory://{key}"
    assert ref["key"] == key

    payload = json.loads(await store.get(key))
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


async def test_abort_persists_terminal_state_in_handle() -> None:
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())
    assert await engine.abort(handle, reason="operator request") is None
    # Abort state lives in the durable handle and remains terminal over time.
    await asyncio.sleep(FAST_DURATION_S * 2)
    assert (await engine.get_status(handle)).phase is EngineRunPhase.ABORTED


async def test_abort_neither_persists_nor_logs_opaque_reason_content() -> None:
    secret_reason = "opaque-api-key-value-that-redaction-cannot-recognize"
    engine = _engine(duration_s=FAST_DURATION_S)
    handle = await engine.provision(_spec())

    with capture_logs() as logs:
        await engine.abort(handle, reason=secret_reason)

    assert secret_reason not in repr(handle.model_dump(mode="json"))
    assert secret_reason not in repr(logs)
    event = next(log for log in logs if log.get("event") == "sim_engine.aborted")
    assert event["reason_present"] is True
    assert event["reason_length"] == len(secret_reason)


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


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"duration_s": float("nan")}, "duration_s option"),
        ({"duration_s": float("inf")}, "duration_s option"),
        ({"duration_s": 0}, "duration_s option"),
        ({"duration_s": 86_401}, "duration_s option"),
        ({"duration_s": "9" * 1_000}, "duration_s option"),
        ({"fail_at_pct": float("nan")}, "fail_at_pct option"),
        ({"fail_at_pct": float("inf")}, "fail_at_pct option"),
        ({"fail_at_pct": -1}, "fail_at_pct option"),
        ({"fail_at_pct": 101}, "fail_at_pct option"),
        ({"fail_at_pct": "9" * 1_000}, "fail_at_pct option"),
    ],
)
def test_constructor_rejects_invalid_numeric_options(
    options: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _engine(**options)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("started_at", "nan"),
        ("started_at", "inf"),
        ("started_at", str(time.time() + 10_000)),
        ("duration_s", "nan"),
        ("duration_s", "inf"),
        ("duration_s", "0"),
        ("duration_s", "86401"),
        ("duration_s", "9" * 1_000),
        ("vusers", "0"),
        ("vusers", "10001"),
        ("vusers", "1.0"),
        ("vusers", "9" * 1_000),
        ("fail_at_pct", "nan"),
        ("fail_at_pct", "inf"),
        ("fail_at_pct", "-1"),
        ("fail_at_pct", "101"),
        ("fail_at_pct", "9" * 1_000),
    ],
)
async def test_status_rejects_corrupt_numeric_handle_extras(name: str, value: str) -> None:
    handle = await _engine().provision(_spec())
    # Durable handles can originate in old/corrupt storage and bypass model admission.
    handle.extras[name] = value
    with pytest.raises(ValueError, match=f"handle {name}"):
        await _engine().get_status(handle)


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "101", "9" * 1_000])
async def test_status_rejects_corrupt_aborted_progress(value: str) -> None:
    handle = await _engine().provision(_spec())
    handle.extras.update({"aborted": "true", "aborted_progress_pct": value})
    with pytest.raises(ValueError, match="handle aborted_progress_pct"):
        await _engine().get_status(handle)


async def test_validate_and_provision_reject_nonfinite_bypassed_spec() -> None:
    spec = _spec().model_copy(
        update={"vusers": True, "duration_s": float("nan"), "ramp_s": float("inf")}
    )
    report = await _engine().validate(spec)
    assert report.ok is False
    assert len(report.issues) == 3
    with pytest.raises(ValueError, match="invalid sim load-test spec"):
        await _engine().provision(spec)
