"""Scoped execution preparations and exact links into the dispatch lifecycle."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update

from apex.app.dependencies import CurrentIdentity
from apex.domain.cycle_executions import (
    ArchiveExecutionPreparation,
    ExecutionSnapshot,
    LinkCycleRun,
    PreflightCheck,
    PrepareExecution,
    execution_checks,
    prepare_snapshot,
)
from apex.domain.cycles import version_digest
from apex.domain.input_limits import RecordId
from apex.persistence.models import (
    Connection,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    CycleRunLink,
    CycleScriptArtifact,
    EngineRun,
    EnvironmentSnapshot,
    TestCycle,
)
from apex.persistence.repositories.catalog import CatalogRepository
from apex.routers.cycles import Operator, Session, enabled, find
from apex.services.cycle_dispatch import verified_dispatch_runs
from apex.services.cycle_results import CycleResult, compare_result, result_view
from apex.services.environments import EnvironmentTargetNotFoundError, resolve_environment_target

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


class ConnectionChoice(BaseModel):
    id: str
    name: str
    provider: str
    runtime_version: datetime


class RunChoice(BaseModel):
    id: str
    thread_id: str
    engine: str
    attempt: int
    status: str
    started_at: datetime
    bindings_verified: bool = False


class PreparationRead(BaseModel):
    id: str
    created_at: datetime
    created_by: str
    snapshot: ExecutionSnapshot
    checks: list[PreflightCheck]
    checked_at: datetime
    archived_at: datetime | None = None
    archived_by: str | None = None
    # Per-preparation dispatch endpoint owns the live capability decision.
    launch_available: bool | None = None


class ExecutionWorkspaceRead(BaseModel):
    revision: int
    preparations: list[PreparationRead]
    connections: list[ConnectionChoice]
    linked_runs: list[RunChoice]
    recent_runs: list[RunChoice]
    inventory_snapshot_id: str | None
    inventory_scanned_at: datetime | None
    target_version: int | None


async def context(session: Session, cycle: TestCycle):
    try:
        target = await resolve_environment_target(
            CatalogRepository(session),
            cycle.environment_id,
            project_id=cycle.project_id,
            app_id=cycle.app_id,
        )
    except EnvironmentTargetNotFoundError:
        target = None
    connections = (
        (
            await session.execute(
                select(
                    Connection.id, Connection.name, Connection.provider, Connection.runtime_version
                )
                .where(
                    Connection.kind == "execution_engine",
                    Connection.enabled.is_(True),
                    Connection.provider.in_(["apex_load", "loadrunner"]),
                    or_(Connection.project_id == cycle.project_id, Connection.project_id.is_(None)),
                )
                .order_by(Connection.name, Connection.id)
            )
        )
        .mappings()
        .all()
    )
    inventory = await session.scalar(
        select(EnvironmentSnapshot)
        .where(EnvironmentSnapshot.environment_id == cycle.environment_id)
        .order_by(EnvironmentSnapshot.scanned_at.desc(), EnvironmentSnapshot.id)
        .limit(1)
    )
    return target, [ConnectionChoice.model_validate(c) for c in connections], inventory


def run_scope(cycle: TestCycle):
    return (
        EngineRun.project_id == cycle.project_id,
        EngineRun.app_id == cycle.app_id,
        EngineRun.ownership_known.is_(True),
        EngineRun.scope_ownership_known.is_(True),
    )


def linked_run_scope(cycle: TestCycle):
    """One shared exact-attempt projection for workspace, results and report capture."""
    return or_(
        EngineRun.id.in_(
            select(CycleRunLink.engine_run_id).where(CycleRunLink.cycle_id == cycle.id)
        ),
        and_(
            EngineRun.attempt == 1,
            EngineRun.thread_id.in_(
                select(CycleExecutionDispatch.thread_id).where(
                    CycleExecutionDispatch.cycle_id == cycle.id
                )
            ),
        ),
    )


def run_view(run: EngineRun, *, bindings_verified: bool = False) -> RunChoice:
    return RunChoice(
        **{
            name: getattr(run, name)
            for name in RunChoice.model_fields
            if name != "bindings_verified"
        },
        bindings_verified=bindings_verified,
    )


@router.get(
    "/{cycle_id}/executions",
    response_model=ExecutionWorkspaceRead,
    operation_id="getCycleExecutions",
)
async def read_executions(cycle_id: RecordId, session: Session, identity: CurrentIdentity):
    cycle = await find(session, identity, cycle_id)
    target, connections, inventory = await context(session, cycle)
    rows = (
        await session.scalars(
            select(CycleExecutionPreparation)
            .where(CycleExecutionPreparation.cycle_id == cycle.id)
            .order_by(CycleExecutionPreparation.created_at.desc(), CycleExecutionPreparation.id)
        )
    ).all()
    checked_at = datetime.now(UTC)
    preparations = []
    for row in rows:
        snapshot = ExecutionSnapshot.model_validate(row.snapshot)
        connection = next(
            (
                c
                for c in connections
                if c.id == snapshot.connection_id
                and c.provider == snapshot.engine.replace("-", "_")
            ),
            None,
        )
        preparations.append(
            PreparationRead(
                id=row.id,
                created_at=row.created_at,
                created_by=row.created_by,
                snapshot=snapshot,
                checked_at=checked_at,
                archived_at=row.archived_at,
                archived_by=row.archived_by,
                launch_available=False if row.archived_at is not None else None,
                checks=execution_checks(
                    snapshot,
                    cycle.versions,
                    target_version=target.version if target else None,
                    target_valid=target is not None,
                    connection_valid=connection is not None,
                    connection_version=connection.runtime_version.isoformat()
                    if connection
                    else None,
                    inventory_id=inventory.id if inventory else None,
                    inventory_at=inventory.scanned_at if inventory else None,
                    now=checked_at,
                )
                + (
                    [
                        PreflightCheck(
                            key="archived",
                            label="Archived preparation",
                            status="blocked",
                            detail=(
                                "This preparation is retained for audit "
                                "and cannot be used for new work."
                            ),
                        )
                    ]
                    if row.archived_at is not None
                    else []
                ),
            )
        )
    linked = (
        await session.scalars(
            select(EngineRun)
            .where(
                linked_run_scope(cycle),
                *run_scope(cycle),
            )
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
        )
    ).all()
    recent = (
        await session.scalars(
            select(EngineRun)
            .where(*run_scope(cycle))
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
            .limit(50)
        )
    ).all()
    verified = await verified_dispatch_runs(session, cycle, list(linked) + list(recent))
    return ExecutionWorkspaceRead(
        revision=cycle.revision,
        preparations=preparations,
        connections=connections,
        linked_runs=[run_view(r, bindings_verified=r.id in verified) for r in linked],
        recent_runs=[run_view(r, bindings_verified=r.id in verified) for r in recent],
        inventory_snapshot_id=inventory.id if inventory else None,
        inventory_scanned_at=inventory.scanned_at if inventory else None,
        target_version=target.version if target else None,
    )


async def reserve_revision(session: Session, cycle: TestCycle, revision: int):
    changed = await session.scalar(
        update(TestCycle)
        .where(TestCycle.id == cycle.id, TestCycle.revision == revision)
        .values(revision=revision + 1)
        .returning(TestCycle.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        await session.rollback()
        raise HTTPException(409, "Cycle changed; refresh and review before preparing execution")


class ExecutionMutationRead(BaseModel):
    id: str
    revision: int


@router.post(
    "/{cycle_id}/executions",
    response_model=ExecutionMutationRead,
    operation_id="prepareCycleExecution",
)
async def prepare_execution(
    cycle_id: RecordId, body: PrepareExecution, session: Session, identity: Operator
):
    cycle = await find(session, identity, cycle_id)
    fingerprint = version_digest(body.model_dump(exclude={"expected_revision", "idempotency_key"}))
    existing = await session.scalar(
        select(CycleExecutionPreparation).where(
            CycleExecutionPreparation.cycle_id == cycle.id,
            CycleExecutionPreparation.idempotency_key == body.idempotency_key,
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency key already belongs to a different preparation")
        return ExecutionMutationRead(id=existing.id, revision=cycle.revision)
    if cycle.revision != body.expected_revision:
        raise HTTPException(409, "Cycle changed; refresh before preparing execution")
    count = await session.scalar(
        select(func.count())
        .select_from(CycleExecutionPreparation)
        .where(CycleExecutionPreparation.cycle_id == cycle.id)
    )
    if count is not None and count >= 100:
        raise HTTPException(
            409, "Cycle preparation history limit reached, including archived preparations"
        )
    target, connections, _ = await context(session, cycle)
    connection = next((c for c in connections if c.id == body.connection_id), None)
    if body.connection_id and connection is None:
        raise HTTPException(422, "Select an available execution connection")
    if cycle.mode == "simulation" and body.connection_id:
        raise HTTPException(422, "Simulation preparations must not select a live connection")
    if cycle.mode not in ("live", "simulation"):
        raise HTTPException(409, "Cycle mode is invalid")
    try:
        snapshot = prepare_snapshot(
            body,
            cycle.versions,
            mode=cycle.mode,
            environment_id=cycle.environment_id,
            environment_version=target.version if target else None,
            connection_version=connection.runtime_version.isoformat() if connection else None,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if connection and connection.provider != snapshot.engine.replace("-", "_"):
        raise HTTPException(422, "Connection provider does not match the approved script")
    artifact = await session.scalar(
        select(CycleScriptArtifact).where(
            CycleScriptArtifact.cycle_id == cycle.id,
            CycleScriptArtifact.version_id == snapshot.script_version_id,
            CycleScriptArtifact.version_checksum == snapshot.script_checksum,
            CycleScriptArtifact.source_checksum == snapshot.script_source_checksum,
            CycleScriptArtifact.stored_at.is_not(None),
        )
    )
    if artifact is not None:
        snapshot = snapshot.model_copy(
            update={
                "script_artifact_id": artifact.id,
                "script_artifact_stored_at": artifact.stored_at.isoformat()
                if artifact.stored_at
                else None,
            }
        )
    await reserve_revision(session, cycle, body.expected_revision)
    row = CycleExecutionPreparation(
        cycle_id=cycle.id,
        idempotency_key=body.idempotency_key,
        request_fingerprint=fingerprint,
        snapshot=snapshot.model_dump(mode="json"),
        created_by=identity.consumer_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ExecutionMutationRead(id=row.id, revision=body.expected_revision + 1)


@router.post(
    "/{cycle_id}/executions/{preparation_id}/archive",
    response_model=ExecutionMutationRead,
    operation_id="archiveCycleExecutionPreparation",
)
async def archive_preparation(
    cycle_id: RecordId,
    preparation_id: RecordId,
    body: ArchiveExecutionPreparation,
    session: Session,
    identity: Operator,
):
    """Retain bindings and validation history; prevent new work on this preparation."""
    cycle = await find(session, identity, cycle_id)
    preparation = await session.scalar(
        select(CycleExecutionPreparation).where(
            CycleExecutionPreparation.id == preparation_id,
            CycleExecutionPreparation.cycle_id == cycle.id,
        )
    )
    if preparation is None:
        raise HTTPException(404, "Execution preparation not found")
    if preparation.archived_at is not None:
        return ExecutionMutationRead(id=preparation.id, revision=cycle.revision)
    active = await session.scalar(
        select(CycleExecutionDispatch.id).where(
            CycleExecutionDispatch.preparation_id == preparation.id,
            CycleExecutionDispatch.active_environment_id.is_not(None),
        )
    )
    if active:
        raise HTTPException(
            409,
            "This preparation has an active or uncertain dispatch; confirm its run has "
            "stopped before archiving",
        )
    await reserve_revision(session, cycle, body.expected_revision)
    preparation.archived_at = datetime.now(UTC)
    preparation.archived_by = identity.consumer_id
    await session.commit()
    return ExecutionMutationRead(id=preparation.id, revision=body.expected_revision + 1)


@router.post("/{cycle_id}/runs", response_model=ExecutionMutationRead, operation_id="linkCycleRun")
async def link_run(cycle_id: RecordId, body: LinkCycleRun, session: Session, identity: Operator):
    cycle = await find(session, identity, cycle_id)
    run = await session.scalar(
        select(EngineRun).where(EngineRun.id == body.engine_run_id, *run_scope(cycle))
    )
    if run is None:
        raise HTTPException(404, "Run not found in this cycle's project and application")
    existing = await session.scalar(
        select(CycleRunLink).where(
            CycleRunLink.cycle_id == cycle.id, CycleRunLink.engine_run_id == run.id
        )
    )
    if existing:
        return ExecutionMutationRead(id=existing.id, revision=cycle.revision)
    count = await session.scalar(
        select(func.count()).select_from(CycleRunLink).where(CycleRunLink.cycle_id == cycle.id)
    )
    if count is not None and count >= 100:
        raise HTTPException(409, "Cycle run-link limit reached")
    await reserve_revision(session, cycle, body.expected_revision)
    row = CycleRunLink(cycle_id=cycle.id, engine_run_id=run.id, created_by=identity.consumer_id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ExecutionMutationRead(id=row.id, revision=body.expected_revision + 1)


class CycleResultsRead(BaseModel):
    revision: int
    read_at: datetime
    baseline_id: str | None
    results: list[CycleResult]
    evidence_source: Literal["engine_run_history"] = "engine_run_history"


@router.get("/{cycle_id}/results", response_model=CycleResultsRead, operation_id="getCycleResults")
async def read_results(
    cycle_id: RecordId,
    session: Session,
    identity: CurrentIdentity,
    baseline_id: RecordId | None = None,
):
    cycle = await find(session, identity, cycle_id)
    rows = (
        await session.scalars(
            select(EngineRun)
            .where(
                linked_run_scope(cycle),
                *run_scope(cycle),
            )
            .order_by(EngineRun.started_at.desc(), EngineRun.id)
        )
    ).all()
    verified = await verified_dispatch_runs(session, cycle, list(rows))
    results = [result_view(row, bindings_verified=row.id in verified) for row in rows]
    if baseline_id:
        baseline = next((r for r in results if r.id == baseline_id), None)
        if baseline is None:
            raise HTTPException(404, "Baseline not found among accessible linked attempts")
        results = [compare_result(result, baseline) for result in results]
    return CycleResultsRead(
        revision=cycle.revision, read_at=datetime.now(UTC), baseline_id=baseline_id, results=results
    )
