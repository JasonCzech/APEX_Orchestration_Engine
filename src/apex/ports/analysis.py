"""Version one analysis service contract. Results retain source attribution."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, validate_json_object


class AnalysisCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1]
    engines: list[Literal["loadrunner", "apex_load"]]
    evidence_kinds: list[NoNulStr]
    available: bool


class AnalysisJob(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    job_id: NoNulStr = Field(min_length=1, max_length=256)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    error: NoNulStr | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def safe_job(self):
        if contains_credential_material(self.model_dump(mode="json")):
            raise ValueError("Analysis job contains credential material")
        return self


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1]
    summary: NoNulStr = Field(min_length=1, max_length=12000)
    tool: NoNulStr = Field(min_length=1, max_length=128)
    tool_version: NoNulStr = Field(min_length=1, max_length=128)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    warnings: list[NoNulStr] = Field(default_factory=list, max_length=64)
    gaps: list[NoNulStr] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def safe_evidence(self):
        data = self.model_dump(mode="json")
        validate_json_object(
            data,
            label="analysis result",
            max_bytes=256000,
            max_nodes=4096,
            max_depth=12,
            max_key_chars=256,
        )
        if contains_credential_material(data, max_nodes=4096, max_total_chars=256000):
            raise ValueError("Analysis result contains credential material")
        return self


class AnalysisPort(Protocol):
    async def capabilities(self) -> AnalysisCapabilities: ...
    async def submit(self, request: dict[str, Any]) -> AnalysisJob: ...
    async def status(self, job_id: str) -> AnalysisJob: ...
    async def result(self, job_id: str) -> AnalysisResult: ...
