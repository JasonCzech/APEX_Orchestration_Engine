"""Version 1 computed outcome shared with APEX Load archive reports."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from apex.domain.input_limits import NoNulStr


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", hide_input_in_errors=True, strict=True, revalidate_instances="always"
    )


class Assessment(ContractModel):
    status: Literal["passed", "failed", "inconclusive", "not_assessed", "unavailable"]
    reason_code: NoNulStr = Field(min_length=1, max_length=128)
    explanation: NoNulStr = Field(max_length=20_000)
    criteria_revision: NoNulStr | None = Field(default=None, max_length=255)


class ExecutionOutcome(ContractModel):
    termination_reason: NoNulStr = Field(min_length=1, max_length=128)
    planned_duration_seconds: FiniteFloat | None = Field(default=None, ge=0)
    observed_duration_seconds: FiniteFloat | None = Field(default=None, ge=0)


class EvidenceOutcome(ContractModel):
    status: Literal["complete", "partial", "unavailable"]
    coverage_percent: FiniteFloat | None = Field(default=None, ge=0, le=100)


class ExecutionScope(ContractModel):
    project_id: NoNulStr | None = Field(default=None, max_length=255)
    application_id: NoNulStr | None = Field(default=None, max_length=255)
    release_id: NoNulStr | None = Field(default=None, max_length=255)


class IntegrationOutcome(ContractModel):
    scope: ExecutionScope | None = None
    schema_version: Literal[1]
    execution_id: NoNulStr = Field(min_length=1, max_length=255)
    observed_at: AwareDatetime = Field(strict=False)
    assessment: Assessment
    execution: ExecutionOutcome
    evidence: EvidenceOutcome

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value
