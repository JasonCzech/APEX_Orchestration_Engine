"""Bounded, versioned model deliverables; provider text is never execution authority."""

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apex.domain.diagnostics import contains_credential_material
from apex.domain.pipeline import Phase
from apex.domain.prompt_catalog import PHASE_SECTIONS

MAX_DELIVERABLE_CHARS = 20_000
Text = Annotated[str, Field(strict=True, min_length=1, max_length=12_000)]


class WorkloadShare(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    scenario_id: Annotated[str, Field(min_length=1, max_length=128)]
    percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class AgentDeliverable(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schema_version: Literal[1]
    phase: str
    disposition: Literal["complete", "blocked"]
    summary: Annotated[str, Field(min_length=1, max_length=2_000)]
    sections: dict[str, Text]
    evidence: Annotated[list[Text], Field(max_length=64)]
    assumptions: Annotated[list[Text], Field(max_length=32)]
    gaps: Annotated[list[Text], Field(max_length=32)]
    blockers: Annotated[list[Text], Field(max_length=32)]
    workload: Annotated[list[WorkloadShare], Field(max_length=100)]


def output_contract(phase: Phase) -> str:
    schema = AgentDeliverable.model_json_schema()
    schema["properties"]["phase"] = {"const": phase.value}
    schema["properties"]["sections"] = {
        "type": "object",
        "additionalProperties": False,
        "required": list(PHASE_SECTIONS[phase]),
        "properties": {
            key: {"type": "string", "minLength": 1, "maxLength": 12_000}
            for key in PHASE_SECTIONS[phase]
        },
    }
    return (
        "=== RUNTIME OUTPUT CONTRACT v1 ===\n"
        "Return exactly one JSON object, without Markdown fences or extra prose. "
        f"The complete JSON must fit {MAX_DELIVERABLE_CHARS} characters; never truncate code. "
        "Use every required section; explain UNKNOWN or not applicable instead of omitting it. "
        "disposition=complete means the phase deliverable is complete, not that a test passed. "
        "Required missing evidence/conflicts require disposition=blocked and nonempty blockers. "
        "Complete requires empty blockers. Use evidence for source references and concise claims, "
        "assumptions for provisional choices, gaps for limitations. Never invent references. "
        "For a complete test_planning deliverable, workload must contain unique scenario_id "
        "entries with positive percent values totaling 100; for all other phases use []. "
        "A blocked plan may have an empty or partial workload. "
        "Keep scripts intact in sections.source or cite a supplied immutable import reference. "
        "If the full source cannot fit, report a blocker; do not claim an executable fragment.\n"
        + json.dumps(schema, separators=(",", ":"))
    )


def parse_deliverable(text: str, phase: Phase) -> AgentDeliverable:
    # Bound before parsing; reject instead of altering code/JSON through redaction.
    if type(text) is not str or len(text) > MAX_DELIVERABLE_CHARS or "\x00" in text:
        raise ValueError("Model deliverable exceeds its size limit or contains invalid text")
    if contains_credential_material(text):
        raise ValueError("Model deliverable contains credential material")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate deliverable fields")
            result[key] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=unique_object)
        if type(data) is not dict or type(data.get("schema_version")) is not int:
            raise ValueError("Invalid schema version")
        value = AgentDeliverable.model_validate(data)
    except (ValidationError, ValueError, RecursionError):
        raise ValueError("Model deliverable does not match the JSON output contract") from None
    if value.phase != phase.value or set(value.sections) != set(PHASE_SECTIONS[phase]):
        raise ValueError("Model deliverable has the wrong phase or required sections")
    if not value.summary.strip() or any(not section.strip() for section in value.sections.values()):
        raise ValueError("Model deliverable contains empty required text")
    strings = [
        value.summary,
        *value.sections.values(),
        *value.evidence,
        *value.assumptions,
        *value.gaps,
        *value.blockers,
        *(row.scenario_id for row in value.workload),
    ]
    if any(not item.strip() or "\x00" in item for item in strings):
        raise ValueError("Model deliverable contains invalid text")
    if (value.disposition == "blocked") != bool(value.blockers):
        raise ValueError("Model deliverable disposition contradicts its blockers")
    if phase is not Phase.TEST_PLANNING and value.workload:
        raise ValueError("Workload shares belong only to test_planning")
    if phase is Phase.TEST_PLANNING:
        ids = [row.scenario_id for row in value.workload]
        if len(ids) != len(set(ids)) or any(not key.strip() for key in ids):
            raise ValueError("Workload scenario IDs must be unique and nonempty")
        if value.disposition == "complete" and (
            not value.workload
            or any(row.percent <= 0 for row in value.workload)
            or not math.isclose(sum(row.percent for row in value.workload), 100, abs_tol=0.01)
        ):
            raise ValueError("Complete plan workload shares must be positive and total 100 percent")
    if contains_credential_material(value.model_dump(), max_nodes=2_000, max_total_chars=50_000):
        raise ValueError("Model deliverable contains credential material")
    return value
