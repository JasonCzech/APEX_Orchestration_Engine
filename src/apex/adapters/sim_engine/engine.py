"""Simulated execution engine (provider "sim"): time-compressed, deterministic,
failure-injectable. Default engine for dev/CI.

Stateless across calls — all run state lives in EngineHandle.extras, written at
provision time, so get_status survives process restarts as long as the handle is
checkpointed. M1 limitations (by design, documented for the integrator):

- Get-or-create holds at the identity level only: the same idempotency_key always
  derives the same external_run_id, but re-provisioning resets the simulated clock
  (there is no server side to consult).
- abort() is a logged no-op: there is no server-side run to kill, so the caller
  simply stops polling. Real engine abort lands in M3.

Connection options: {"duration_s": float} overrides the spec duration (fast tests);
{"fail_at_pct": float} injects a failure once progress reaches that percentage.
"""

import hashlib
import json
import time
from typing import Any

import structlog

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    LoadTestSpec,
    SecretValue,
    TestResultSummary,
    ValidationReport,
)
from apex.domain.pipeline import ArtifactRef, EngineHandle
from apex.ports.artifact_store import ArtifactStorePort
from apex.ports.execution_engine import EngineRunPhase, EngineRunStatus, LiveStats

logger = structlog.get_logger(__name__)

DEFAULT_DURATION_S = 2.0
_RAMP_FRACTION = 0.2  # vusers ramp over the first 20% of the run
_TPS_PER_VUSER = 2.4
_BASE_ERROR_RATE = 0.004
_BASE_P95_MS = 180.0


def _derive_run_id(idempotency_key: str) -> str:
    return "sim-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]


def _parse_extras(handle: EngineHandle) -> tuple[float, float, int, float | None]:
    try:
        started_at = float(handle.extras["started_at"])
        duration_s = float(handle.extras["duration_s"])
        vusers = int(handle.extras["vusers"])
    except KeyError as exc:
        raise ValueError(
            f"handle for run {handle.external_run_id!r} was not provisioned by the sim "
            f"engine (missing extras[{exc.args[0]!r}]); call provision() first"
        ) from None
    fail_raw = handle.extras.get("fail_at_pct")
    fail_at_pct = float(fail_raw) if fail_raw is not None else None
    return started_at, duration_s, vusers, fail_at_pct


def _live_stats(progress_pct: float, vusers_peak: int) -> LiveStats:
    """Fake-but-plausible curve: linear ramp to peak, then steady state."""
    ramp = min(1.0, progress_pct / (_RAMP_FRACTION * 100.0)) if progress_pct > 0 else 0.0
    active = vusers_peak * ramp
    return LiveStats(
        vusers=round(active, 1),
        tps=round(active * _TPS_PER_VUSER, 1),
        error_rate=_BASE_ERROR_RATE,
        p95_ms=round(_BASE_P95_MS + 0.6 * progress_pct, 1),
    )


@AdapterRegistry.register(PortKind.EXECUTION_ENGINE, "sim")
class SimExecutionEngine:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn
        options = conn.options if conn is not None else {}
        self._duration_override = options.get("duration_s")
        self._fail_at_pct = options.get("fail_at_pct")

    async def validate(self, spec: LoadTestSpec) -> ValidationReport:
        issues: list[str] = []
        if spec.vusers < 1:
            issues.append("vusers must be >= 1")
        if spec.duration_s <= 0:
            issues.append("duration_s must be > 0")
        if spec.ramp_s < 0:
            issues.append("ramp_s must be >= 0")
        return ValidationReport(ok=not issues, issues=issues)

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        duration = (
            float(self._duration_override)
            if self._duration_override is not None
            else float(spec.duration_s or DEFAULT_DURATION_S)
        )
        extras = {
            "started_at": f"{time.time():.6f}",
            "duration_s": f"{duration:.6f}",
            "vusers": str(spec.vusers),
        }
        if self._fail_at_pct is not None:
            extras["fail_at_pct"] = f"{float(self._fail_at_pct):.6f}"
        return EngineHandle(
            engine="sim",
            connection_id=self._conn.id if self._conn is not None else None,
            external_run_id=_derive_run_id(spec.idempotency_key),
            idempotency_key=spec.idempotency_key,
            extras=extras,
        )

    async def start(self, handle: EngineHandle) -> None:
        # The simulated clock starts at provision; start() only validates the handle.
        _parse_extras(handle)

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        started_at, duration_s, vusers, fail_at_pct = _parse_extras(handle)
        elapsed = max(0.0, time.time() - started_at)
        pct = 100.0 if duration_s <= 0 else min(100.0, elapsed / duration_s * 100.0)
        if fail_at_pct is not None and pct >= fail_at_pct:
            return EngineRunStatus(
                phase=EngineRunPhase.FAILED,
                progress_pct=round(fail_at_pct, 1),
                message=f"injected failure at {fail_at_pct:g}% (fail_at_pct option)",
            )
        if pct >= 100.0:
            return EngineRunStatus(
                phase=EngineRunPhase.COMPLETED,
                progress_pct=100.0,
                message="simulated run complete",
            )
        return EngineRunStatus(
            phase=EngineRunPhase.RUNNING,
            progress_pct=round(pct, 1),
            live_stats=_live_stats(pct, vusers),
            message=f"simulated load at {pct:.0f}%",
        )

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        # M1 limitation: nothing server-side to kill (see module docstring).
        logger.info("sim_engine.abort_noop", external_run_id=handle.external_run_id, reason=reason)

    async def collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        summary = await self.fetch_summary(handle)
        payload = {
            "engine": "sim",
            "external_run_id": handle.external_run_id,
            "passed": summary.passed,
            "kpis": summary.kpis,
            "sla_breaches": summary.sla_breaches,
        }
        data = json.dumps(payload, sort_keys=True).encode()
        key = f"engine-runs/{handle.external_run_id}/results.json"
        stored = await store.put(key, data, content_type="application/json")
        ref = ArtifactRef(
            kind="engine_results",
            name="results.json",
            uri=stored.uri,
            media_type="application/json",
            summary=f"Simulated engine results for {handle.external_run_id}",
        )
        return [ref.model_dump(mode="json")]

    async def fetch_summary(self, handle: EngineHandle) -> TestResultSummary:
        _, _, vusers, fail_at_pct = _parse_extras(handle)
        kpis = {
            "tps_avg": round(vusers * _TPS_PER_VUSER, 1),
            "p95_ms": 212.0,
            "error_rate": _BASE_ERROR_RATE,
            "vusers_peak": float(vusers),
        }
        if fail_at_pct is not None:
            return TestResultSummary(
                engine="sim",
                passed=False,
                kpis=kpis,
                sla_breaches=["run failed before completion (injected failure)"],
                notes=f"failure injected at {fail_at_pct:g}%",
            )
        return TestResultSummary(
            engine="sim",
            passed=True,
            kpis=kpis,
            sla_breaches=[],
            notes="simulated run; KPIs are synthetic",
        )

    async def teardown(self, handle: EngineHandle) -> None:
        logger.debug("sim_engine.teardown_noop", external_run_id=handle.external_run_id)
