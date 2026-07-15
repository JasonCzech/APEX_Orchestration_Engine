"""Simulated execution engine (provider "sim"): time-compressed, deterministic,
failure-injectable. Default engine for dev/CI.

Stateless across calls — all run state lives in EngineHandle.extras, written at
provision time, so get_status survives process restarts as long as the handle is
checkpointed. M1 limitations (by design, documented for the integrator):

- Get-or-create holds at the identity level only: the same idempotency_key always
  derives the same external_run_id, but re-provisioning resets the simulated clock
  (there is no server side to consult).
- abort() records terminal state in the durable handle; there is no server-side
  process, but subsequent status checks still observe a faithful ABORTED result.

Connection options: {"duration_s": float} overrides the spec duration (fast tests);
{"fail_at_pct": float} injects a failure once progress reaches that percentage.
"""

import hashlib
import json
import math
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
from apex.ports.artifact_store import ArtifactStorePort, engine_artifact_key
from apex.ports.execution_engine import EngineRunPhase, EngineRunStatus, LiveStats

logger = structlog.get_logger(__name__)

_MAX_DURATION_S = 86_400.0
_MAX_VUSERS = 10_000
_MAX_NUMERIC_CHARS = 64
_MAX_CLOCK_SKEW_S = 300.0
_RAMP_FRACTION = 0.2  # vusers ramp over the first 20% of the run
_TPS_PER_VUSER = 2.4
_BASE_ERROR_RATE = 0.004
_BASE_P95_MS = 180.0


def _derive_run_id(idempotency_key: str) -> str:
    return "sim-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]


def _bounded_float(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
    minimum_inclusive: bool = True,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > _MAX_NUMERIC_CHARS or "\x00" in text:
            raise ValueError(f"{label} must be a finite number")
        value = text
    elif not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    minimum_ok = parsed >= minimum if minimum_inclusive else parsed > minimum
    if not math.isfinite(parsed) or not minimum_ok or parsed > maximum:
        relation = ">=" if minimum_inclusive else ">"
        raise ValueError(f"{label} must be {relation} {minimum:g} and <= {maximum:g}")
    return parsed


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if (
            not text
            or len(text) > _MAX_NUMERIC_CHARS
            or not text.isascii()
            or not text.isdecimal()
        ):
            raise ValueError(f"{label} must be an integer")
        parsed = int(text)
    else:
        raise ValueError(f"{label} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _parse_extras(handle: EngineHandle) -> tuple[float, float, int, float | None]:
    try:
        started_at = _bounded_float(
            handle.extras["started_at"],
            label="handle started_at",
            minimum=0.0,
            maximum=time.time() + _MAX_CLOCK_SKEW_S,
        )
        duration_s = _bounded_float(
            handle.extras["duration_s"],
            label="handle duration_s",
            minimum=0.0,
            maximum=_MAX_DURATION_S,
            minimum_inclusive=False,
        )
        vusers = _bounded_int(
            handle.extras["vusers"],
            label="handle vusers",
            minimum=1,
            maximum=_MAX_VUSERS,
        )
    except KeyError as exc:
        raise ValueError(
            f"handle for run {handle.external_run_id!r} was not provisioned by the sim "
            f"engine (missing extras[{exc.args[0]!r}]); call provision() first"
        ) from None
    fail_raw = handle.extras.get("fail_at_pct")
    fail_at_pct = (
        _bounded_float(
            fail_raw,
            label="handle fail_at_pct",
            minimum=0.0,
            maximum=100.0,
        )
        if fail_raw is not None
        else None
    )
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
        duration_override = options.get("duration_s")
        self._duration_override = (
            _bounded_float(
                duration_override,
                label="duration_s option",
                minimum=0.0,
                maximum=_MAX_DURATION_S,
                minimum_inclusive=False,
            )
            if duration_override is not None
            else None
        )
        fail_at_pct = options.get("fail_at_pct")
        self._fail_at_pct = (
            _bounded_float(
                fail_at_pct,
                label="fail_at_pct option",
                minimum=0.0,
                maximum=100.0,
            )
            if fail_at_pct is not None
            else None
        )

    async def validate(self, spec: LoadTestSpec) -> ValidationReport:
        issues: list[str] = []
        try:
            _bounded_int(spec.vusers, label="vusers", minimum=1, maximum=_MAX_VUSERS)
        except ValueError:
            issues.append("vusers must be >= 1")
        try:
            _bounded_float(
                spec.duration_s,
                label="duration_s",
                minimum=0.0,
                maximum=_MAX_DURATION_S,
                minimum_inclusive=False,
            )
        except ValueError:
            issues.append("duration_s must be > 0")
        try:
            _bounded_float(
                spec.ramp_s,
                label="ramp_s",
                minimum=0.0,
                maximum=_MAX_DURATION_S,
            )
        except ValueError:
            issues.append("ramp_s must be >= 0")
        return ValidationReport(ok=not issues, issues=issues)

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        validation = await self.validate(spec)
        if not validation.ok:
            raise ValueError("invalid sim load-test spec: " + "; ".join(validation.issues))
        duration = (
            self._duration_override
            if self._duration_override is not None
            else _bounded_float(
                spec.duration_s,
                label="duration_s",
                minimum=0.0,
                maximum=_MAX_DURATION_S,
                minimum_inclusive=False,
            )
        )
        extras = {
            "started_at": f"{time.time():.6f}",
            "duration_s": f"{duration:.6f}",
            "vusers": str(spec.vusers),
        }
        if self._fail_at_pct is not None:
            extras["fail_at_pct"] = f"{self._fail_at_pct:.6f}"
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
        if handle.extras.get("aborted") == "true":
            aborted_progress = _bounded_float(
                handle.extras.get("aborted_progress_pct", "0"),
                label="handle aborted_progress_pct",
                minimum=0.0,
                maximum=100.0,
            )
            return EngineRunStatus(
                phase=EngineRunPhase.ABORTED,
                progress_pct=aborted_progress,
                message="simulated run aborted",
            )
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
        started_at, duration_s, _, _ = _parse_extras(handle)
        elapsed = max(0.0, time.time() - started_at)
        progress = 100.0 if duration_s <= 0 else min(100.0, elapsed / duration_s * 100.0)
        handle.extras["aborted"] = "true"
        handle.extras["aborted_progress_pct"] = f"{progress:.1f}"
        handle.extras["abort_reason_recorded"] = "true"
        logger.info(
            "sim_engine.aborted",
            external_run_id=handle.external_run_id,
            reason_present=bool(reason),
            reason_length=min(len(reason), 1_024),
        )

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
        key = engine_artifact_key(handle.idempotency_key, "results.json")
        stored = await store.put(key, data, content_type="application/json")
        ref = ArtifactRef(
            kind="engine_results",
            name="results.json",
            uri=stored.uri,
            key=stored.key,
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
