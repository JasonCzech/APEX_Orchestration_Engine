"""Opt-in cycle/version API. No implicit execution or external publication."""

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, false, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.domain.cycles import (
    CycleAction,
    CycleCreate,
    CycleProgress,
    VersionCreate,
    add_version,
    approval_current,
    content_integrity,
    cycle_blockers,
    cycle_progress,
    transition,
)
from apex.domain.deliverables import DELIVERABLE_FIELDS, DeliverableField, content_issues
from apex.domain.input_limits import RecordId
from apex.persistence.db import get_session
from apex.persistence.models import (
    ApiConsumer,
    Application,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    CycleScriptValidation,
    Environment,
    ProjectApplication,
    TestCycle,
)
from apex.services.connection_credentials import sanitize_credential_text_for_output
from apex.settings import get_settings


def enabled(identity: CurrentIdentity) -> None:
    if not get_settings().cycles_enabled:
        raise HTTPException(404, "Cycle workspace is not enabled")


router = APIRouter(prefix="/cycles", tags=["cycles"], dependencies=[Depends(enabled)])
Session = Annotated[AsyncSession, Depends(get_session)]
Operator = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]


class CycleSummary(BaseModel):
    id: str
    name: str
    project_id: str
    app_id: str
    environment_id: str
    mode: str
    owner: str
    revision: int
    independent_review: bool
    created_at: datetime
    updated_at: datetime
    owner_name: str | None = None
    progress: CycleProgress | None = None


class CycleActors(BaseModel):
    names: dict[str, str]
    truncated: bool = False


class CycleVersionRead(BaseModel):
    origin: dict[str, str] | None = None
    id: str
    kind: str
    title: str
    content: dict[str, Any]
    checksum: str
    dependencies: list[str]
    replaces: str | None = None
    change_summary: str
    status: str
    created_by: str
    created_at: str
    archived: bool
    reviews: list[dict[str, str]]
    approval_current: bool
    validation_issues: list[str]
    available_actions: list[str]


class CycleRead(CycleSummary):
    followups: list[dict[str, Any]] = []
    origin_cycle_id: str | None = None
    versions: list[CycleVersionRead]
    fields: dict[str, list[DeliverableField]] = DELIVERABLE_FIELDS
    blockers: list[str]


def visibility(identity: ConsumerIdentity):
    if identity.is_unscoped:
        return TestCycle.id.is_not(None)
    return or_(
        false(),
        *[
            and_(TestCycle.project_id == scope.project_id, TestCycle.app_id == scope.app_id)
            if scope.app_id
            else TestCycle.project_id == scope.project_id
            for scope in identity.scopes
        ],
    )


def version_view(
    row: TestCycle, version: dict[str, Any], actor: ConsumerIdentity | None
) -> CycleVersionRead:
    issues = content_issues(version["kind"], version["content"])
    if not content_integrity(version):
        issues.append("Version content does not match its recorded checksum")
    actions: list[str] = []
    active = not any(v.get("replaces") == version["id"] for v in row.versions)
    if actor is not None and actor.role.at_least(Role.OPERATOR):
        actions.append("restore" if version["archived"] else "archive")
        if active and not version["archived"]:
            if version["status"] == "draft" and not issues:
                actions.append("submit")
            if version["status"] == "in_review":
                actions.append("request_revision")
                if (
                    not issues
                    and (not row.independent_review or actor.consumer_id != version["created_by"])
                    and all(approval_current(row.versions, dep) for dep in version["dependencies"])
                ):
                    actions.append("approve")
    return CycleVersionRead(
        **version,
        approval_current=approval_current(row.versions, version["id"]),
        validation_issues=issues,
        available_actions=actions,
    )


def view(row: TestCycle, actor: ConsumerIdentity | None = None) -> CycleRead:
    return CycleRead(
        **{
            name: getattr(row, name)
            for name in CycleRead.model_fields
            if name
            not in {
                "blockers",
                "versions",
                "fields",
                "followups",
                "origin_cycle_id",
                "owner_name",
                "progress",
            }
        },
        owner_name=actor.name if actor and row.owner == actor.consumer_id else None,
        progress=cycle_progress(row.versions),
        followups=getattr(row, "followups", None) or [],
        origin_cycle_id=getattr(row, "origin_cycle_id", None),
        versions=[version_view(row, v, actor) for v in row.versions],
        blockers=cycle_blockers(row.versions),
    )


async def find(session: AsyncSession, identity: ConsumerIdentity, cycle_id: str) -> TestCycle:
    row = await session.scalar(
        select(TestCycle).where(TestCycle.id == cycle_id, visibility(identity))
    )
    if row is None:
        raise HTTPException(404, "Cycle not found")
    return row


@router.get("", response_model=list[CycleSummary], operation_id="listCycles")
async def list_cycles(
    session: Session,
    identity: CurrentIdentity,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
):
    rows = await session.execute(
        select(
            *(
                getattr(TestCycle, name)
                for name in CycleSummary.model_fields
                if name != "owner_name"
            ),
            ApiConsumer.name.label("owner_name"),
        )
        .outerjoin(
            ApiConsumer, and_(ApiConsumer.id == TestCycle.owner, ApiConsumer.deleted_at.is_(None))
        )
        .where(visibility(identity))
        .order_by(TestCycle.created_at.desc(), TestCycle.id)
        .limit(limit)
        .offset(offset)
    )
    page = [dict(row) for row in rows.mappings()]
    await fill_missing_progress(session, identity, page)
    return [
        CycleSummary.model_validate(
            {**row, "owner_name": sanitize_credential_text_for_output(row["owner_name"])}
        )
        for row in page
    ]


async def fill_missing_progress(
    session: AsyncSession, identity: ConsumerIdentity, page: list[dict[str, Any]]
) -> None:
    """Fill legacy rows in batches; ordinary lists never load version history.

    The revision must match both the response row and database CAS. An
    intervening edit never attaches new progress to old list metadata.
    """
    missing = []
    for row in page:
        try:
            row["progress"] = CycleProgress.model_validate(row["progress"]).model_dump()
        except (ValueError, TypeError):
            row["progress"] = None
            missing.append(row)
    changed = False
    for offset in range(0, len(missing), 10):
        batch = {row["id"]: row for row in missing[offset : offset + 10]}
        histories = await session.execute(
            select(TestCycle.id, TestCycle.revision, TestCycle.versions).where(
                TestCycle.id.in_(batch), visibility(identity)
            )
        )
        for history in histories:
            target = batch[history.id]
            if target["revision"] != history.revision:
                continue
            try:
                if not isinstance(history.versions, list) or len(history.versions) > 200:
                    continue
                progress = cycle_progress(history.versions).model_dump()
            except (KeyError, TypeError, ValueError, RecursionError):
                continue
            saved = await session.scalar(
                update(TestCycle)
                .where(TestCycle.id == history.id, TestCycle.revision == history.revision)
                .values(progress=progress, updated_at=TestCycle.updated_at)
                .returning(TestCycle.id)
            )
            if saved is not None:
                target["progress"] = progress
                changed = True
    if changed:
        await session.commit()


@router.post("", response_model=CycleRead, status_code=201, operation_id="createCycle")
async def create_cycle(body: CycleCreate, session: Session, identity: Operator):
    ensure_scope(identity, project_id=body.project_id, app_id=body.app_id)
    target = await session.scalar(
        select(Environment.id)
        .join(Application, Environment.application_id == Application.id)
        .where(
            Environment.id == body.environment_id,
            Application.id == body.app_id,
            select(ProjectApplication.application_id)
            .where(
                ProjectApplication.application_id == Application.id,
                ProjectApplication.project_id == body.project_id,
            )
            .exists(),
            Application.archived_at.is_(None),
        )
    )
    if target is None:
        raise HTTPException(422, "Select an active application and its environment in this project")
    row = TestCycle(
        **body.model_dump(),
        owner=identity.consumer_id,
        revision=1,
        independent_review=get_settings().cycles_independent_review,
        versions=[],
        progress=cycle_progress([]).model_dump(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return view(row, identity)


@router.get("/{cycle_id}", response_model=CycleRead, operation_id="getCycle")
async def read_cycle(cycle_id: RecordId, session: Session, identity: CurrentIdentity):
    return view(await find(session, identity, cycle_id), identity)


@router.get("/{cycle_id}/actors", response_model=CycleActors, operation_id="getCycleActors")
async def read_cycle_actors(cycle_id: RecordId, session: Session, identity: CurrentIdentity):
    cycle = await find(session, identity, cycle_id)
    # Scope is established before querying names. Only identities already on
    # this permitted cycle are exposed; never enumerate the consumer directory.
    ids = dict.fromkeys([cycle.owner])

    def add_actor(value: Any) -> None:
        if isinstance(value, str) and value:
            ids.setdefault(value)

    for version in reversed(cycle.versions):
        add_actor(version.get("created_by"))
        for review in reversed(version.get("reviews", [])):
            add_actor(review.get("actor"))
    ids.pop("", None)
    for model in (CycleExecutionPreparation, CycleExecutionDispatch, CycleScriptValidation):
        # Only bounded identity columns are read, never execution credentials.
        for actor_id in await session.scalars(
            select(model.created_by).where(model.cycle_id == cycle.id).distinct().limit(257)
        ):
            add_actor(actor_id)
    for model, column in (
        (CycleExecutionPreparation, CycleExecutionPreparation.archived_by),
        (CycleExecutionDispatch, CycleExecutionDispatch.canceled_by),
    ):
        for actor_id in await session.scalars(
            select(column)
            .where(model.cycle_id == cycle.id, column.is_not(None))
            .distinct()
            .limit(257)
        ):
            add_actor(actor_id)
    selected = list(ids)[:256]
    rows = await session.execute(
        select(ApiConsumer.id, ApiConsumer.name).where(
            ApiConsumer.id.in_(selected), ApiConsumer.deleted_at.is_(None)
        )
    )
    names = {
        row.id: sanitize_credential_text_for_output(row.name) or "Name unavailable" for row in rows
    }
    if identity.consumer_id in selected:
        names.setdefault(identity.consumer_id, identity.name)
    return CycleActors(names=names, truncated=len(ids) > len(selected))


async def save_versions(
    session: AsyncSession,
    row: TestCycle,
    revision: int,
    versions: list[dict[str, Any]],
    actor: ConsumerIdentity | None = None,
) -> CycleRead:
    # SQL CAS covers edits and approvals across workers, not just an in-process lock.
    if len(json.dumps(versions).encode()) > 2_000_000:
        raise HTTPException(413, "Cycle content limit reached; store large evidence as artifacts")
    result = await session.execute(
        update(TestCycle)
        .where(TestCycle.id == row.id, TestCycle.revision == revision)
        .values(
            versions=versions, progress=cycle_progress(versions).model_dump(), revision=revision + 1
        )
        .returning(TestCycle.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise HTTPException(409, "Cycle changed; refresh and review the latest version")
    await session.commit()
    await session.refresh(row)
    return view(row, actor)


@router.post(
    "/{cycle_id}/versions",
    response_model=CycleRead,
    status_code=201,
    operation_id="createCycleVersion",
)
async def create_version(
    cycle_id: RecordId, body: VersionCreate, session: Session, identity: Operator
):
    row = await find(session, identity, cycle_id)
    if row.revision != body.expected_revision:
        raise HTTPException(409, "Cycle changed; refresh before editing")
    if len(row.versions) >= 200:
        raise HTTPException(409, "Cycle version limit reached; create the next cycle")
    try:
        version = add_version(row.versions, body, identity.consumer_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return await save_versions(
        session, row, body.expected_revision, [*row.versions, version], identity
    )


@router.post("/{cycle_id}/actions", response_model=CycleRead, operation_id="reviewCycleVersion")
async def act(cycle_id: RecordId, body: CycleAction, session: Session, identity: Operator):
    row = await find(session, identity, cycle_id)
    if row.revision != body.expected_revision:
        raise HTTPException(409, "Cycle changed; refresh before reviewing")
    try:
        versions = transition(
            row.versions, body, identity.consumer_id, independent_review=row.independent_review
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return await save_versions(session, row, body.expected_revision, versions, identity)
