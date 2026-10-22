"""Optional native APEX Load control-plane capability.

The execution-engine port remains intentionally small.  APEX Load additionally
offers scenarios, schedules, capacity planning, and rich run evidence; callers
that need those provider-native features resolve an execution-engine connection
and require this protocol explicitly.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from apex.domain.integrations import TestResultSummary
from apex.ports.artifact_store import ArtifactStorePort


@dataclass(frozen=True)
class NativeRunSnapshot:
    items: list[dict[str, Any]]
    complete: bool


@runtime_checkable
class ApexLoadControlPlanePort(Protocol):
    """Provider-native, project-scoped APEX Load operations.

    The facade validates and projects all returned objects before exposing them
    publicly.  Keeping this protocol dictionary-based avoids leaking APEX
    Load's fast-moving authoring schema into the engine-neutral domain model.
    """

    @property
    def native_control_enabled(self) -> bool: ...

    @property
    def native_project_id(self) -> str | None: ...

    async def probe(self) -> str: ...

    async def submit_native_analysis(
        self, run_id: str, *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def get_native_analysis(self, run_id: str, analysis_id: str) -> dict[str, Any]: ...

    async def native_capabilities(self) -> dict[str, Any]: ...

    async def list_native_scenarios(self) -> list[dict[str, Any]]: ...

    async def get_native_scenario(self, scenario_id: str) -> dict[str, Any]: ...

    async def create_native_scenario(
        self, draft: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def update_native_scenario(
        self, scenario_id: str, draft: dict[str, Any], *, expected_revision: int | None = None
    ) -> dict[str, Any]: ...

    async def delete_native_scenario(self, scenario_id: str) -> None: ...

    async def validate_native_scenario(self, draft: dict[str, Any]) -> dict[str, Any]: ...

    async def size_native_scenario(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def list_native_schedules(self, scenario_id: str) -> list[dict[str, Any]]: ...

    async def create_native_schedule(
        self, scenario_id: str, draft: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def update_native_schedule(
        self,
        scenario_id: str,
        schedule_id: str,
        draft: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]: ...

    async def delete_native_schedule(self, scenario_id: str, schedule_id: str) -> None: ...

    async def list_native_schedule_occurrences(
        self, scenario_id: str, schedule_id: str
    ) -> list[dict[str, Any]]: ...

    async def decide_native_schedule_approval(
        self, scenario_id: str, schedule_id: str, approval_id: str, *, approved: bool
    ) -> dict[str, Any]: ...

    async def native_schedule_health(self) -> dict[str, Any]: ...

    async def native_run_calendar(self) -> dict[str, Any]: ...

    async def list_native_runs(self, *, active_only: bool = False) -> list[dict[str, Any]]: ...

    async def scan_native_runs(
        self, *, active_only: bool = False, max_items: int | None = None
    ) -> NativeRunSnapshot: ...

    async def get_native_run(self, run_id: str) -> dict[str, Any]: ...

    async def native_run_summary(self, run_id: str) -> TestResultSummary: ...

    async def native_run_metrics(self, run_id: str) -> dict[str, Any]: ...

    async def native_run_error_evidence(self, run_id: str, signature: str) -> dict[str, Any]: ...

    async def collect_native_artifacts(
        self, run_id: str, idempotency_key: str, store: ArtifactStorePort
    ) -> list[dict[str, Any]]: ...

    def native_console_url(self, run_id: str) -> str: ...
