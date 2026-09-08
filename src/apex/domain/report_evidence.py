"""Version-bound report evidence capture request."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apex.domain.input_limits import RecordId

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class FreezeReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    expected_results: dict[RecordId, Digest] = Field(min_length=1, max_length=20)
