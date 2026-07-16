"""Pure domain models for the pipeline. JSON-serializable; zero IO.

These models are stored inside LangGraph state as plain dicts (model_dump(mode="json"))
to avoid per-superstep revalidation and checkpoint/model-evolution traps. Validate at
boundaries with model_validate when typed access is needed.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from apex.domain.diagnostics import (
    bounded_diagnostic,
    contains_credential_material,
    is_credential_field,
)
from apex.domain.input_limits import NoNulStr, validate_json_object

MAX_CONTEXT_ID_CHARS = 128
MAX_CONTEXT_SOURCE_CHARS = 128
MAX_CONTEXT_TITLE_CHARS = 500
MAX_CONTEXT_SUMMARY_CHARS = 4_000
MAX_CONTEXT_REF_CHARS = 2_048
MAX_CONTEXT_TEXT_CHARS = 150_000
MAX_GATE_TEXT_CHARS = 20_000
MAX_GATE_DECISION_TEXT_CHARS = 50_000
MAX_TOOL_CALL_RECORDS = 80
MAX_TOOL_ARGS_PREVIEW_BYTES = 8_192


class Phase(StrEnum):
    STORY_ANALYSIS = "story_analysis"
    TEST_PLANNING = "test_planning"
    ENV_TRIAGE = "env_triage"
    SCRIPT_SCENARIO = "script_scenario"
    EXECUTION = "execution"
    REPORTING = "reporting"
    POSTMORTEM = "postmortem"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.STORY_ANALYSIS,
    Phase.TEST_PLANNING,
    Phase.ENV_TRIAGE,
    Phase.SCRIPT_SCENARIO,
    Phase.EXECUTION,
    Phase.REPORTING,
    Phase.POSTMORTEM,
)

# Hard upstream requirements checked by the plan resolver. A prerequisite is satisfied
# by a succeeded result already on the thread OR by the prerequisite phase running
# earlier in the same plan. Partial-results policy (e.g. reporting on a failed
# execution) is an open question in the rebuild plan (risk #8) — strict for now.
PHASE_PREREQUISITES: dict[Phase, tuple[Phase, ...]] = {
    Phase.STORY_ANALYSIS: (),
    Phase.TEST_PLANNING: (Phase.STORY_ANALYSIS,),
    Phase.ENV_TRIAGE: (),
    Phase.SCRIPT_SCENARIO: (Phase.TEST_PLANNING,),
    Phase.EXECUTION: (Phase.SCRIPT_SCENARIO,),
    Phase.REPORTING: (Phase.EXECUTION,),
    Phase.POSTMORTEM: (Phase.REPORTING,),
}


class PhaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_PROMPT_REVIEW = "awaiting_prompt_review"
    AWAITING_OUTPUT_REVIEW = "awaiting_output_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    ABORTED = "aborted"


TERMINAL_PHASE_STATUSES = frozenset(
    {PhaseStatus.SUCCEEDED, PhaseStatus.FAILED, PhaseStatus.SKIPPED, PhaseStatus.ABORTED}
)


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex


class ArtifactRef(BaseModel):
    id: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=128)
    kind: NoNulStr = Field(min_length=1, max_length=64)
    name: NoNulStr = Field(min_length=1, max_length=512)
    uri: NoNulStr = Field(min_length=1, max_length=4_096)
    # Canonical object-store key.  Keeping this separate from ``uri`` avoids
    # reverse-parsing provider-specific URLs when persisting ownership metadata.
    key: NoNulStr | None = Field(default=None, max_length=1_024)
    # Durable resolver identity for the object store that owns ``key``. Project
    # defaults can change after a run, so artifact reads must not re-resolve by
    # today's default and accidentally fetch from a different store.
    artifact_connection_id: NoNulStr | None = Field(default=None, max_length=256)
    media_type: NoNulStr = Field(default="application/octet-stream", min_length=1, max_length=255)
    summary: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    created_at: NoNulStr = Field(default_factory=utcnow_iso, min_length=1, max_length=64)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=256)
    gate: Literal["prompt_review", "phase_review"]
    action: NoNulStr = Field(min_length=1, max_length=32)
    actor: NoNulStr = Field(default="unknown", min_length=1, max_length=255)
    at: NoNulStr = Field(default_factory=utcnow_iso, min_length=1, max_length=64)
    note: NoNulStr | None = Field(default=None, max_length=MAX_GATE_DECISION_TEXT_CHARS)

    @field_validator("actor", "note")
    @classmethod
    def reject_credential_material(cls, value: str | None) -> str | None:
        if value is not None and contains_credential_material(value):
            raise ValueError("approval attribution must not contain credential material")
        return value


class ToolCallRecord(BaseModel):
    id: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=256)
    tool: NoNulStr = Field(min_length=1, max_length=256)
    args_preview: dict[str, Any] = Field(default_factory=dict, max_length=32)
    status: Literal["ok", "error"] = "ok"
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    error: NoNulStr | None = Field(default=None, max_length=4_096)
    at: NoNulStr = Field(default_factory=utcnow_iso, min_length=1, max_length=64)

    @field_validator("args_preview")
    @classmethod
    def validate_args_preview(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_object(
            value,
            label="tool-call argument preview",
            max_bytes=MAX_TOOL_ARGS_PREVIEW_BYTES,
            max_nodes=128,
            max_depth=6,
            max_key_chars=128,
        )


class DialogueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=256)
    phase: Phase
    attempt: int = Field(default=1, ge=1, le=1_000_000)
    role: Literal["operator", "agent"]
    content: NoNulStr = Field(max_length=MAX_GATE_DECISION_TEXT_CHARS)
    at: NoNulStr = Field(default_factory=utcnow_iso, min_length=1, max_length=64)

    @field_validator("content")
    @classmethod
    def reject_dialogue_credentials(cls, value: str) -> str:
        if contains_credential_material(value):
            raise ValueError("dialogue must not contain credential material")
        return value


class ContextPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=MAX_CONTEXT_ID_CHARS)
    source: NoNulStr = Field(min_length=1, max_length=MAX_CONTEXT_SOURCE_CHARS)
    title: NoNulStr = Field(min_length=1, max_length=MAX_CONTEXT_TITLE_CHARS)
    summary: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    ref: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_REF_CHARS)
    text: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_TEXT_CHARS)


class ExternalResults(BaseModel):
    """Results produced outside APEX (e.g. by a standalone analysis dashboard).

    Supplied as run input so an analysis-only run (reporting/postmortem) can report
    on them honestly — plan_resolver seeds a succeeded execution result from this
    instead of the caller forging internal phase state. Maps onto TestResultSummary
    so the reporting phase reads it the same way it reads a real engine run.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source: NoNulStr = Field(min_length=1, max_length=MAX_CONTEXT_SOURCE_CHARS)
    uri: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_REF_CHARS)
    engine: NoNulStr | None = Field(default=None, max_length=128)
    passed: bool | None = None
    kpis: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=32)
    summary: NoNulStr | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    notes: NoNulStr | None = Field(default=None, max_length=MAX_GATE_TEXT_CHARS)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str | None) -> str | None:
        """Keep bearer/signed URLs out of durable graph state."""

        if value is None:
            return None
        if (
            not value
            or value != value.strip()
            or "\\" in value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("external results uri must be a bounded http(s) URL")
        parsed = None
        port = None
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            parsed = None
        if parsed is None:
            raise ValueError("external results uri must be a bounded http(s) URL")
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise ValueError(
                "external results uri must not contain credentials, query, or fragment"
            )
        return value

    @field_validator("kpis")
    @classmethod
    def validate_kpis(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if not name.strip() or len(name) > 64 or "\x00" in name:
                raise ValueError("KPI names must be 1-64 characters")
            if abs(value) > 1_000_000_000_000:
                raise ValueError("KPI values must be between -1e12 and 1e12")
        return values

    @model_validator(mode="after")
    def reject_credential_material(self) -> "ExternalResults":
        """Revalidate direct-graph and legacy replay inputs at the domain boundary."""

        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("external results must not contain credential material")
        return self


ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL = (
    "engine run lacks durable execution-connection affinity; identify the original "
    "provider connection and recover the run out of band before retrying"
)


class EngineConnectionAffinityMissingError(RuntimeError):
    """A legacy run cannot be mapped safely to one exact execution connection."""

    def __init__(self) -> None:
        super().__init__(ENGINE_CONNECTION_AFFINITY_RECOVERY_DETAIL)


class EngineHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    engine: NoNulStr = Field(min_length=1, max_length=64)
    connection_id: NoNulStr | None = Field(default=None, max_length=256)
    external_run_id: NoNulStr | None = Field(default=None, max_length=255)
    idempotency_key: NoNulStr = Field(default_factory=new_id, min_length=1, max_length=256)
    extras: dict[NoNulStr, NoNulStr] = Field(default_factory=dict, max_length=32)

    @field_validator("extras")
    @classmethod
    def validate_extras(cls, values: dict[str, str]) -> dict[str, str]:
        aggregate_chars = 0
        for name, value in values.items():
            if not name or len(name) > 64 or "\x00" in name:
                raise ValueError("engine handle extra names must be 1-64 characters")
            if is_credential_field(name):
                raise ValueError("engine handle extras must not contain credential fields")
            if len(value) > 2_048 or "\x00" in value:
                raise ValueError("engine handle extra values must not exceed 2048 characters")
            if bounded_diagnostic(value, max_chars=max(1, len(value))) != value:
                raise ValueError("engine handle extras must not contain credential material")
            aggregate_chars += len(name) + len(value)
        if aggregate_chars > 16_384:
            raise ValueError("engine handle extras must not exceed 16384 aggregate characters")
        return values

    @model_validator(mode="after")
    def reject_credential_material(self) -> "EngineHandle":
        """Keep executable provider identity capability-free at every consumer."""

        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("engine handle must not contain credential material")
        return self


class ResolvedPromptSource(BaseModel):
    origin: Literal["catalog", "assistant_pin", "run_override", "gate_edit"]
    ref: str | None = None
    editor: str | None = None


class PhaseResult(BaseModel):
    phase: Phase
    status: PhaseStatus = PhaseStatus.PENDING
    attempt: int = Field(default=1, ge=1, le=1_000_000)
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    summary: str | None = None
    reasoning_digest: str | None = None
    transcript_ref: ArtifactRef | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list, max_length=MAX_TOOL_CALL_RECORDS)
    resolved_prompt_source: ResolvedPromptSource | None = None

    def as_state(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
