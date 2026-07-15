"""Secret-free bounded projections for operational provider state."""

from __future__ import annotations

from typing import Any

from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.integrations import TestResultSummary

MAX_PUBLIC_SLA_BREACHES = 16
MAX_PUBLIC_SLA_BREACH_CHARS = 512
MAX_PUBLIC_RESULT_NOTES_CHARS = 2_048


def public_engine_handle_summary(value: Any) -> dict[str, str | None] | None:
    """Return only the two engine-handle fields consumed by dashboard reads."""

    if not isinstance(value, dict):
        return None
    engine = value.get("engine")
    if not isinstance(engine, str) or not engine or len(engine) > 64 or "\x00" in engine:
        return None
    safe_engine = bounded_diagnostic(engine, max_chars=len(engine))
    external_run_id = value.get("external_run_id")
    if external_run_id is not None and (
        not isinstance(external_run_id, str)
        or len(external_run_id) > 255
        or "\x00" in external_run_id
    ):
        external_run_id = None
    if isinstance(external_run_id, str):
        external_run_id = bounded_diagnostic(
            external_run_id,
            max_chars=max(1, len(external_run_id)),
        )
    return {
        "engine": safe_engine,
        "external_run_id": external_run_id if isinstance(external_run_id, str) else None,
    }


def public_test_result_summary(value: Any) -> dict[str, Any] | None:
    """Return only the bounded normalized result schema from durable JSON."""

    if not isinstance(value, dict):
        return None
    try:
        summary = TestResultSummary.model_validate(value)
    except (TypeError, ValueError):
        return None
    payload = summary.model_dump(mode="json")
    payload["engine"] = bounded_diagnostic(
        summary.engine,
        max_chars=max(1, len(summary.engine)),
    )
    payload["kpis"] = {
        name: value
        for name, value in summary.kpis.items()
        if bounded_diagnostic(name, max_chars=max(1, len(name))) == name
    }
    payload["sla_breaches"] = [
        bounded_diagnostic(
            item,
            max_chars=min(max(1, len(item)), MAX_PUBLIC_SLA_BREACH_CHARS),
        )
        for item in summary.sla_breaches[:MAX_PUBLIC_SLA_BREACHES]
    ]
    if summary.notes is not None:
        payload["notes"] = bounded_diagnostic(
            summary.notes,
            max_chars=min(max(1, len(summary.notes)), MAX_PUBLIC_RESULT_NOTES_CHARS),
        )
    return payload
