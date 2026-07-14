"""Pure domain models for the pipeline. JSON-serializable; zero IO.

These models are stored inside LangGraph state as plain dicts (model_dump(mode="json"))
to avoid per-superstep revalidation and checkpoint/model-evolution traps. Validate at
boundaries with model_validate when typed access is needed.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, FiniteFloat, field_validator

MAX_CONTEXT_ID_CHARS = 128
MAX_CONTEXT_SOURCE_CHARS = 128
MAX_CONTEXT_TITLE_CHARS = 500
MAX_CONTEXT_SUMMARY_CHARS = 4_000
MAX_CONTEXT_REF_CHARS = 2_048
MAX_CONTEXT_TEXT_CHARS = 150_000
MAX_GATE_TEXT_CHARS = 20_000


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
    id: str = Field(default_factory=new_id)
    kind: str
    name: str
    uri: str
    # Canonical object-store key.  Keeping this separate from ``uri`` avoids
    # reverse-parsing provider-specific URLs when persisting ownership metadata.
    key: str | None = None
    # Durable resolver identity for the object store that owns ``key``. Project
    # defaults can change after a run, so artifact reads must not re-resolve by
    # today's default and accidentally fetch from a different store.
    artifact_connection_id: str | None = None
    media_type: str = "application/octet-stream"
    summary: str | None = None
    created_at: str = Field(default_factory=utcnow_iso)


class ApprovalRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    gate: Literal["prompt_review", "phase_review"]
    action: str
    actor: str = "unknown"
    at: str = Field(default_factory=utcnow_iso)
    note: str | None = Field(default=None, max_length=MAX_GATE_TEXT_CHARS)


class ToolCallRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    tool: str
    args_preview: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error"] = "ok"
    duration_ms: int | None = None
    error: str | None = None
    at: str = Field(default_factory=utcnow_iso)


class DialogueEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    phase: Phase
    role: Literal["operator", "agent"]
    content: str = Field(max_length=MAX_GATE_TEXT_CHARS)
    at: str = Field(default_factory=utcnow_iso)


class ContextPacket(BaseModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=MAX_CONTEXT_ID_CHARS)
    source: str = Field(min_length=1, max_length=MAX_CONTEXT_SOURCE_CHARS)
    title: str = Field(min_length=1, max_length=MAX_CONTEXT_TITLE_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    ref: str | None = Field(default=None, max_length=MAX_CONTEXT_REF_CHARS)
    text: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT_CHARS)


class ExternalResults(BaseModel):
    """Results produced outside APEX (e.g. by a standalone analysis dashboard).

    Supplied as run input so an analysis-only run (reporting/postmortem) can report
    on them honestly — plan_resolver seeds a succeeded execution result from this
    instead of the caller forging internal phase state. Maps onto TestResultSummary
    so the reporting phase reads it the same way it reads a real engine run.
    """

    source: str = Field(min_length=1, max_length=MAX_CONTEXT_SOURCE_CHARS)
    uri: str | None = Field(default=None, max_length=MAX_CONTEXT_REF_CHARS)
    engine: str | None = Field(default=None, max_length=128)
    passed: bool | None = None
    kpis: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=32)
    summary: str | None = Field(default=None, max_length=MAX_CONTEXT_SUMMARY_CHARS)
    notes: str | None = Field(default=None, max_length=MAX_GATE_TEXT_CHARS)

    @field_validator("kpis")
    @classmethod
    def validate_kpis(cls, values: dict[str, float]) -> dict[str, float]:
        for name, value in values.items():
            if not name.strip() or len(name) > 64:
                raise ValueError("KPI names must be 1-64 characters")
            if abs(value) > 1_000_000_000_000:
                raise ValueError("KPI values must be between -1e12 and 1e12")
        return values


class EngineHandle(BaseModel):
    engine: str
    connection_id: str | None = None
    external_run_id: str | None = None
    idempotency_key: str = Field(default_factory=new_id)
    extras: dict[str, str] = Field(default_factory=dict)


class ResolvedPromptSource(BaseModel):
    origin: Literal["catalog", "assistant_pin", "run_override", "gate_edit"]
    ref: str | None = None
    editor: str | None = None


class PhaseResult(BaseModel):
    phase: Phase
    status: PhaseStatus = PhaseStatus.PENDING
    attempt: int = 1
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
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    resolved_prompt_source: ResolvedPromptSource | None = None

    def as_state(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
