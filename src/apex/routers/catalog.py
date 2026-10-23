"""Application/environment catalog CRUD (`/catalog`) — legacy app_env surface.

Layout decision: environments are exposed FLAT at /catalog/environments
(application_id is a body field on create and a query filter on list) rather
than nested under /applications/{id}/ — flat ids keep dashboard deep links
stable and avoid double-lookup on every environment route.

Scoping: an application links to one or more projects (``project_ids``) and
its environments belong to it. A scoped consumer sees an application, and its
environments, through any linked project it may access; cross-project
single-resource access returns 404 (not 403) to avoid leaking existence.
Application names are unique across the catalog.
Roles: GET = any authenticated; create/update/archive = operator+;
DELETE /applications/{id} = admin (environment delete is operator+).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.input_limits import (
    MAX_CHILD_ITEMS,
    MAX_DB_LIST_OFFSET,
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    RecordId,
    ScopeId,
    validate_json_object,
)
from apex.persistence.db import get_session
from apex.persistence.models import Environment, EnvironmentSnapshot
from apex.persistence.repositories.catalog import (
    CatalogInUseError,
    CatalogRepository,
    ConcurrentLinkError,
    DuplicateNameError,
)
from apex.services.connection_credentials import (
    connection_options_require_repair,
    reject_credential_text,
    reject_raw_secret_options,
    sanitize_connection_options_for_output,
    sanitize_connection_url_for_output,
    sanitize_credential_text_for_output,
)
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
ApplicationId = Annotated[RecordId, Path(description="Application id")]
EnvironmentId = Annotated[RecordId, Path(description="Environment id")]


# ── schemas ──────────────────────────────────────────────────────────────────


ProjectIds = Annotated[list[ScopeId], Field(min_length=1, max_length=64)]


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: ProjectIds
    name: NoNulStr = Field(min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("*")
    @classmethod
    def reject_raw_credential_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            reject_credential_text(value, label="application text field")
        return value


class ProjectLabelWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    project_id: ScopeId
    name: NoNulStr = Field(min_length=1, max_length=255)

    @field_validator("*")
    @classmethod
    def safe_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Project fields must not be blank")
        reject_credential_text(value, label="project metadata")
        return value


class ProjectLabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project_id: str
    name: str
    updated_at: datetime

    @field_validator("name", mode="before")
    @classmethod
    def safe_name(cls, value: Any) -> str:
        return sanitize_credential_text_for_output(value) or "Name unavailable"


class ApplicationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    # Replaces the set of linked projects (link and unlink in one write).
    project_ids: ProjectIds | None = None

    @field_validator("name", "project_ids")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("value cannot be null")
        return value

    @field_validator("*")
    @classmethod
    def reject_raw_credential_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            reject_credential_text(value, label="application text field")
        return value


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

    id: str
    project_ids: list[str]
    name: str
    description: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("name", "description", mode="before")
    @classmethod
    def sanitize_legacy_labels(cls, value: Any) -> str | None:
        return sanitize_credential_text_for_output(value)

    @field_validator("project_ids", mode="before")
    @classmethod
    def sanitize_project_ids(cls, value: Any) -> list[str]:
        return [sanitize_credential_text_for_output(item) or "" for item in value]


class HostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: NoNulStr = Field(min_length=1, max_length=1024)
    role: NoNulStr | None = Field(default=None, max_length=255)

    @field_validator("*")
    @classmethod
    def reject_raw_credential_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            reject_credential_text(value, label="environment host text field")
        return value


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

    id: str
    hostname: str
    role: str | None

    @field_validator("hostname", "role", mode="before")
    @classmethod
    def sanitize_legacy_labels(cls, value: Any) -> str | None:
        return sanitize_credential_text_for_output(value)


class SnapshotSummary(BaseModel):
    """Latest cluster-inventory scan, summarized (full data stays server-side)."""

    scanned_at: datetime
    service_count: int


class EnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: NoNulStr = Field(min_length=1, max_length=32)
    name: NoNulStr = Field(min_length=1, max_length=255)
    kind: NoNulStr | None = Field(default=None, max_length=64)
    base_url: NoNulStr | None = Field(default=None, min_length=1, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)
    hosts: list[HostIn] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)

    @field_validator("*")
    @classmethod
    def reject_raw_credential_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            reject_credential_text(value, label="environment text field")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, options: dict[str, Any]) -> dict[str, Any]:
        return validate_json_object(options, label="environment options")


class EnvironmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    kind: NoNulStr | None = Field(default=None, max_length=64)
    base_url: NoNulStr | None = Field(default=None, min_length=1, max_length=1024)
    options: dict[str, Any] | None = None
    hosts: list[HostIn] | None = Field(
        default=None, max_length=MAX_CHILD_ITEMS
    )  # when present, REPLACES the full host list

    @field_validator("name")
    @classmethod
    def reject_null_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null")
        return value

    @field_validator("*")
    @classmethod
    def reject_raw_credential_scalars(cls, value: Any) -> Any:
        if type(value) is str:
            reject_credential_text(value, label="environment text field")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, options: dict[str, Any] | None) -> dict[str, Any] | None:
        if options is None:
            # Explicit null clears the JSON object; omission is still excluded by
            # model_dump(exclude_unset=True).
            return {}
        return validate_json_object(options, label="environment options")


class EnvironmentTargetApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_target_version: int = Field(ge=0, strict=True)


class EnvironmentOut(BaseModel):
    # Environment rows predate the secret-free connection contract. Treat
    # persisted values as untrusted when projecting them: an old/direct-SQL row
    # must not reflect URI userinfo, signed query strings, or raw option
    # credentials to every viewer in the application's scope.
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)

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
    # Latest scan summary; the list loads all of them in one query (latest_snapshots).
    last_snapshot: SnapshotSummary | None = None

    @field_validator("application_id", "name", "kind", mode="before")
    @classmethod
    def sanitize_legacy_labels(cls, value: Any) -> str | None:
        return sanitize_credential_text_for_output(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def sanitize_legacy_base_url(cls, value: Any) -> str | None:
        return sanitize_connection_url_for_output(value)

    @field_validator("options", mode="before")
    @classmethod
    def sanitize_legacy_options(cls, value: Any) -> dict[str, Any]:
        return sanitize_connection_options_for_output(value)

    @model_validator(mode="after")
    def quarantine_repair_required_target(self) -> "EnvironmentOut":
        if self.base_url == "[REDACTED]" or self.options.get("_apex_repair_required") is True:
            # Keep this projection atomic. Revealing a safe sibling URL while
            # only the options are redacted can still make an operator/UI treat
            # a legacy target as approved and ready to execute.
            self.base_url = "[REDACTED]"
            self.options = {"_apex_repair_required": True}
            self.target_approved = False
        return self


# ── helpers ──────────────────────────────────────────────────────────────────


def _visible_projects(identity: ConsumerIdentity) -> tuple[str, ...] | None:
    """None = unrestricted; otherwise the allow-list of scoped project ids."""
    return None if identity.is_unscoped else identity.scoped_project_ids()


def _not_found(what: str, resource_id: str) -> HTTPException:
    del resource_id
    return HTTPException(status_code=404, detail=f"{what} not found")


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
    return any(
        identity.allows_scope(project_id=project_id, app_id=app.id)
        for project_id in app.project_ids
    )


def _can_access_environment(identity: ConsumerIdentity, env: Environment) -> bool:
    return _can_access_application(identity, env.application)


def _can_change_everywhere(identity: ConsumerIdentity, app: Any) -> bool:
    """Archive and delete affect every project that uses the application, so they
    need access to it through each linked project, not just one."""
    return all(
        identity.allows_scope(project_id=project_id, app_id=app.id)
        for project_id in app.project_ids
    )


def _application_out(identity: ConsumerIdentity, app: Any) -> ApplicationOut:
    """Scoped consumers see only the linked projects they may access."""
    out = ApplicationOut.model_validate(app)
    if not identity.is_unscoped:
        visible = set(identity.scoped_project_ids())
        out.project_ids = [project_id for project_id in out.project_ids if project_id in visible]
    return out


def _require_change_everywhere(identity: ConsumerIdentity, app: Any, action: str) -> None:
    if not _can_change_everywhere(identity, app):
        raise HTTPException(
            status_code=403,
            detail=f"{action} requires access through every project that uses it",
        )


def _require_project_wide(identity: ConsumerIdentity, project_ids: list[str], action: str) -> None:
    """Linking or unlinking a project changes what that project's tests can use."""
    if not all(identity.contains_scope(ScopeRef(project_id=pid)) for pid in project_ids):
        raise HTTPException(status_code=403, detail=f"{action} requires project-wide scope")


def _is_platform_admin(identity: ConsumerIdentity) -> bool:
    return identity.role is Role.ADMIN and identity.is_unscoped


def _validate_environment_options(identity: ConsumerIdentity, options: dict[str, Any]) -> None:
    validation_error: HTTPException | None = None
    try:
        reject_raw_secret_options(
            options,
            label="environment options",
            reference="a managed connection secret_ref",
        )
    except ValueError:
        validation_error = HTTPException(status_code=422, detail="invalid environment options")
    if validation_error is not None:
        raise validation_error
    if connection_options_require_repair(options):
        raise HTTPException(
            status_code=422,
            detail="environment options contain unsafe credential-bearing configuration",
        )
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
    validation_error: HTTPException | None = None
    try:
        validate_adapter_base_url(
            base_url,
            allow_private_hosts=options.get(TRUSTED_PRIVATE_HOST_OPTION) is True or None,
        )
    except ValueError:
        validation_error = HTTPException(status_code=422, detail="invalid environment target")
    if validation_error is not None:
        raise validation_error
    return True


# ── applications ─────────────────────────────────────────────────────────────


@router.get("/projects", operation_id="listProjectLabels")
async def list_project_labels(
    identity: CurrentIdentity,
    repo: CatalogRepo,
    project: Annotated[ScopeId | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[ProjectLabelOut]:
    ensure_scope(identity, project_id=project)
    return [
        ProjectLabelOut.model_validate(row)
        for row in await repo.list_project_labels(
            project=project,
            visible_projects=_visible_projects(identity),
            limit=limit,
            offset=offset,
        )
    ]


def _can_name_project(identity: ConsumerIdentity, project_id: str) -> None:
    if not identity.contains_scope(ScopeRef(project_id=project_id)):
        raise HTTPException(403, "Project naming requires project-wide administrator access")


@router.put("/projects", operation_id="setProjectLabel")
async def set_project_label(
    body: ProjectLabelWrite, identity: AdminIdentity, repo: CatalogRepo
) -> ProjectLabelOut:
    _can_name_project(identity, body.project_id)
    try:
        row = await repo.put_project_label(body.project_id, body.name)
    except DuplicateNameError:
        raise HTTPException(409, "Project metadata changed; refresh and retry") from None
    return ProjectLabelOut.model_validate(row)


@router.delete("/projects", operation_id="deleteProjectLabel", status_code=204)
async def delete_project_label(
    identity: AdminIdentity, repo: CatalogRepo, project: Annotated[ScopeId, Query()]
) -> None:
    _can_name_project(identity, project)
    await repo.delete_project_label(project)


@router.get("/applications", operation_id="listApplications")
async def list_applications(
    identity: CurrentIdentity,
    repo: CatalogRepo,
    project: Annotated[ScopeId | None, Query()] = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[ApplicationOut]:
    ensure_scope(identity, project_id=project)
    apps = await repo.list_applications(
        project=project,
        visible_projects=_visible_projects(identity),
        allowed_scopes=None if identity.is_unscoped else identity.scopes,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return [
        _application_out(identity, app) for app in apps if _can_access_application(identity, app)
    ]


@router.post("/applications", operation_id="createApplication", status_code=201)
async def create_application(
    body: ApplicationCreate, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    _require_project_wide(identity, body.project_ids, "application creation")
    write_error: HTTPException | None = None
    app = None
    try:
        app = await repo.create_application(
            project_ids=body.project_ids, name=body.name, description=body.description
        )
    except DuplicateNameError:
        write_error = HTTPException(status_code=409, detail="application name already exists")
    if write_error is not None:
        raise write_error
    assert app is not None
    return _application_out(identity, app)


@router.get("/applications/{application_id}", operation_id="getApplication")
async def get_application(
    application_id: ApplicationId, identity: CurrentIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    return _application_out(identity, app)


@router.patch("/applications/{application_id}", operation_id="updateApplication")
async def update_application(
    application_id: ApplicationId,
    body: ApplicationUpdate,
    identity: OperatorIdentity,
    repo: CatalogRepo,
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    changes = body.model_dump(exclude_unset=True)
    if "project_ids" in changes:
        if not identity.is_unscoped:
            # Links to projects the caller can't see are kept as they are.
            visible = set(identity.scoped_project_ids())
            hidden = [project_id for project_id in app.project_ids if project_id not in visible]
            changes["project_ids"] = sorted(set(changes["project_ids"]) | set(hidden))
        changed = set(changes["project_ids"]) ^ set(app.project_ids)
        _require_project_wide(identity, sorted(changed), "linking or unlinking projects")
    write_error: HTTPException | None = None
    try:
        app = await repo.update_application(app, changes)
    except DuplicateNameError:
        write_error = HTTPException(status_code=409, detail="application name already exists")
    except ConcurrentLinkError:
        write_error = HTTPException(
            status_code=409, detail="application projects changed concurrently; reload and retry"
        )
    if write_error is not None:
        raise write_error
    return _application_out(identity, app)


@router.post("/applications/{application_id}/archive", operation_id="archiveApplication")
async def archive_application(
    application_id: ApplicationId, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    _require_change_everywhere(identity, app, "archiving a shared application")
    return _application_out(identity, await repo.set_application_archived(app, True))


@router.post("/applications/{application_id}/unarchive", operation_id="unarchiveApplication")
async def unarchive_application(
    application_id: ApplicationId, identity: OperatorIdentity, repo: CatalogRepo
) -> ApplicationOut:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    _require_change_everywhere(identity, app, "unarchiving a shared application")
    return _application_out(identity, await repo.set_application_archived(app, False))


@router.delete("/applications/{application_id}", operation_id="deleteApplication", status_code=204)
async def delete_application(
    application_id: ApplicationId, identity: AdminIdentity, repo: CatalogRepo
) -> None:
    app = await repo.get_application(application_id)
    if app is None or not _can_access_application(identity, app):
        raise _not_found("application", application_id)
    _require_change_everywhere(identity, app, "deleting a shared application")
    try:
        await repo.delete_application(app)
    except CatalogInUseError:
        raise HTTPException(
            409, "Application is referenced by retained records; archive it instead"
        ) from None


# ── environments ─────────────────────────────────────────────────────────────


@router.get("/environments", operation_id="listEnvironments")
async def list_environments(
    identity: CurrentIdentity,
    repo: CatalogRepo,
    application: Annotated[RecordId | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[EnvironmentOut]:
    envs = await repo.list_environments(
        application_id=application,
        visible_projects=_visible_projects(identity),
        allowed_scopes=None if identity.is_unscoped else identity.scopes,
        limit=limit,
        offset=offset,
    )
    visible = [env for env in envs if _can_access_environment(identity, env)]
    # The list shows each environment's last scan, so fetch them in one query.
    snapshots = await repo.latest_snapshots([env.id for env in visible])
    return [_environment_out(env, snapshots.get(env.id)) for env in visible]


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
    write_error: HTTPException | None = None
    env = None
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
        write_error = HTTPException(
            status_code=409,
            detail="environment name already exists on application",
        )
    if write_error is not None:
        raise write_error
    assert env is not None
    return _environment_out(env)


@router.get("/environments/{environment_id}", operation_id="getEnvironment")
async def get_environment(
    environment_id: EnvironmentId, identity: CurrentIdentity, repo: CatalogRepo
) -> EnvironmentOut:
    env = await repo.get_environment(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    snapshot = await repo.latest_snapshot(env.id)
    return _environment_out(env, snapshot)


@router.patch("/environments/{environment_id}", operation_id="updateEnvironment")
async def update_environment(
    environment_id: EnvironmentId,
    body: EnvironmentUpdate,
    identity: OperatorIdentity,
    repo: CatalogRepo,
) -> EnvironmentOut:
    env = await repo.get_environment_for_update(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    changes = body.model_dump(exclude_unset=True)
    hosts = changes.pop("hosts", None)
    next_options = changes.get("options", env.options) or {}
    if "options" in changes:
        _validate_environment_options(identity, next_options)
        previous_reserved = {
            key: value for key, value in (env.options or {}).items() if key.startswith("_apex_")
        }
        next_reserved = {
            key: value for key, value in next_options.items() if key.startswith("_apex_")
        }
        if not _is_platform_admin(identity) and previous_reserved != next_reserved:
            raise HTTPException(
                status_code=403,
                detail="Reserved _apex_ environment options require an unscoped platform admin",
            )
    target_fields_changed = "base_url" in changes or (
        "options" in changes
        and next_options.get(TRUSTED_PRIVATE_HOST_OPTION)
        != (env.options or {}).get(TRUSTED_PRIVATE_HOST_OPTION)
    )
    if target_fields_changed:
        _validate_environment_options(identity, next_options)
        next_base_url = changes.get("base_url", env.base_url)
        changes["target_approved"] = _approve_environment_target(
            identity, next_base_url, next_options
        )
        changes["target_version"] = int(env.target_version) + 1
    write_error: HTTPException | None = None
    try:
        env = await repo.update_environment(env, changes, hosts=hosts)
    except DuplicateNameError:
        write_error = HTTPException(
            status_code=409,
            detail="environment name already exists on application",
        )
    if write_error is not None:
        raise write_error
    return _environment_out(env)


@router.post(
    "/environments/{environment_id}/target-approval",
    operation_id="approveEnvironmentTarget",
)
async def approve_environment_target(
    environment_id: EnvironmentId,
    body: EnvironmentTargetApproval,
    identity: AdminIdentity,
    repo: CatalogRepo,
) -> EnvironmentOut:
    """Approve the current target under a row lock without replaying a stale URL."""
    if not _is_platform_admin(identity):
        raise HTTPException(
            status_code=403,
            detail="Execution targets require approval by an unscoped platform admin",
        )
    env = await repo.get_environment_for_update(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    if env.target_version != body.expected_target_version:
        raise HTTPException(
            status_code=409,
            detail="Environment target changed. Reload the environment before approving it.",
        )
    options = env.options or {}
    _validate_environment_options(identity, options)
    approved = _approve_environment_target(identity, env.base_url, options)
    if not approved:
        raise HTTPException(status_code=422, detail="Set an execution target URL before approval")
    env = await repo.update_environment(
        env,
        {"target_approved": True, "target_version": int(env.target_version) + 1},
    )
    return _environment_out(env)


@router.delete("/environments/{environment_id}", operation_id="deleteEnvironment", status_code=204)
async def delete_environment(
    environment_id: EnvironmentId, identity: OperatorIdentity, repo: CatalogRepo
) -> None:
    env = await repo.get_environment_for_update(environment_id)
    if env is None or not _can_access_environment(identity, env):
        raise _not_found("environment", environment_id)
    # Every project using the application uses its environments too.
    _require_change_everywhere(identity, env.application, "deleting a shared environment")
    try:
        await repo.delete_environment(env)
    except CatalogInUseError:
        raise HTTPException(
            409, "Environment is referenced by retained records and cannot be deleted"
        ) from None
