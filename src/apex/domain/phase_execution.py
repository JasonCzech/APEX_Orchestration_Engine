"""Versioned ownership and analysis configuration for full lifecycle runs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.input_limits import NoNulStr
from apex.domain.pipeline import PHASE_ORDER, Phase


class PhaseExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    owners: dict[Phase, Literal["langgraph", "hitl"]]

    @model_validator(mode="after")
    def complete_map(self):
        if set(self.owners) != set(PHASE_ORDER):
            raise ValueError("phase_execution.owners must contain all seven phases")
        return self


class AnalysisSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["lra_engine", "internal_rca", "perfreportforge", "other"]
    connection_id: NoNulStr = Field(min_length=1, max_length=256)


class AnalysisConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    selections: list[AnalysisSelection] = Field(default_factory=list, max_length=16)
    timeout_s: int = Field(default=1800, ge=1, le=86400)

    @model_validator(mode="after")
    def distinct_tools(self):
        keys = [(s.tool, s.connection_id if s.tool == "other" else "") for s in self.selections]
        if len(keys) != len(set(keys)) or len({s.connection_id for s in self.selections}) != len(
            self.selections
        ):
            raise ValueError("analysis selections must use distinct tools and connections")
        return self

    def validate_engine(self, engine: str) -> None:
        allowed = {
            "loadrunner": {"lra_engine", "perfreportforge", "other"},
            "apex_load": {"internal_rca", "perfreportforge", "other"},
            "sim": set(),
        }
        if any(s.tool not in allowed.get(engine, set()) for s in self.selections):
            raise ValueError("analysis tool is incompatible with the execution engine")
