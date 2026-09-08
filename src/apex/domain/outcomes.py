"""Trust-preserving projections; never rewrite historical execution evidence."""

from typing import Any


def qualified_execution_summary(entry: dict[str, Any]) -> Any:
    summary = entry.get("test_summary")
    if (
        isinstance(summary, dict)
        and entry.get("external") is True
        and summary.get("passed") is True
        and entry.get("source_outcome_explicit") is not True
    ):
        return {
            **summary,
            "passed": None,
            "notes": (
                "Historical imported outcome is unverified: "
                "no explicit source outcome was recorded."
            ),
        }
    return summary
