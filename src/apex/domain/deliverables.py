"""Engineering document fields shared by API validation and the cycle editors."""

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class DeliverableField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    control: Literal["text", "multiline", "number", "select"] = "multiline"
    required: bool = True
    options: list[str] = []
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


def field(key: str, label: str, **kwargs: Any) -> DeliverableField:
    return DeliverableField(key=key, label=label, **kwargs)


DELIVERABLE_FIELDS: dict[str, list[DeliverableField]] = {
    "scope": [
        field(
            "requirements",
            "Requirements and risks",
            help="Include requirement keys, titles and performance risks.",
        ),
        field(
            "source_manifest",
            "Frozen source references",
            help=(
                "Record each source title, provider, revision, retrieval time and "
                "readability. Retrieval connectors are not available in this editor yet."
            ),
        ),
        field("included", "Included journeys"),
        field("excluded", "Excluded scope", required=False),
    ],
    "test_plan": [
        field("objectives", "Objectives"),
        field("journeys", "User journeys and requirement coverage"),
        field("workload_mix", "Workload mix", help="Record each journey and its share of traffic."),
        field("vusers", "Concurrent users", control="number", minimum=1, maximum=1_000_000),
        field(
            "arrival_rate",
            "Arrival rate (requests per second)",
            control="number",
            required=False,
            minimum=0,
            maximum=1_000_000,
        ),
        field("ramp_s", "Ramp (seconds)", control="number", minimum=0, maximum=86_400),
        field(
            "duration_s",
            "Steady-state duration (seconds)",
            control="number",
            minimum=1,
            maximum=86_400,
        ),
        field("test_data", "Test data and parameterization"),
        field(
            "thresholds",
            "Transaction thresholds",
            help=(
                "For each transaction, state latency percentile, error-rate limit and "
                "throughput target."
            ),
        ),
        field(
            "p95_ms_threshold",
            "Overall p95 latency limit (ms)",
            control="number",
            required=False,
            minimum=0,
            maximum=1_000_000_000_000,
        ),
        field(
            "error_rate_threshold",
            "Overall error-rate limit (fraction 0–1)",
            control="number",
            required=False,
            minimum=0,
            maximum=1,
        ),
        field("observability", "Observability and evidence collection"),
        field("entry_criteria", "Entry criteria"),
        field("exit_criteria", "Exit criteria"),
    ],
    "readiness": [
        field("expected", "Expected environment configuration"),
        field("observed", "Observed inventory and telemetry"),
        field("evidence", "Evidence references and observation times"),
        field(
            "status",
            "Readiness assessment",
            control="select",
            options=["not_checked", "blocked", "warning", "ready"],
        ),
        field("inventory_snapshot_id", "Inventory snapshot ID", control="text", required=False),
        field(
            "target_version",
            "Approved target version",
            control="number",
            required=False,
            minimum=0,
            maximum=2_147_483_647,
        ),
        field("remediation", "Blockers, owners and remediation notes", required=False),
    ],
    "script": [
        field(
            "engine", "Execution platform", control="select", options=["apex-load", "loadrunner"]
        ),
        field(
            "format",
            "Script format",
            control="select",
            options=["apex_native_json", "web_http_html", "other_import"],
        ),
        field(
            "source",
            "Script source or immutable import reference",
            help=(
                "Paste native JSON or source text. For an external import, record an "
                "immutable artifact reference. This approval does not launch a test."
            ),
        ),
        field("parameters", "Parameter files and dependencies"),
        field("coverage", "Requirement and journey coverage"),
        field(
            "validation",
            "Inspection and validation evidence",
            help=(
                "Record validation results and tool/provider version. Provider preflight "
                "remains required before execution."
            ),
        ),
    ],
    "report": [
        field("executive_summary", "Executive summary"),
        field("technical_analysis", "Technical analysis"),
        field(
            "evidence",
            "Execution evidence and versions",
            help=(
                "Reference immutable measurements, engine and attempt. Do not replace "
                "raw measurements with interpretation."
            ),
        ),
        field("findings", "Findings and baseline differences"),
        field("exclusions", "Exclusions and limitations"),
        field("accepted_risks", "Accepted risks", required=False),
        field("recommendation", "Recommendation"),
    ],
    "postmortem": [
        field("findings", "Findings"),
        field("factors", "Contributing factors"),
        field("lessons", "Lessons learned"),
        field("actions", "Follow-up actions, owners and status"),
    ],
}


def content_issues(kind: str, content: dict[str, Any]) -> list[str]:
    fields = DELIVERABLE_FIELDS.get(kind, [])
    issues: list[str] = []
    if (
        set(content)
        - {spec.key for spec in fields}
        - (
            {"_execution_evidence"}
            if kind == "report"
            else {"_source_evidence"}
            if kind == "scope"
            else set()
        )
    ):
        issues.append("Remove unsupported fields before review")
    for spec in fields:
        value = content.get(spec.key)
        if value is None or value == "":
            if spec.required:
                issues.append(f"{spec.label} is required")
            continue
        if spec.control == "number":
            if (
                type(value) not in {float, int}
                or (isinstance(value, float) and not math.isfinite(value))
                or (spec.minimum is not None and value < spec.minimum)
                or (spec.maximum is not None and value > spec.maximum)
            ):
                issues.append(f"{spec.label} must be between {spec.minimum:g} and {spec.maximum:g}")
        elif not isinstance(value, str) or not value.strip() or len(value) > 50_000:
            issues.append(f"{spec.label} must be non-empty text under 50,000 characters")
        elif spec.control == "select" and value not in spec.options:
            issues.append(f"Choose a supported value for {spec.label}")
    if kind == "script":
        engine, format_name = content.get("engine"), content.get("format")
        if (engine == "apex-load" and format_name == "web_http_html") or (
            engine == "loadrunner" and format_name == "apex_native_json"
        ):
            issues.append("The script format does not match the execution platform")
        if format_name == "apex_native_json" and isinstance(content.get("source"), str):
            try:
                parsed = json.loads(content["source"])
                if not isinstance(parsed, dict) or not parsed:
                    raise ValueError("empty script")
            except (ValueError, RecursionError):
                issues.append("APEX native script source must be a non-empty JSON object")
    return issues
