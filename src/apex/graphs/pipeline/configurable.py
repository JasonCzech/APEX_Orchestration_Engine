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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import (
    NoNulStr,
    ScopeId,
    validate_json_object,
    validation_error_summary,
)
from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.services.run_validation import (
    MAX_MODEL_NAME_CHARS,
    MAX_PROMPT_PART_CHARS_HARD,
    validate_model_by_phase,
)

MAX_POLL_CYCLES = 50_000
MAX_RECOMMENDED_RECURSION_LIMIT = MAX_POLL_CYCLES * 2 + 64


class GateMode(StrEnum):
    GATED = "gated"
    AUTO = "auto"


class GatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_review: GateMode = GateMode.GATED
    output_review: GateMode = GateMode.GATED


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    content: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    version_id: NoNulStr | None = Field(default=None, max_length=256)


class PipelineConfigurable(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    # Assistant used to create the run. The graph itself does not branch on
    # this value, but persisting it lets later phase re-runs target the same
    # golden assistant instead of silently falling back to the base graph.
    assistant_id: NoNulStr = Field(default="pipeline", max_length=256)
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None
    environment_id: NoNulStr | None = Field(default=None, max_length=256)
    # Server-owned target resolved from environment_id at run authorization time.
    # It is checkpointed so a gated run cannot drift if the catalog changes later.
    environment_target: NoNulStr | None = Field(default=None, max_length=2_048)
    environment_target_version: int | None = None
    engine: NoNulStr = Field(default="sim", max_length=64)
    connections: dict[str, NoNulStr] = Field(default_factory=dict, max_length=16)

    # Phase selection: explicit list wins over start/stop range; default = all.
    phases: list[Phase] | None = Field(default=None, max_length=len(PHASE_ORDER))
    start_phase: Phase | None = None
    stop_after: Phase | None = None

    gates: dict[Phase, GatePolicy] = Field(default_factory=dict, max_length=len(PHASE_ORDER))
    prompt_overrides: dict[str, PromptOverride] = Field(default_factory=dict, max_length=32)
    pre_execution_context: list[NoNulStr] = Field(default_factory=list, max_length=32)
    model_by_phase: dict[Phase, NoNulStr] = Field(default_factory=dict, max_length=len(PHASE_ORDER))
    # Per-run LoadTestSpec overrides plus provider-specific execution options.
    # This is part of the durable run contract: execution may happen after one
    # or more human gates, when the original RunnableConfig is no longer
    # available unless it was checkpointed explicitly.
    load_test: dict[str, Any] = Field(default_factory=dict, max_length=16)
    # Agent backend selector (mirrors the engine selector). "stub" is the
    # deterministic, offline default; "anthropic" wires a real LLM but only takes
    # effect when an Anthropic key is configured (else it degrades to the stub).
    agent_backend: str = Field(default="stub", max_length=32)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("connections")
    @classmethod
    def validate_connections(cls, values: dict[str, str]) -> dict[str, str]:
        validate_json_object(values, label="pipeline connections", max_bytes=20_000)
        if any(not key or len(key) > 128 for key in values):
            raise ValueError("connection kinds must be 1-128 characters")
        if any(not value or len(value) > 256 for value in values.values()):
            raise ValueError("connection ids must be 1-256 characters")
        return values

    @field_validator("agent_backend")
    @classmethod
    def validate_agent_backend(cls, value: str) -> str:
        if value not in {"stub", "anthropic"}:
            raise ValueError("agent_backend must be 'stub' or 'anthropic'")
        return value

    @field_validator("pre_execution_context")
    @classmethod
    def validate_pre_execution_context(cls, values: list[str]) -> list[str]:
        if any(len(value) > 4_000 for value in values):
            raise ValueError("pre_execution_context entries must not exceed 4000 characters")
        return values

    @field_validator("prompt_overrides")
    @classmethod
    def validate_prompt_overrides(
        cls, values: dict[str, PromptOverride]
    ) -> dict[str, PromptOverride]:
        validate_json_object(
            {
                key: value.model_dump(mode="json", exclude_none=True)
                for key, value in values.items()
            },
            label="prompt_overrides",
            max_bytes=4_000_000,
            max_nodes=1_000,
        )
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
        validate_json_object(
            self.load_test,
            label="load_test configuration",
            max_bytes=20_000,
        )
        encoded: str | None = None
        try:
            encoded = json.dumps(self.load_test, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            pass
        if encoded is None:
            raise ValueError("load_test must contain finite JSON values")
        if len(encoded) > 20_000:
            raise ValueError("load_test configuration must not exceed 20000 characters")

        spec_fields = set(LoadTestSpec.model_fields) - {
            "idempotency_key",
            "target_environment",
            "script_refs",
        }
        spec_payload: dict[str, Any] = {"title": "pipeline load test"}
        spec_payload.update(
            {key: value for key, value in self.load_test.items() if key in spec_fields}
        )
        LoadTestSpec.model_validate(spec_payload)
        return self

    @model_validator(mode="after")
    def reject_credential_material(self) -> "PipelineConfigurable":
        """Reject secrets inherited from legacy assistant config as well as run input."""

        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("pipeline configuration must not contain credential material")
        return self

    @classmethod
    def from_config(cls, config: RunnableConfig | None) -> "PipelineConfigurable":
        if config is not None and type(config) is not dict:
            raise ValueError("pipeline run configuration is invalid")
        configurable = (config if config is not None else {}).get("configurable")
        if configurable is None:
            configurable = {}
        if type(configurable) is not dict:
            raise ValueError("pipeline run configuration is invalid")
        # Runnable config may carry framework-owned native objects alongside the
        # JSON fields. Type-gate keys before membership so a hostile legacy key
        # cannot run ``__eq__`` while we select the durable contract fields.
        known = {
            key: value
            for key, value in configurable.items()
            if type(key) is str and key in cls.model_fields
        }
        invalid_json = False
        try:
            validate_json_object(
                known,
                label="pipeline run configuration",
                max_bytes=5_000_000,
                max_nodes=20_000,
            )
        except ValueError:
            invalid_json = True
        if invalid_json:
            raise ValueError("pipeline run configuration is invalid")
        # Scan the raw bounded tree before Pydantic sees it.  Otherwise a secret
        # that also violates a field constraint is retained on ValidationError's
        # rejected-input chain even though the rendered public message is safe.
        if contains_credential_material(
            known,
            max_nodes=20_000,
            max_total_chars=5_000_000,
        ):
            raise ValueError("pipeline configuration must not contain credential material")
        parsed: PipelineConfigurable | None = None
        validation_summary: str | None = None
        try:
            parsed = cls.model_validate(known)
        except ValidationError as exc:
            validation_summary = validation_error_summary(exc)
        if parsed is None:
            detail = validation_summary or "validation failed"
            raise ValueError(f"pipeline run configuration is invalid: {detail}")
        # HTTP and LangGraph authorization stamp these fields for new runs. Keep
        # the invariant here as well because a legacy checkpoint can resume
        # directly at poll/cleanup/collection without re-entering engine_reserve
        # (and therefore without resolving the catalog environment again).
        if parsed.app_id is not None and parsed.project_id is None:
            raise ValueError("pipeline application scope requires project_id")
        if parsed.environment_id is not None and (
            parsed.project_id is None or parsed.app_id is None
        ):
            raise ValueError(
                "environment-scoped pipeline configuration is missing authoritative ownership"
            )
        return parsed

    @classmethod
    def from_state(
        cls,
        state: Any,
        config: RunnableConfig | None,
    ) -> "PipelineConfigurable":
        """Require live application config to match a checkpointed run contract.

        New runs always carry the full snapshot. Missing snapshots remain readable
        only for direct legacy/unit call sites; authenticated replay fails closed in
        ``PipelineReadService`` before it can reach a model or provider node.
        """

        current = cls.from_config(config)
        snapshot = state.get("run_config")
        if snapshot is None:
            return current
        if type(snapshot) is not dict:
            raise ValueError("checkpointed pipeline configuration is invalid")
        invalid_json = False
        try:
            validate_json_object(
                snapshot,
                label="checkpointed pipeline configuration",
                max_bytes=5_000_000,
                max_nodes=20_000,
            )
        except ValueError:
            invalid_json = True
        if invalid_json:
            raise ValueError("checkpointed pipeline configuration is invalid")
        # As with live config, scan before Pydantic can retain rejected secret
        # values in a ValidationError object reachable from the wrapper context.
        if contains_credential_material(
            snapshot,
            max_nodes=20_000,
            max_total_chars=5_000_000,
        ):
            raise ValueError(
                "checkpointed pipeline configuration must not contain credential material"
            )
        if set(snapshot) != set(cls.model_fields):
            raise ValueError("checkpointed pipeline configuration is incomplete")
        durable: PipelineConfigurable | None = None
        try:
            durable = cls.model_validate(snapshot)
        except (TypeError, ValueError):
            pass
        if durable is None:
            raise ValueError("checkpointed pipeline configuration is invalid")
        durable_snapshot = durable.snapshot()
        canonical_snapshot = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if (
            json.dumps(
                durable_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            != canonical_snapshot
        ):
            raise ValueError("checkpointed pipeline configuration is not canonical")
        if (
            json.dumps(
                current.snapshot(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            != canonical_snapshot
        ):
            raise ValueError("live pipeline configuration does not match its durable checkpoint")
        return durable

    @classmethod
    def from_state_for_phase(
        cls,
        state: Any,
        config: RunnableConfig | None,
        phase: Phase,
    ) -> "PipelineConfigurable":
        """Validate replay config and prove the current phase belongs to its plan.

        A durable config snapshot alone does not protect routing: a poisoned
        ``phases_plan`` could otherwise send a valid config into an unselected
        provider phase.  Legacy direct/unit call sites without ``run_config`` keep
        the compatibility behavior of :meth:`from_state`; authenticated replay
        already rejects those snapshots before graph execution.
        """

        parsed = cls.from_state(state, config)
        snapshot = state.get("run_config")
        if snapshot is None:
            return parsed
        plan = state.get("phases_plan")
        expected = [selected.value for selected in parsed.selected_phases()]
        if (
            type(plan) is not list
            or len(plan) != len(expected)
            or any(
                type(name) is not str
                or not 1 <= len(name) <= 64
                or "\x00" in name
                or contains_credential_material(name)
                for name in plan
            )
            or plan != expected
            or phase.value not in expected
        ):
            raise ValueError(
                "checkpointed pipeline phase plan does not match its durable configuration"
            )
        return parsed

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
