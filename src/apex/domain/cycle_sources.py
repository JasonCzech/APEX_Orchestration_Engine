"""Exact source revision capture request."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from apex.domain.input_limits import RecordId
from apex.domain.integrations import DocRef


class CaptureSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: RecordId
    connection_id: RecordId
    connection_version: datetime
    ref: DocRef
    expected_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=500)
