"""Public, credential-free dispatch receipts and bounded mutation input."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apex.domain.cycle_executions import PreflightCheck
from apex.domain.input_limits import RecordId


class DispatchCycleExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: RecordId


class CancelCycleDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispatch_id: RecordId


class CycleDispatchRead(BaseModel):
    id: str
    preparation_id: str
    request_key: str
    status: Literal[
        "reserved",
        "submitted",
        "uncertain",
        "running",
        "cleanup_pending",
        "completed",
        "failed",
        "aborted",
    ]
    created_by: str
    canceled_by: str | None = None
    created_at: datetime
    updated_at: datetime
    thread_id: str | None
    run_id: str | None
    engine_run_id: str | None
    external_run_id: str | None
    detail: str


class CycleDispatchWorkspace(BaseModel):
    revision: int
    checks: list[PreflightCheck]
    can_dispatch: bool
    can_resume: bool
    can_cancel: bool
    attempt: CycleDispatchRead | None
