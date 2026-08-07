"""Project-scoped facade for APEX Load's optional native control plane."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.adapters.registry import PortKind
from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import Role
from apex.domain.diagnostics import contains_credential_material
from apex.domain.input_limits import NoNulStr, RecordId, validate_json_object
from apex.ports.apex_load_control import ApexLoadControlPlanePort
from apex.routers.connections import ConnectionsRepo
from apex.services.apex_load_native import get_apex_load_native_repository
from apex.services.connections import ConnectionResolver, close_adapter, get_connection_resolver

router = APIRouter(
    prefix="/apex-load/connections/{connection_id}",
    tags=["apex-load"],
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
index_router = APIRouter(
    prefix="/apex-load",
    tags=["apex-load"],
    dependencies=[Depends(require_role(Role.OPERATOR))],
)

ConnectionId = Annotated[RecordId, Path(description="Enabled APEX Load connection id")]
ResolverDep = Annotated[ConnectionResolver, Depends(get_connection_resolver)]
MAX_NATIVE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_NATIVE_LIST_ITEMS = 5_000


class NativeObjectRequest(BaseModel):
    """Opaque provider draft, bounded before it reaches the adapter."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=0, le=1_000_000_000)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_json_object(
            value,
            label="APEX Load control payload",
            max_bytes=MAX_NATIVE_RESPONSE_BYTES,
        )
        if contains_credential_material(value):
            raise ValueError("APEX Load control payload must not contain credential material")
        return value


class NativeApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class NativeConsoleLink(BaseModel):
    console_url: str


class NativeConnectionSummary(BaseModel):
    id: str
    name: str
    project_id: str


@index_router.get("/connections", operation_id="listApexLoadNativeConnections")
async def list_native_connections(
    identity: CurrentIdentity,
    repository: ConnectionsRepo,
) -> dict[str, list[NativeConnectionSummary]]:
    rows = await repository.list_connections(kind=PortKind.EXECUTION_ENGINE.value, limit=500)
    items = [
        NativeConnectionSummary(id=row.id, name=row.name, project_id=row.project_id)
        for row in rows
        if (
            row.enabled
            and row.provider == "apex_load"
            and row.project_id is not None
            and row.options.get("native_control_enabled") is True
            and identity.allows_project(row.project_id)
        )
    ]
    return {"items": items}


@dataclass
class ResolvedApexLoadControl:
    adapter: ApexLoadControlPlanePort
    connection_id: str
    project_id: str
    connection_version: datetime
    remote_project_id: str


async def _resolve_control(
    connection_id: ConnectionId,
    identity: CurrentIdentity,
    repository: ConnectionsRepo,
    resolver: ResolverDep,
) -> AsyncIterator[ResolvedApexLoadControl]:
    """Resolve exactly one opted-in, persisted, project-scoped adapter lease."""

    row = await repository.get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="APEX Load connection not found")
    if (
        not row.enabled
        or row.kind != PortKind.EXECUTION_ENGINE.value
        or row.provider != "apex_load"
    ):
        raise HTTPException(status_code=409, detail="connection is not an enabled APEX Load engine")
    project_id = row.project_id
    if project_id is None:
        raise HTTPException(
            status_code=409,
            detail="APEX Load native control requires a project-scoped connection",
        )
    ensure_scope(identity, project_id=project_id)

    adapter: Any = None
    try:
        resolved = await resolver.resolve_with_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id=connection_id,
            project_id=project_id,
            expected_provider="apex_load",
        )
        adapter = resolved.adapter
        if resolved.connection_id != connection_id or not resolved.persisted:
            raise RuntimeError("APEX Load connection resolution did not preserve affinity")
        if not isinstance(adapter, ApexLoadControlPlanePort):
            raise RuntimeError("resolved adapter does not expose APEX Load native control")
        if not adapter.native_control_enabled:
            raise HTTPException(status_code=409, detail="APEX Load native control is disabled")
        remote_project_id = adapter.native_project_id
        if type(remote_project_id) is not str or not remote_project_id:
            raise HTTPException(
                status_code=409, detail="APEX Load provider project binding is missing"
            )
        connection_version = resolved.connection_version
        if not isinstance(connection_version, datetime):
            raise RuntimeError("APEX Load persisted connection has no version")
        yield ResolvedApexLoadControl(
            adapter=adapter,
            connection_id=connection_id,
            project_id=project_id,
            connection_version=connection_version,
            remote_project_id=remote_project_id,
        )
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="APEX Load connection not found") from None
    except ValueError:
        raise HTTPException(
            status_code=409, detail="APEX Load connection configuration is invalid"
        ) from None
    except Exception:
        raise HTTPException(status_code=502, detail="APEX Load connection is unavailable") from None
    finally:
        if adapter is not None:
            await close_adapter(adapter)


ControlDep = Annotated[ResolvedApexLoadControl, Depends(_resolve_control)]


def _safe_provider_object(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise HTTPException(status_code=502, detail=f"APEX Load {label} response is invalid")
    try:
        validate_json_object(
            value,
            label=f"APEX Load {label} response",
            max_bytes=MAX_NATIVE_RESPONSE_BYTES,
        )
    except ValueError:
        raise HTTPException(
            status_code=502, detail=f"APEX Load {label} response is invalid"
        ) from None
    if contains_credential_material(value):
        raise HTTPException(status_code=502, detail=f"APEX Load {label} response is unsafe")
    return value


def _safe_provider_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > MAX_NATIVE_LIST_ITEMS:
        raise HTTPException(status_code=502, detail=f"APEX Load {label} response is invalid")
    result: list[dict[str, Any]] = []
    for item in value:
        result.append(_safe_provider_object(item, label=label))
    return result


async def _provider_call(operation: Callable[[], Awaitable[Any]], *, label: str) -> Any:
    """Normalize provider failures without reflecting provider diagnostics."""

    try:
        return await operation()
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail=f"APEX Load {label} was not found") from None
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid APEX Load {label} request") from None
    except Exception:
        raise HTTPException(status_code=502, detail=f"APEX Load {label} is unavailable") from None


@router.get("/capabilities", operation_id="getApexLoadCapabilities")
async def get_capabilities(control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(control.adapter.native_capabilities, label="capabilities")
    return _safe_provider_object(value, label="capabilities")


@router.get("/scenarios", operation_id="listApexLoadScenarios")
async def list_scenarios(control: ControlDep) -> dict[str, Any]:
    scenarios = await _provider_call(control.adapter.list_native_scenarios, label="scenarios")
    items = _safe_provider_list(scenarios, label="scenarios")
    return {"items": items, "count": len(items)}


@router.post("/scenarios", operation_id="createApexLoadScenario")
async def create_scenario(body: NativeObjectRequest, control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.create_native_scenario(body.payload), label="scenario"
    )
    return _safe_provider_object(value, label="scenario")


@router.post("/scenarios/validation", operation_id="validateApexLoadScenario")
async def validate_scenario(body: NativeObjectRequest, control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.validate_native_scenario(body.payload), label="scenario validation"
    )
    return _safe_provider_object(value, label="scenario validation")


@router.post("/scenarios/sizing", operation_id="sizeApexLoadScenario")
async def size_scenario(body: NativeObjectRequest, control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.size_native_scenario(body.payload), label="scenario sizing"
    )
    return _safe_provider_object(value, label="scenario sizing")


@router.get("/scenarios/{scenario_id}", operation_id="getApexLoadScenario")
async def get_scenario(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)], control: ControlDep
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.get_native_scenario(scenario_id), label="scenario"
    )
    return _safe_provider_object(value, label="scenario")


@router.put("/scenarios/{scenario_id}", operation_id="updateApexLoadScenario")
async def update_scenario(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    body: NativeObjectRequest,
    control: ControlDep,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.update_native_scenario(
            scenario_id, body.payload, expected_revision=body.expected_revision
        ),
        label="scenario",
    )
    return _safe_provider_object(value, label="scenario")


@router.delete("/scenarios/{scenario_id}", status_code=204, operation_id="deleteApexLoadScenario")
async def delete_scenario(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)], control: ControlDep
) -> None:
    await _provider_call(
        lambda: control.adapter.delete_native_scenario(scenario_id), label="scenario"
    )


@router.get("/scenarios/{scenario_id}/schedules", operation_id="listApexLoadSchedules")
async def list_schedules(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)], control: ControlDep
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.list_native_schedules(scenario_id), label="schedules"
    )
    items = _safe_provider_list(value, label="schedules")
    return {"items": items, "count": len(items)}


@router.post("/scenarios/{scenario_id}/schedules", operation_id="createApexLoadSchedule")
async def create_schedule(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    body: NativeObjectRequest,
    control: ControlDep,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.create_native_schedule(scenario_id, body.payload), label="schedule"
    )
    return _safe_provider_object(value, label="schedule")


@router.put(
    "/scenarios/{scenario_id}/schedules/{schedule_id}", operation_id="updateApexLoadSchedule"
)
async def update_schedule(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    schedule_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    body: NativeObjectRequest,
    control: ControlDep,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.update_native_schedule(
            scenario_id,
            schedule_id,
            body.payload,
            expected_revision=body.expected_revision,
        ),
        label="schedule",
    )
    return _safe_provider_object(value, label="schedule")


@router.delete(
    "/scenarios/{scenario_id}/schedules/{schedule_id}",
    status_code=204,
    operation_id="deleteApexLoadSchedule",
)
async def delete_schedule(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    schedule_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    control: ControlDep,
) -> None:
    await _provider_call(
        lambda: control.adapter.delete_native_schedule(scenario_id, schedule_id), label="schedule"
    )


@router.get(
    "/scenarios/{scenario_id}/schedules/{schedule_id}/occurrences",
    operation_id="listApexLoadScheduleOccurrences",
)
async def list_schedule_occurrences(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    schedule_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    control: ControlDep,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.list_native_schedule_occurrences(scenario_id, schedule_id),
        label="schedule occurrences",
    )
    items = _safe_provider_list(value, label="schedule occurrences")
    return {"items": items, "count": len(items)}


@router.post(
    "/scenarios/{scenario_id}/schedules/{schedule_id}/approvals/{approval_id}",
    operation_id="decideApexLoadScheduleApproval",
)
async def decide_schedule_approval(
    scenario_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    schedule_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    approval_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    body: NativeApprovalDecision,
    control: ControlDep,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.decide_native_schedule_approval(
            scenario_id, schedule_id, approval_id, approved=body.approved
        ),
        label="schedule approval",
    )
    return _safe_provider_object(value, label="schedule approval")


@router.get("/schedule-health", operation_id="getApexLoadScheduleHealth")
async def get_schedule_health(control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(control.adapter.native_schedule_health, label="schedule health")
    return _safe_provider_object(value, label="schedule health")


@router.get("/calendar", operation_id="getApexLoadRunCalendar")
async def get_run_calendar(control: ControlDep) -> dict[str, Any]:
    value = await _provider_call(control.adapter.native_run_calendar, label="run calendar")
    return _safe_provider_object(value, label="run calendar")


@router.get("/runs", operation_id="listApexLoadRuns")
async def list_runs(
    control: ControlDep,
    active_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.list_native_runs(active_only=active_only), label="runs"
    )
    items = _safe_provider_list(value, label="runs")
    for item in items:
        run_id = item.get("id")
        if type(run_id) is str:
            item["console_url"] = control.adapter.native_console_url(run_id)
    return {"items": items, "count": len(items)}


@router.get("/mirrored-runs", operation_id="listMirroredApexLoadRuns")
async def list_mirrored_runs(
    control: ControlDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> dict[str, Any]:
    """Read the local, crash-safe native-run ledger rather than provider metrics."""

    rows = await get_apex_load_native_repository().list_runs(
        connection_id=control.connection_id,
        project_id=control.project_id,
        limit=limit,
        offset=offset,
    )
    items = [
        {
            "id": row.remote_id,
            "status": row.status,
            "terminal": row.terminal,
            "revision": row.revision,
            "scenario_id": row.scenario_remote_id,
            "schedule_id": row.schedule_remote_id,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "last_seen_at": row.last_seen_at.isoformat(),
            "connection_version": row.connection_version.isoformat(),
            "pipeline_status": row.pipeline_status,
            "pipeline_thread_id": row.pipeline_thread_id,
            "pipeline_run_id": row.pipeline_run_id,
            "console_url": (
                control.adapter.native_console_url(row.remote_id)
                if (
                    row.status != "missing"
                    and row.connection_version == control.connection_version
                    and row.remote_project_id == control.remote_project_id
                )
                else None
            ),
        }
        for row in rows
    ]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}/metrics", operation_id="getApexLoadRunMetrics")
async def get_run_metrics(
    run_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)], control: ControlDep
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.native_run_metrics(run_id), label="run metrics"
    )
    return _safe_provider_object(value, label="run metrics")


@router.get("/runs/{run_id}/evidence", operation_id="getApexLoadRunEvidence")
async def get_run_evidence(
    run_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)],
    control: ControlDep,
    signature: Annotated[NoNulStr, Query(min_length=1, max_length=255)],
) -> dict[str, Any]:
    value = await _provider_call(
        lambda: control.adapter.native_run_error_evidence(run_id, signature), label="run evidence"
    )
    return _safe_provider_object(value, label="run evidence")


@router.get(
    "/runs/{run_id}/console",
    operation_id="getApexLoadRunConsole",
    response_model=NativeConsoleLink,
)
async def get_run_console(
    run_id: Annotated[NoNulStr, Path(min_length=1, max_length=255)], control: ControlDep
) -> NativeConsoleLink:
    await _provider_call(lambda: control.adapter.get_native_run(run_id), label="run")
    try:
        console_url = control.adapter.native_console_url(run_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid APEX Load run id") from None
    return NativeConsoleLink(console_url=console_url)
