"""Conservative result projections and descriptive comparisons of linked attempts."""

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from apex.domain.cycles import version_digest
from apex.services.public_projection import public_test_result_summary


class CycleMetric(BaseModel):
    key: str
    label: str
    unit: str
    value: float | None
    preference: Literal["higher", "lower", "context"]
    baseline_value: float | None = None
    difference: float | None = None
    percent_difference: float | None = None


class CycleResult(BaseModel):
    id: str
    thread_id: str
    engine: str
    attempt: int
    execution_status: str
    started_at: datetime
    ended_at: datetime | None
    test_outcome: Literal["passed", "failed"] | None = None
    evidence_status: Literal["available", "partial", "unavailable"]
    metrics: list[CycleMetric]
    sla_breaches: list[str]
    warnings: list[str]
    bindings_verified: Literal[False] = False
    comparison_available: bool = False
    evidence_checksum: str = ""


METRICS: tuple[tuple[str, str, str, Literal["higher", "lower", "context"]], ...] = (
    ("tps_avg", "Average throughput", "transactions/s", "higher"),
    ("p95_ms", "95th percentile response time", "ms", "lower"),
    ("error_rate", "Error rate", "ratio", "lower"),
    ("vusers_peak", "Peak virtual users", "users", "context"),
)


def result_view(run: Any) -> CycleResult:
    summary = public_test_result_summary(run.summary)
    warnings = [
        "History projection; inspect execution evidence before approving a report.",
        "Script, workload and environment bindings to this cycle remain unverified.",
    ]
    if summary and summary["engine"] != run.engine:
        warnings.append("Summary engine conflicts with the linked attempt; evidence withheld.")
        summary = None
    outcome = None
    if summary:
        if summary["passed"] is False:
            outcome = "failed"
        elif summary["passed"] is True and run.status == "completed":
            outcome = "passed"
        if summary["sla_breaches"]:
            if summary["passed"] is True:
                warnings.append(
                    "Recorded pass contradicts SLA breaches; treat the result as failed."
                )
            outcome = "failed"
        if summary["passed"] is True and run.status != "completed":
            warnings.append(
                "A pass is unavailable because execution did not complete successfully."
            )
    if run.status != "completed":
        warnings.append(
            "Execution evidence may be partial; status does not determine test outcome."
        )
    kpis = summary["kpis"] if summary else {}
    metrics = []
    for key, label, unit, preference in METRICS:
        value = kpis.get(key)
        if value is not None and (value < 0 or (key == "error_rate" and value > 1)):
            warnings.append(f"{label} is outside its supported range and is unavailable.")
            value = None
        metrics.append(
            CycleMetric(key=key, label=label, unit=unit, value=value, preference=preference)
        )
    result = CycleResult(
        id=run.id,
        thread_id=run.thread_id,
        engine=run.engine,
        attempt=run.attempt,
        execution_status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        test_outcome=outcome,
        evidence_status=(
            "unavailable"
            if summary is None
            else "available"
            if outcome is not None
            and run.status == "completed"
            and all(m.value is not None for m in metrics)
            else "partial"
        ),
        metrics=metrics,
        sla_breaches=summary["sla_breaches"] if summary else [],
        warnings=warnings,
    )
    result.evidence_checksum = version_digest(
        result.model_dump(mode="json", exclude={"evidence_checksum"})
    )
    return result


def compare_result(result: CycleResult, baseline: CycleResult) -> CycleResult:
    result = result.model_copy(deep=True)
    if result.id == baseline.id:
        return result
    if result.engine != baseline.engine:
        result.warnings.append("Comparison unavailable: select an attempt from the same engine.")
        return result
    if result.execution_status != "completed" or baseline.execution_status != "completed":
        result.warnings.append("Comparison unavailable until both executions are completed.")
        return result
    for metric, reference in zip(result.metrics, baseline.metrics, strict=True):
        metric.baseline_value = reference.value
        if metric.value is None or reference.value is None:
            continue
        result.comparison_available = True
        metric.difference = metric.value - reference.value
        if reference.value != 0:
            percent = metric.difference / abs(reference.value) * 100
            metric.percent_difference = percent if math.isfinite(percent) else None
    result.warnings.append(
        "Differences are descriptive; equivalent workloads and targets have not been verified."
    )
    return result
