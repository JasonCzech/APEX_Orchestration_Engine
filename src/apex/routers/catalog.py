"""Application/environment catalog CRUD (`/catalog`) — legacy app_env surface.

Layout decision: environments are exposed FLAT at /catalog/environments
(application_id is a body field on create and a query filter on list) rather
than nested under /applications/{id}/ — flat ids keep dashboard deep links
stable and avoid double-lookup on every environment route.

Scoping: applications carry project_id; environments inherit it through their
application. Scoped consumers see only their projects' rows; cross-project
single-resource access returns 404 (not 403) to avoid leaking existence.
Roles: GET = any authenticated; create/update/archive = operator+;
DELETE /applications/{id} = admin (environment delete is operator+).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.persistence.db import get_session
from apex.persistence.models import Environment, EnvironmentSnapshot
from apex.persistence.repositories.catalog import CatalogRepository, DuplicateNameError
from apex.services.connections import (
    TRUSTED_PRIVATE_HOST_OPTION,
    validate_adapter_base_url,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])

OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]


def get_catalog_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CatalogRepository:
    return CatalogRepository(session)


CatalogRepo = Annotated[CatalogRepository, Depends(get_catalog_repository)]


# ── schemas ──────────────────────────────────────────────────────────────────


class ApplicationCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ApplicationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    description: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HostIn(BaseModel):
    hostname: str = Field(min_length=1, max_length=1024)
    role: str | None = None


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    hostname: str
    role: str | None


class SnapshotSummary(BaseModel):
    """Latest cluster-inventory scan, summarized (full data stays server-side)."""

    scanned_at: datetime
    service_count: int


class EnvironmentCreate(BaseModel):
    application_id: str
    name: str = Field(min_length=1, max_length=255)
    kind: str | None = None
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    hosts: list[HostIn] = Field(default_factory=list)


class EnvironmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: str | None = None
    base_url: str | None = None
    options: dict[str, Any] | None = None
    hosts: list[HostIn] | None = None  # when present, REPLACES the full host list


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    application_id: str
    name: str
    kind: str | None
    base_url: str | None
    options: dict[str, Any]
    target_approved: bool
    target_version: int
    hosts: list[HostOut]
    created_at: datetime
    updated_at: datetime
    # Populated on getEnvironment only (list omits it to avoid N+1 scans).
    last_snapshot: SnapshotSummary | None = None


# ── helpers ──────────────────────────────────────────────────────────────────


def _visible_projects(identity: ConsumerIdentity) -> tuple[str, ...] | None:
    """None = unrestricted; otherwise the allow-list of scoped project ids."""
    return None if identity.is_unscoped else identity.scoped_project_ids()


def _not_found(what: str, resource_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} {resource_id!r} not found")


def _environment_out(
    env: Environment, snapshot: EnvironmentSnapshot | None = None
) -> EnvironmentOut:
    out = EnvironmentOut.model_validate(env)
    if snapshot is not None:
        services = (snapshot.data or {}).get("services", [])
        count = len(services) if isinstance(services, list) else 0
        out.last_snapshot = SnapshotSummary(scanned_at=snapshot.scanned_at, service_count=count)
    return out


def _can_access_application(identity: ConsumerIdentity, app: Any) -> bool:
    return identity.allows_scope(project_id=app.project_id, app_id=app.id)


def _can_access_environment(identity: ConsumerIdentity, env: Environment) -> bool:
    return identity.allows_scope(project_id=env.application.project_id, app_id=env.application_id)


def _is_platform_admin(identity: ConsumerIdentity) -> bool:
    return identity.role is Role.ADMIN and identity.is_unscoped


def _validate_environment_options(identity: ConsumerIdentity, options: dict[str, Any]) -> None:
    if not _is_platform_admin(identity) and any(str(key).startswith("_apex_") for key in options):
        raise HTTPException(
            status_code=403,
            detail="Reserved _apex_ environment options require an unscoped platform admin",
        )


def _approve_environment_target(
    identity: ConsumerIdentity,
    base_url: str | None,
    options: dict[str, Any],
) -> bool:
    if not _is_platform_admin(identity):
        raise HTTPException(
            status_code=403,
            detail="Execution targets require approval by an unscoped platform admin",
        )
    if base_url is None or not base_url.strip():
        return False
    try:
        validate_adapter_base_url(
            base_url,
            allow_private_hosts=options.get(TRUSTED_PRIVATE_HOST_OPTION) is True or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return True


# ── applications ─────────────────────────────────────────────────────────────


@router.get("/applications", operation_id="listApplications")
async def list_applications(
    identity: CurrentIdentity,
    repo: CatalogRepo,
    project: str | None = None,
    include_archived: bool = False,
) -> list[ApplicationOut]:
    ensure_scope(identity, project_id=project)
    apps = await repo.list_applications(
        project=project,
        visible_projects=_visible_projects(identity),
        include_archived=include_archived,
    )
    return [
        ApplicationOut.model_validate(app) for app in apps if _can_access_application(identity, app)
    ]


@router.post("/applications", operation_id="createApplication", status_code=201)
async def create_application(
    body: ApplicationCreate, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    if not identity.contains_scope(ScopeRef(project_id=body.project_id)):
        raise HTTPException(
            status_code=403,
            detail="application creation requires project-wide scope",
        )
    try:
        app = await repo.create_application(
            project_id=body.project_id, name=body.name, description=body.description
        )
    except DuplicateNameError:
        raise HTTPException(
            status_code=409,
            detail=f"Application {body.name!r} already exists in project {body.project_id!r}",
        ) from None
    return ApplicationOut.model_validate(app)


@router.get("/applications/{application_id}", operation_id="getApplication")
async def get_application(
    application_id: str, identity: CurrentIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    return ApplicationOut.model_validate(app)


@router.patch("/applications/{application_id}", operation_id="updateApplication")
async def update_application(
    application_id: str, body: ApplicationUpdate, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    project_id = app.project_id  # capture before mutation: rollback expires the instance
    try:
        app = await repo.update_application(app, body.model_dump(exclude_unset=True))
    except DuplicateNameError:
        raise HTTPException(
            status_code=409,
            detail=f"Application {body.name!r} already exists in project {project_id!r}",
        ) from None
    return ApplicationOut.model_validate(app)


@router.post("/applications/{application_id}/archive", operation_id="archiveApplication")
async def archive_application(
    application_id: str, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    return ApplicationOut.model_validate(await repo.set_application_archived(app, True))


@router.post("/applications/{application_id}/unarchive", operation_id="unarchiveApplication")
async def unarchive_application(
    application_id: str, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    return ApplicationOut.model_validate(await repo.set_application_archived(app, False))


@router.delete("/applications/{application_id}", operation_id="deleteApplication", status_code=204)
async def delete_application(
    application_id: str, identity: AdminIdentity, repo: CatalogRepo
) -> None:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    await repo.delete_application(app)


# ── environments ─────────────────────────────────────────────────────────────


@router.get("/environments", operation_id="listEnvironments")
async def list_environments(
    identity: CurrentIdentity,
    repo: CatalogRepo,
    application: str | None = None,
) -> list[EnvironmentOut]:
    envs = await repo.list_environments(
        application_id=application, visible_projects=_visible_projects(identity)
    )
    return [_environment_out(env) for env in envs if _can_access_environment(identity, env)]


@router.post("/environments", operation_id="createEnvironment", status_code=201)
async def create_environment(
    body: EnvironmentCreate, identity: OperatorIdentity, repo: CatalogRepo
) -> EnvironmentOut:
    app = await repo.get_application(body.application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", body.application_id)
    _validate_environment_options(identity, body.options)
    target_approved = False
    target_version = 0
    if body.base_url is not None:
        target_approved = _approve_environment_target(identity, body.base_url, body.options)
        target_version = 1
    try:
        env = await repo.create_environment(
            application_id=body.application_id,
            name=body.name,
            kind=body.kind,
            base_url=body.base_url,
            target_approved=target_approved,
            target_version=target_version,
            options=body.options,
            hosts=[host.model_dump() for host in body.hosts],
        )
    except DuplicateNameError:
        raise HTTPException(
            status_code=409,
            detail=f"Environment {body.name!r} already exists on application "
            f"{body.application_id!r}",
        ) from None
    return _environment_out(env)


@router.get("/environments/{environment_id}", operation_id="getEnvironment")
async def get_environment(
    environment_id: str, identity: CurrentIdentity, repo: CatalogRepo
) -> EnvironmentOut:
    env = await repo.get_environment(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    snapshot = await repo.latest_snapshot(env.id)
    return _environment_out(env, snapshot)


@router.patch("/environments/{environment_id}", operation_id="updateEnvironment")
async def update_environment(
    environment_id: str, body: EnvironmentUpdate, identity: OperatorIdentity, repo: CatalogRepo
) -> EnvironmentOut:
    env = await repo.get_environment(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    application_id = env.application_id  # capture before mutation: rollback expires the instance
    changes = body.model_dump(exclude_unset=True)
    hosts = changes.pop("hosts", None)
    next_options = changes.get("options", env.options) or {}
    _validate_environment_options(identity, next_options)
    target_fields_changed = "base_url" in changes or (
        "options" in changes
        and next_options.get(TRUSTED_PRIVATE_HOST_OPTION)
        != (env.options or {}).get(TRUSTED_PRIVATE_HOST_OPTION)
    )
    if target_fields_changed:
        next_base_url = changes.get("base_url", env.base_url)
        changes["target_approved"] = _approve_environment_target(
            identity, next_base_url, next_options
        )
        changes["target_version"] = int(env.target_version) + 1
    try:
        env = await repo.update_environment(env, changes, hosts=hosts)
    except DuplicateNameError:
        raise HTTPException(
            status_code=409,
            detail=f"Environment {body.name!r} already exists on application {application_id!r}",
        ) from None
    return _environment_out(env)


@router.delete("/environments/{environment_id}", operation_id="deleteEnvironment", status_code=204)
async def delete_environment(
    environment_id: str, identity: OperatorIdentity, repo: CatalogRepo
) -> None:
    env = await repo.get_environment(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    await repo.delete_environment(env)
