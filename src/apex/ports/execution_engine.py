"""Execution-engine port (plan Part 1 "Execution engine port"; ADR-0002).

EngineHandle (apex.domain.pipeline) is the only state that may survive across
calls and process restarts: every method must be drivable from the handle alone
so the checkpointed poll loop can resume after a crash. provision() is
get-or-create by spec.idempotency_key — the write-ahead key in graph state makes
re-execution unable to double-start load.
"""

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from apex.domain.input_limits import NoNulStr
from apex.domain.integrations import LoadTestSpec, TestResultSummary, ValidationReport
from apex.domain.pipeline import EngineHandle
from apex.ports.artifact_store import ArtifactStorePort


class EngineRunPhase(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    COLLECTING = "collecting"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_ENGINE_PHASES = frozenset(
    {EngineRunPhase.COMPLETED, EngineRunPhase.FAILED, EngineRunPhase.ABORTED}
)


class EngineProviderRunNotFoundError(KeyError):
    """The provider definitively reports that the exact external run is gone.

    This is deliberately narrower than ``KeyError``: parser bugs and malformed
    provider responses must never be mistaken for proof that a load run stopped.
    """


class LiveStats(BaseModel):
    """Normalized live metrics; engines that lack a metric report it as 0/None upstream."""

    model_config = ConfigDict(extra="forbid")

    vusers: FiniteFloat = Field(default=0.0, ge=0, le=1_000_000_000)
    tps: FiniteFloat = Field(default=0.0, ge=0, le=1_000_000_000_000)
    error_rate: FiniteFloat = Field(default=0.0, ge=0, le=1)
    p95_ms: FiniteFloat = Field(default=0.0, ge=0, le=1_000_000_000_000)


class EngineRunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: EngineRunPhase
    progress_pct: FiniteFloat = Field(default=0.0, ge=0, le=100)
    live_stats: LiveStats | None = None
    message: NoNulStr | None = Field(default=None, max_length=4_096)


@runtime_checkable
class ExecutionEnginePort(Protocol):
    async def validate(self, spec: LoadTestSpec) -> ValidationReport: ...

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        """Get-or-create by spec.idempotency_key; safe to re-execute after a crash."""
        ...

    async def start(self, handle: EngineHandle) -> None: ...

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        """Cheap and poll-safe: called every limits.poll_interval_s for hours."""
        ...

    async def abort(self, handle: EngineHandle, *, reason: str) -> None: ...

    async def collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        """Persist engine outputs via the store; returns ArtifactRef-shaped dicts."""
        ...

    async def fetch_summary(self, handle: EngineHandle) -> TestResultSummary: ...

    async def teardown(self, handle: EngineHandle) -> None:
        """Release provider-side resources idempotently; safe to repeat after a crash."""
        ...
