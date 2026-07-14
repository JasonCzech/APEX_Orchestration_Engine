"""Per-run configuration contract (config.configurable -> PipelineConfigurable).

Parsed through the single from_config accessor everywhere so the eventual
configurable -> context_schema migration touches one place. Golden configurations
are assistants pinning bundles of these fields; per-run config layers on top.
"""

import json
import math
from enum import StrEnum
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.services.run_validation import (
    MAX_MODEL_NAME_CHARS,
    MAX_PROMPT_PART_CHARS_HARD,
    validate_model_by_phase,
)

MAX_POLL_CYCLES = 50_000
MAX_RECOMMENDED_RECURSION_LIMIT = MAX_POLL_CYCLES + 64


class GateMode(StrEnum):
    GATED = "gated"
    AUTO = "auto"


class GatePolicy(BaseModel):
    prompt_review: GateMode = GateMode.GATED
    output_review: GateMode = GateMode.GATED


class Limits(BaseModel):
    max_revise_loops: int = Field(default=3, ge=0, le=10)
    max_dialogue_turns: int = Field(default=20, ge=0, le=100)
    poll_interval_s: float = Field(default=5.0, ge=0.01, le=300.0, allow_inf_nan=False)
    poll_timeout_s: float = Field(
        default=4 * 3600.0,
        gt=0,
        le=86_400.0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_poll_budget(self) -> "Limits":
        cycles = math.ceil(self.poll_timeout_s / self.poll_interval_s)
        if cycles > MAX_POLL_CYCLES:
            raise ValueError(
                "poll_timeout_s / poll_interval_s exceeds the maximum poll-cycle "
                f"budget ({MAX_POLL_CYCLES})"
            )
        return self


class PromptOverride(BaseModel):
    content: str | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    version_id: str | None = Field(default=None, max_length=256)


class PipelineConfigurable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Assistant used to create the run. The graph itself does not branch on
    # this value, but persisting it lets later phase re-runs target the same
    # golden assistant instead of silently falling back to the base graph.
    assistant_id: str = "pipeline"
    project_id: str | None = Field(default=None, max_length=256)
    app_id: str | None = Field(default=None, max_length=256)
    environment_id: str | None = Field(default=None, max_length=256)
    # Server-owned target resolved from environment_id at run authorization time.
    # It is checkpointed so a gated run cannot drift if the catalog changes later.
    environment_target: str | None = Field(default=None, max_length=2_048)
    environment_target_version: int | None = None
    engine: str = "sim"
    connections: dict[str, str] = Field(default_factory=dict, max_length=16)

    # Phase selection: explicit list wins over start/stop range; default = all.
    phases: list[Phase] | None = Field(default=None, max_length=len(PHASE_ORDER))
    start_phase: Phase | None = None
    stop_after: Phase | None = None

    gates: dict[Phase, GatePolicy] = Field(default_factory=dict, max_length=len(PHASE_ORDER))
    prompt_overrides: dict[str, PromptOverride] = Field(default_factory=dict, max_length=32)
    pre_execution_context: list[str] = Field(default_factory=list, max_length=32)
    model_by_phase: dict[Phase, str] = Field(default_factory=dict, max_length=len(PHASE_ORDER))
    # Per-run LoadTestSpec overrides plus provider-specific execution options.
    # This is part of the durable run contract: execution may happen after one
    # or more human gates, when the original RunnableConfig is no longer
    # available unless it was checkpointed explicitly.
    load_test: dict[str, Any] = Field(default_factory=dict, max_length=16)
    # Agent backend selector (mirrors the engine selector). "stub" is the
    # deterministic, offline default; "anthropic" wires a real LLM but only takes
    # effect when an Anthropic key is configured (else it degrades to the stub).
    agent_backend: str = "stub"
    limits: Limits = Field(default_factory=Limits)

    @field_validator("connections")
    @classmethod
    def validate_connections(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 128 for key in values):
            raise ValueError("connection kinds must be 1-128 characters")
        if any(not value or len(value) > 256 for value in values.values()):
            raise ValueError("connection ids must be 1-256 characters")
        return values

    @field_validator("pre_execution_context")
    @classmethod
    def validate_pre_execution_context(cls, values: list[str]) -> list[str]:
        if any(len(value) > 4_000 for value in values):
            raise ValueError("pre_execution_context entries must not exceed 4000 characters")
        return values

    @field_validator("model_by_phase")
    @classmethod
    def validate_models(cls, values: dict[Phase, str]) -> dict[Phase, str]:
        if any(len(value) > MAX_MODEL_NAME_CHARS for value in values.values()):
            raise ValueError("model_by_phase values must not exceed 200 characters")
        validate_model_by_phase(values)
        return values

    @model_validator(mode="after")
    def validate_load_test_controls(self) -> "PipelineConfigurable":
        try:
            encoded = json.dumps(self.load_test, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("load_test must contain finite JSON values") from exc
        if len(encoded) > 20_000:
            raise ValueError("load_test configuration must not exceed 20000 characters")

        spec_fields = set(LoadTestSpec.model_fields) - {
            "idempotency_key",
            "target_environment",
        }
        spec_payload: dict[str, Any] = {"title": "pipeline load test"}
        spec_payload.update(
            {key: value for key, value in self.load_test.items() if key in spec_fields}
        )
        LoadTestSpec.model_validate(spec_payload)
        return self

    @classmethod
    def from_config(cls, config: RunnableConfig | None) -> "PipelineConfigurable":
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        known = {k: v for k, v in configurable.items() if k in cls.model_fields}
        return cls.model_validate(known)

    def gate_policy(self, phase: Phase) -> GatePolicy:
        return self.gates.get(phase, GatePolicy())

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe configuration persisted in pipeline state for gate resumes."""
        return self.model_dump(mode="json")

    def selected_phases(self) -> list[Phase]:
        """Resolve the requested phases in canonical order."""
        order = list(PHASE_ORDER)
        if self.phases is not None:
            requested = set(self.phases)
            return [p for p in order if p in requested]
        start = order.index(self.start_phase) if self.start_phase else 0
        stop = order.index(self.stop_after) if self.stop_after else len(order) - 1
        if start > stop:
            return []
        return order[start : stop + 1]
