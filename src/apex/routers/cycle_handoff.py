"""Cycle closure and reuse through the shared authenticated domain API."""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update

from apex.domain.cycle_handoff import (
    FollowupWrite,
    NextCycleRequest,
    reuse_actions,
    reuse_drafts,
    write_followup,
)
from apex.domain.cycles import cycle_progress, version_digest
from apex.domain.input_limits import RecordId
from apex.persistence.models import Application, Environment, TestCycle
from apex.routers.cycles import CycleRead, Operator, Session, enabled, find, view
from apex.settings import get_settings

router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])


async def reserve(session: Session, cycle: TestCycle, revision: int, **values):
    changed = await session.scalar(
        update(TestCycle)
        .where(TestCycle.id == cycle.id, TestCycle.revision == revision)
        .values(revision=revision + 1, **values)
        .returning(TestCycle.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        await session.rollback()
        raise HTTPException(409, "Cycle changed; refresh before continuing")


@router.post("/{cycle_id}/followups", response_model=CycleRead, operation_id="writeCycleFollowup")
async def write_action(
    cycle_id: RecordId, body: FollowupWrite, session: Session, identity: Operator
):
    cycle = await find(session, identity, cycle_id)
    try:
        actions, replay = write_followup(cycle.followups or [], body, identity.consumer_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if replay:
        return view(cycle, identity)
    if len(json.dumps(actions).encode()) > 1_000_000:
        raise HTTPException(413, "Action history size limit reached")
    await reserve(session, cycle, body.expected_revision, followups=actions)
    await session.commit()
    await session.refresh(cycle)
    return view(cycle, identity)


@router.post("/{cycle_id}/next-cycle", response_model=CycleRead, operation_id="startNextCycle")
async def next_cycle(
    cycle_id: RecordId, body: NextCycleRequest, session: Session, identity: Operator
):
    parent = await find(session, identity, cycle_id)
    fingerprint = version_digest(body.model_dump(exclude={"expected_revision", "idempotency_key"}))
    existing = await session.scalar(
        select(TestCycle.id).where(
            TestCycle.origin_cycle_id == parent.id,
            TestCycle.origin_request_key == body.idempotency_key,
        )
    )
    if existing:
        child = await find(session, identity, existing)
        if child.origin_fingerprint != fingerprint:
            raise HTTPException(409, "Operation key already used for different next-cycle inputs")
        return view(child, identity)
    target = await session.scalar(
        select(Environment.id)
        .join(Application, Environment.application_id == Application.id)
        .where(
            Environment.id == parent.environment_id,
            Application.id == parent.app_id,
            Application.project_id == parent.project_id,
            Application.archived_at.is_(None),
        )
    )
    if target is None:
        raise HTTPException(
            422,
            "Original target unavailable; create a cycle with an active application/environment",
        )
    try:
        versions = reuse_drafts(parent.versions, body.version_ids, identity.consumer_id, parent.id)
        actions = (
            reuse_actions(parent.followups or [], parent.id, identity.consumer_id)
            if body.carry_unresolved_actions
            else []
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if (
        len(json.dumps(versions).encode()) > 2_000_000
        or len(json.dumps(actions).encode()) > 1_000_000
    ):
        raise HTTPException(413, "Carried content exceeds cycle limits")
    child = TestCycle(
        name=body.name,
        project_id=parent.project_id,
        app_id=parent.app_id,
        environment_id=parent.environment_id,
        mode=parent.mode,
        owner=identity.consumer_id,
        independent_review=parent.independent_review or get_settings().cycles_independent_review,
        revision=1,
        versions=versions,
        progress=cycle_progress(versions).model_dump(),
        followups=actions,
        origin_cycle_id=parent.id,
        origin_request_key=body.idempotency_key,
        origin_fingerprint=fingerprint,
    )
    await reserve(session, parent, body.expected_revision)
    session.add(child)
    await session.commit()
    await session.refresh(child)
    return view(child, identity)
