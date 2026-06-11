"""Execution-engine port (plan Part 1 "Execution engine port"; ADR-0002).

EngineHandle (apex.domain.pipeline) is the only state that may survive across
calls and process restarts: every method must be drivable from the handle alone
so the checkpointed poll loop can resume after a crash. provision() is
get-or-create by spec.idempotency_key — the write-ahead key in graph state makes
re-execution unable to double-start load.
"""

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

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


class LiveStats(BaseModel):
    """Normalized live metrics; engines that lack a metric report it as 0/None upstream."""

    vusers: float = 0.0
    tps: float = 0.0
    error_rate: float = 0.0
    p95_ms: float = 0.0


class EngineRunStatus(BaseModel):
    phase: EngineRunPhase
    progress_pct: float = 0.0
    live_stats: LiveStats | None = None
    message: str | None = None


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

    async def teardown(self, handle: EngineHandle) -> None: ...
