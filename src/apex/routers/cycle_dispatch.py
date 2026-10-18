"""Authenticated dispatch of one exact preparation into the durable pipeline."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from apex.app.dependencies import CurrentIdentity
from apex.auth.identity import Role
from apex.domain.cycle_dispatch import (
    CancelCycleDispatch,
    CycleDispatchWorkspace,
    DispatchCycleExecution,
)
from apex.domain.cycle_executions import PreflightCheck
from apex.domain.input_limits import RecordId
from apex.persistence.models import CycleExecutionDispatch
from apex.routers.cycle_executions import reserve_revision
from apex.routers.cycles import Operator, Session, enabled, find
from apex.routers.pipelines import _pipeline_read_service_after_scope
from apex.services.cycle_dispatch import dispatch_context, dispatch_read, sync_dispatch

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


async def workspace(cycle_id, preparation_id, session, identity, pipeline):
    cycle, _, _, _, checks = await dispatch_context(session, identity, cycle_id, preparation_id)
    row = await session.scalar(
        select(CycleExecutionDispatch).where(
            CycleExecutionDispatch.preparation_id == preparation_id,
            CycleExecutionDispatch.cycle_id == cycle_id,
        )
    )
    engine = await sync_dispatch(session, row, pipeline) if row else None
    occupied = await session.scalar(
        select(CycleExecutionDispatch).where(
            CycleExecutionDispatch.active_environment_id == cycle.environment_id,
            CycleExecutionDispatch.preparation_id != preparation_id,
        )
    )
    if occupied:
        await sync_dispatch(session, occupied, pipeline)
        if occupied.active_environment_id is None:
            occupied = None
    if occupied:
        checks.append(
            PreflightCheck(
                key="environment_busy",
                label="Environment execution ownership",
                status="blocked",
                detail=(
                    "Another cycle execution owns this environment. Wait for confirmed "
                    "completion or use its run to request a confirmed abort."
                ),
            )
        )
    if not identity.role.at_least(Role.OPERATOR):
        checks.append(
            PreflightCheck(
                key="launch_permission",
                label="Launch permission",
                status="blocked",
                detail=(
                    "Operator access to this cycle is required to launch or resume an execution."
                ),
            )
        )
    ready = not any(c.status == "blocked" for c in checks)
    return CycleDispatchWorkspace(
        revision=cycle.revision,
        checks=checks,
        can_dispatch=ready and row is None,
        # Once thread-bound, explicit replay must remain possible even when
        # future launch prerequisites changed. It adopts the exact root run, or
        # starts that same graph whose durable binding check fails before provider
        # work. A never-bound blocked intent can be canceled instead.
        can_resume=identity.role.at_least(Role.OPERATOR)
        and row is not None
        and row.status in {"reserved", "uncertain"}
        and (ready or row.thread_id is not None),
        can_cancel=identity.role.at_least(Role.OPERATOR)
        and row is not None
        and row.status in {"reserved", "uncertain"}
        and row.thread_id is None,
        attempt=dispatch_read(row, engine) if row else None,
    )


@router.get(
    "/{cycle_id}/executions/{preparation_id}/dispatch",
    response_model=CycleDispatchWorkspace,
    operation_id="getCycleExecutionDispatch",
)
async def read_dispatch(
    cycle_id: RecordId,
    preparation_id: RecordId,
    session: Session,
    identity: CurrentIdentity,
    request: Request,
):
    await find(session, identity, cycle_id)
    return await workspace(
        cycle_id, preparation_id, session, identity, _pipeline_read_service_after_scope(request)
    )


@router.post(
    "/{cycle_id}/executions/{preparation_id}/dispatch",
    response_model=CycleDispatchWorkspace,
    operation_id="dispatchCycleExecution",
)
async def dispatch(
    cycle_id: RecordId,
    preparation_id: RecordId,
    body: DispatchCycleExecution,
    session: Session,
    identity: Operator,
    request: Request,
):
    cycle = await find(session, identity, cycle_id)
    pipeline = _pipeline_read_service_after_scope(request)
    current = await workspace(cycle_id, preparation_id, session, identity, pipeline)
    if current.attempt:
        if current.attempt.request_key != body.idempotency_key:
            raise HTTPException(
                409,
                "This preparation already has a launch intent; refresh and reconcile "
                "that same request",
            )
        if current.attempt.status not in {"reserved", "uncertain"}:
            return current
        if not current.can_resume:
            raise HTTPException(
                409, "Launch prerequisites changed; inspect current dispatch checks before resuming"
            )
        row = await session.scalar(
            select(CycleExecutionDispatch).where(CycleExecutionDispatch.id == current.attempt.id)
        )
        assert row is not None
    else:
        if not current.can_dispatch:
            raise HTTPException(409, "Execution is blocked; inspect current dispatch checks")
        cycle, snapshot, artifact, target, _ = await dispatch_context(
            session, identity, cycle_id, preparation_id
        )
        if target is None or artifact is None:
            raise HTTPException(
                409, "Approved environment target or source artifact is unavailable"
            )
        identifier = uuid4().hex
        config = {
            "cycle_dispatch_id": identifier,
            "project_id": cycle.project_id,
            "app_id": cycle.app_id,
            "environment_id": cycle.environment_id,
            "environment_target": target.base_url,
            "environment_target_version": target.version,
            "engine": "apex_load",
            "connections": {
                "execution_engine": snapshot.connection_id,
                "artifact_store": artifact.connection_id,
            },
            "phases": ["execution"],
            "gates": {"execution": {"prompt_review": "auto", "output_review": "auto"}},
        }
        await reserve_revision(session, cycle, body.expected_revision)
        row = CycleExecutionDispatch(
            id=identifier,
            cycle_id=cycle_id,
            preparation_id=preparation_id,
            request_key=body.idempotency_key,
            active_environment_id=cycle.environment_id,
            status="reserved",
            pipeline_config=config,
            created_by=identity.consumer_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                409,
                "Another launch reserved this preparation, request key or "
                "environment; refresh before continuing",
            ) from None
        await session.refresh(row)
    # Copy immutable fields before releasing the request transaction for runtime IO.
    dispatch_id, author = row.id, row.created_by
    reuse_existing_thread = row.thread_id is not None
    config = dict(row.pipeline_config)
    project_id, app_id = config["project_id"], config["app_id"]
    await session.rollback()

    async def bind_thread(thread_id: str):
        result = await session.scalar(
            update(CycleExecutionDispatch)
            .where(
                CycleExecutionDispatch.id == dispatch_id,
                CycleExecutionDispatch.status.in_(["reserved", "uncertain"]),
            )
            .values(thread_id=thread_id, updated_at=datetime.now(UTC))
            .returning(CycleExecutionDispatch.id)
            .execution_options(synchronize_session=False)
        )
        if result is None:
            raise ValueError("Cycle launch intent changed before pipeline creation")
        await session.commit()
        return None

    try:
        async with asyncio.timeout(45):
            launched = await pipeline.start_run(
                title=f"Cycle execution {preparation_id}",
                request=(
                    "Execute the exact approved cycle preparation without generating "
                    "or substituting a workload."
                ),
                idempotency_key=f"cycle-dispatch:{dispatch_id}",
                principal_id=author,
                project_id=project_id,
                app_id=app_id,
                configurable=config,
                phases=["execution"],
                gates=config["gates"],
                before_run_start=bind_thread,
                reuse_existing_thread=reuse_existing_thread,
            )
        await session.execute(
            update(CycleExecutionDispatch)
            .where(
                CycleExecutionDispatch.id == dispatch_id,
                CycleExecutionDispatch.status.in_(["reserved", "uncertain"]),
            )
            .values(
                status="submitted",
                thread_id=launched["thread_id"],
                run_id=launched["run_id"],
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
    except Exception:
        await session.rollback()
        await session.execute(
            update(CycleExecutionDispatch)
            .where(
                CycleExecutionDispatch.id == dispatch_id,
                CycleExecutionDispatch.status.in_(["reserved", "uncertain"]),
            )
            .values(status="uncertain", updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    session.expire_all()
    return await workspace(cycle_id, preparation_id, session, identity, pipeline)


@router.post(
    "/{cycle_id}/executions/{preparation_id}/dispatch/cancel",
    response_model=CycleDispatchWorkspace,
    operation_id="cancelCycleExecutionDispatch",
)
async def cancel_dispatch(
    cycle_id: RecordId,
    preparation_id: RecordId,
    body: CancelCycleDispatch,
    session: Session,
    identity: Operator,
    request: Request,
):
    await find(session, identity, cycle_id)
    # The thread-binding callback commits before runs.create. Winning this CAS
    # makes that callback fail, proving no native run can start for this intent.
    cancelled = await session.scalar(
        update(CycleExecutionDispatch)
        .where(
            CycleExecutionDispatch.id == body.dispatch_id,
            CycleExecutionDispatch.cycle_id == cycle_id,
            CycleExecutionDispatch.preparation_id == preparation_id,
            CycleExecutionDispatch.status.in_(["reserved", "uncertain"]),
            CycleExecutionDispatch.thread_id.is_(None),
        )
        .values(
            status="aborted",
            active_environment_id=None,
            canceled_by=identity.consumer_id,
            updated_at=datetime.now(UTC),
        )
        .returning(CycleExecutionDispatch.id)
        .execution_options(synchronize_session=False)
    )
    if cancelled is None:
        await session.rollback()
        raise HTTPException(
            409,
            "Launch already bound to a pipeline or changed; refresh and use the linked "
            "run to stop it",
        )
    await session.commit()
    session.expire_all()
    return await workspace(
        cycle_id, preparation_id, session, identity, _pipeline_read_service_after_scope(request)
    )
