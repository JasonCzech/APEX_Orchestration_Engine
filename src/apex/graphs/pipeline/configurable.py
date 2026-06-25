"""Per-run configuration contract (config.configurable -> PipelineConfigurable).

Parsed through the single from_config accessor everywhere so the eventual
configurable -> context_schema migration touches one place. Golden configurations
are assistants pinning bundles of these fields; per-run config layers on top.
"""

from enum import StrEnum
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from apex.domain.pipeline import PHASE_ORDER, Phase


class GateMode(StrEnum):
    GATED = "gated"
    AUTO = "auto"


class GatePolicy(BaseModel):
    prompt_review: GateMode = GateMode.GATED
    output_review: GateMode = GateMode.GATED


class Limits(BaseModel):
    max_revise_loops: int = 3
    max_dialogue_turns: int = 20
    poll_interval_s: float = 5.0
    poll_timeout_s: float = 4 * 3600.0


class PromptOverride(BaseModel):
    content: str | None = None
    version_id: str | None = None


class PipelineConfigurable(BaseModel):
    project_id: str | None = None
    app_id: str | None = None
    environment_id: str | None = None
    engine: str = "sim"
    connections: dict[str, str] = Field(default_factory=dict)

    # Phase selection: explicit list wins over start/stop range; default = all.
    phases: list[Phase] | None = None
    start_phase: Phase | None = None
    stop_after: Phase | None = None

    gates: dict[Phase, GatePolicy] = Field(default_factory=dict)
    prompt_overrides: dict[str, PromptOverride] = Field(default_factory=dict)
    pre_execution_context: list[str] = Field(default_factory=list)
    model_by_phase: dict[Phase, str] = Field(default_factory=dict)
    # Agent backend selector (mirrors the engine selector). "stub" is the
    # deterministic, offline default; "anthropic" wires a real LLM but only takes
    # effect when an Anthropic key is configured (else it degrades to the stub).
    agent_backend: str = "stub"
    limits: Limits = Field(default_factory=Limits)

    @classmethod
    def from_config(cls, config: RunnableConfig | None) -> "PipelineConfigurable":
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        known = {k: v for k, v in configurable.items() if k in cls.model_fields}
        return cls.model_validate(known)

    def gate_policy(self, phase: Phase) -> GatePolicy:
        return self.gates.get(phase, GatePolicy())

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
