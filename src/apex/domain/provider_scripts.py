"""Provider export provenance is separate from executable-content certification."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, RecordId


class ProviderScriptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: NoNulStr = Field(min_length=1, max_length=255)
    name: NoNulStr = Field(min_length=1, max_length=255)
    project_id: NoNulStr = Field(min_length=1, max_length=255)
    version: NoNulStr = Field(max_length=255)
    protocol: NoNulStr = Field(max_length=64)


class ProviderScriptExport(ProviderScriptSummary):
    source: NoNulStr = Field(min_length=1, max_length=50_000)
    source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_scope: Literal["provider_export"] = "provider_export"
    executable_content_verified: Literal[False] = False


class ImportProviderScript(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    connection_id: RecordId
    connection_version: datetime
    remote_script_id: NoNulStr = Field(min_length=1, max_length=255)
    expected_source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_version_id: RecordId
    title: NoNulStr = Field(min_length=1, max_length=255)
    parameters: NoNulStr = Field(min_length=1, max_length=10_000)
    coverage: NoNulStr = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def safe(self) -> "ImportProviderScript":
        if self.connection_version.tzinfo is None:
            raise ValueError("Connection generation requires a timezone")
        if contains_credential_material(self.model_dump()):
            raise ValueError("Script import metadata must not contain credentials")
        return self
