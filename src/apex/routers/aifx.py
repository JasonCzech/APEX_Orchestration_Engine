"""AIFx Context Services onboarding (admin).

Organizations name the GitHub Enterprise connection, owner and global-rules
repository; application mappings add an APM ID, team-documents and ACB
repository. Naming conventions only *suggest* repository names: an admin
confirms them. ``preview`` is a dry run of exactly what a launch would pin,
reporting each problem by repository, path and line without file content.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.domain.aifx import (
    AifxEntryType,
    AifxRole,
    suggest_repo,
    validate_branch_ref,
    validate_repo_name,
)
from apex.domain.input_limits import RecordId
from apex.domain.pipeline import Phase
from apex.persistence.db import get_session, release_read_transactions
from apex.persistence.models import AifxApplicationSource, AifxOrganization
from apex.persistence.repositories.aifx import (
    AifxConflictError,
    AifxInUseError,
    AifxRepository,
)
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.connections import ConnectionsRepository
from apex.services.aifx_context import (
    AifxIssue,
    AifxUnavailableError,
    SourceRead,
    check_phase_budgets,
    check_source_count,
    close_adapters,
    open_adapters,
    plan_sources,
    read_sources,
    seal_snapshot,
)
from apex.services.connections import ConnectionResolver, get_connection_resolver
from apex.settings import AifxSettings, get_settings

_APM_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEFAULT_TEAM_DOCUMENTS_PATTERN = "{apm}-team-documents"
DEFAULT_ACB_PATTERN = "{apm}-acb"


def enabled(identity: CurrentIdentity) -> None:
    if not get_settings().aifx_enabled:
        raise HTTPException(404, "AIFx Context Services are not enabled")


router = APIRouter(
    prefix="/admin/aifx",
    tags=["aifx"],
    dependencies=[Depends(enabled), Depends(require_role(Role.ADMIN))],
)


def get_aifx_repository(session: Annotated[AsyncSession, Depends(get_session)]) -> AifxRepository:
    return AifxRepository(session)


def get_aifx_catalog_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CatalogRepository:
    return CatalogRepository(session)


def get_aifx_connections_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionsRepository:
    return ConnectionsRepository(session)


AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]
Repo = Annotated[AifxRepository, Depends(get_aifx_repository)]
Catalog = Annotated[CatalogRepository, Depends(get_aifx_catalog_repository)]
Connections = Annotated[ConnectionsRepository, Depends(get_aifx_connections_repository)]
Resolver = Annotated[ConnectionResolver, Depends(get_connection_resolver)]
OrganizationId = Annotated[RecordId, Path(description="AIFx organization id")]
ApplicationId = Annotated[RecordId, Path(description="Application id")]


# ── schemas ──────────────────────────────────────────────────────────────────


def _pattern(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 120:
        raise ValueError("Naming conventions are at most 120 characters")
    if "{apm}" not in value:
        raise ValueError("Naming conventions must contain {apm}")
    validate_repo_name(value.replace("{apm}", "apm"), label="Naming convention")
    return value


def _optional_repo(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    return validate_repo_name(value, label=label)


def _org_name(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Name the organization")
    return value.strip()


class OrganizationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    connection_id: RecordId
    ghe_owner: str
    global_rules_repo: str | None = "global-rules"
    team_documents_repo_pattern: str | None = DEFAULT_TEAM_DOCUMENTS_PATTERN
    acb_repo_pattern: str | None = DEFAULT_ACB_PATTERN
    default_ref: str = "main"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _org_name(value)

    @field_validator("ghe_owner")
    @classmethod
    def _owner(cls, value: str) -> str:
        return validate_repo_name(value, label="GitHub owner")

    @field_validator("global_rules_repo")
    @classmethod
    def _global(cls, value: str | None) -> str | None:
        return _optional_repo(value, "Global rules repository")

    @field_validator("team_documents_repo_pattern", "acb_repo_pattern")
    @classmethod
    def _patterns(cls, value: str | None) -> str | None:
        return _pattern(value)

    @field_validator("default_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        return validate_branch_ref(value)


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    connection_id: RecordId | None = None
    ghe_owner: str | None = None
    global_rules_repo: str | None = None
    team_documents_repo_pattern: str | None = None
    acb_repo_pattern: str | None = None
    default_ref: str | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _org_name(value)

    @field_validator("ghe_owner")
    @classmethod
    def _owner(cls, value: str | None) -> str | None:
        return None if value is None else validate_repo_name(value, label="GitHub owner")

    @field_validator("global_rules_repo")
    @classmethod
    def _global(cls, value: str | None) -> str | None:
        return _optional_repo(value, "Global rules repository")

    @field_validator("team_documents_repo_pattern", "acb_repo_pattern")
    @classmethod
    def _patterns(cls, value: str | None) -> str | None:
        return _pattern(value)

    @field_validator("default_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return None if value is None else validate_branch_ref(value)


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    connection_id: str
    ghe_owner: str
    global_rules_repo: str | None
    team_documents_repo_pattern: str | None
    acb_repo_pattern: str | None
    default_ref: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ApplicationSourceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: RecordId
    apm_id: str
    team_documents_repo: str | None = None
    acb_repo: str | None = None
    ref: str | None = None
    enabled: bool = True

    @field_validator("apm_id")
    @classmethod
    def _apm(cls, value: str) -> str:
        if _APM_RE.fullmatch(value) is None:
            raise ValueError("APM IDs use letters, digits, '-' or '_' (max 64)")
        # One canonical spelling, so the database's unique constraint is
        # case-insensitive in effect.
        return value.upper()

    @field_validator("team_documents_repo")
    @classmethod
    def _team(cls, value: str | None) -> str | None:
        return _optional_repo(value, "Team documents repository")

    @field_validator("acb_repo")
    @classmethod
    def _acb(cls, value: str | None) -> str | None:
        return _optional_repo(value, "ACB repository")

    @field_validator("ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return None if value in (None, "") else validate_branch_ref(value)


class ApplicationSourceOut(BaseModel):
    application_id: str
    application_name: str | None
    organization_id: str
    apm_id: str
    team_documents_repo: str | None
    acb_repo: str | None
    ref: str | None
    enabled: bool
    updated_at: datetime | None = None


class ApplicationSourceSuggestion(BaseModel):
    organization_id: str
    apm_id: str
    team_documents_repo: str | None
    acb_repo: str | None
    ref: str


class PreviewEntry(BaseModel):
    id: str
    type: AifxEntryType
    title: str
    path: str
    phases: list[Phase] | str
    chars: int


class PreviewSource(BaseModel):
    role: AifxRole
    repo: str
    ref: str
    ok: bool
    commit_sha: str | None = None
    web_url: str | None = None
    application_id: str | None = None
    entries: list[PreviewEntry] = Field(default_factory=list)


class PreviewIssue(BaseModel):
    repo: str
    path: str | None = None
    lines: list[int] = Field(default_factory=list)
    message: str


class AifxPreview(BaseModel):
    ok: bool
    sources: list[PreviewSource]
    issues: list[PreviewIssue]
    total_chars: int


# ── helpers ──────────────────────────────────────────────────────────────────


def _platform_admin(identity: ConsumerIdentity, action: str) -> None:
    # The organization's connection carries a secret reference; changing which
    # repositories every onboarded application reads is a platform decision.
    if not identity.is_unscoped:
        raise HTTPException(403, f"{action} requires an unscoped platform admin")


def _can_access(identity: ConsumerIdentity, app: Any) -> bool:
    return any(
        identity.allows_scope(project_id=project_id, app_id=app.id)
        for project_id in app.project_ids
    ) or (identity.is_unscoped and not app.project_ids)


def _can_change_everywhere(identity: ConsumerIdentity, app: Any) -> bool:
    if identity.is_unscoped:
        return True
    return bool(app.project_ids) and all(
        identity.allows_scope(project_id=project_id, app_id=app.id)
        for project_id in app.project_ids
    )


async def _application(catalog: CatalogRepository, identity: ConsumerIdentity, app_id: str) -> Any:
    app = await catalog.get_application(app_id)
    if app is None or not _can_access(identity, app):
        raise HTTPException(404, "application not found")
    return app


async def _organization(repo: AifxRepository, organization_id: str) -> AifxOrganization:
    org = await repo.get_organization(organization_id)
    if org is None:
        raise HTTPException(404, "AIFx organization not found")
    return org


async def _require_global_source_control(connections: ConnectionsRepository, conn_id: str) -> None:
    conn = await connections.get(conn_id)
    if conn is None or conn.kind != "source_control":
        raise HTTPException(422, "Choose a source-control connection")
    if conn.project_id is not None:
        raise HTTPException(
            422, "AIFx reads repositories for every project; use a global connection"
        )


async def _check_draft(
    repo: AifxRepository, identity: ConsumerIdentity, app: Any, body: ApplicationSourceWrite
) -> AifxOrganization:
    """What saving (or previewing) a mapping requires before anything is read."""

    if not _can_change_everywhere(identity, app):
        raise HTTPException(
            403, "Onboarding requires access through every project that uses the application"
        )
    org = await _organization(repo, body.organization_id)
    if await repo.apm_taken(body.apm_id, except_application_id=app.id):
        raise HTTPException(409, "this APM ID is already mapped to another application")
    return org


def _require_conventions(
    identity: ConsumerIdentity, org: AifxOrganization, body: ApplicationSourceWrite
) -> None:
    """Scoped admins may only use the organization's naming conventions.

    Which repository steers an application's agents is otherwise a platform
    decision: a project admin could point an application at any repository the
    GitHub App can read.
    """

    if identity.is_unscoped:
        return
    expected = {
        "team documents": suggest_repo(org.team_documents_repo_pattern, body.apm_id),
        "ACB": suggest_repo(org.acb_repo_pattern, body.apm_id),
    }
    given = {"team documents": body.team_documents_repo, "ACB": body.acb_repo}
    for label, value in given.items():
        if value is not None and value != expected[label]:
            convention = expected[label] or "none"
            raise HTTPException(
                403,
                f"Only an unscoped platform admin can map a {label} repository that does "
                f"not follow the organization's naming convention ({convention})",
            )


def _source_out(row: AifxApplicationSource, name: str | None) -> ApplicationSourceOut:
    return ApplicationSourceOut(
        application_id=row.application_id,
        application_name=name,
        organization_id=row.organization_id,
        apm_id=row.apm_id,
        team_documents_repo=row.team_documents_repo,
        acb_repo=row.acb_repo,
        ref=row.ref,
        enabled=row.enabled,
        updated_at=row.updated_at,
    )


def _launch_checks(reads: list[SourceRead], settings: AifxSettings) -> list[AifxIssue]:
    """The snapshot and per-phase budget checks a launch would apply to these reads."""

    try:
        snapshot = seal_snapshot(reads, settings=settings, require_all=False)
        if snapshot is not None:
            check_phase_budgets(snapshot)
    except AifxUnavailableError as exc:
        return exc.issues
    return []


def _preview(reads: list[SourceRead], *, extra: Sequence[AifxIssue] = ()) -> AifxPreview:
    sources: list[PreviewSource] = []
    issues: list[PreviewIssue] = []
    for read in reads:
        source = read.source
        sources.append(
            PreviewSource(
                role=read.plan.role,
                repo=read.plan.full_name,
                ref=read.plan.ref,
                ok=read.ok,
                commit_sha=source.commit_sha if source else None,
                web_url=source.web_url if source else None,
                application_id=read.plan.application_id,
                entries=[
                    PreviewEntry(
                        id=entry.id,
                        type=entry.type,
                        title=entry.title,
                        path=entry.path,
                        phases=entry.phases if entry.phases == "all" else list(entry.phases),
                        chars=len(entry.content),
                    )
                    for entry in read.entries
                ],
            )
        )
        issues.extend(
            PreviewIssue(
                repo=issue.repo, path=issue.path, lines=list(issue.lines), message=issue.message
            )
            for issue in read.issues
        )
    issues.extend(
        PreviewIssue(
            repo=issue.repo, path=issue.path, lines=list(issue.lines), message=issue.message
        )
        for issue in extra
    )
    total = sum(entry.chars for source in sources for entry in source.entries)
    return AifxPreview(
        ok=bool(sources) and not issues, sources=sources, issues=issues, total_chars=total
    )


# ── organizations ────────────────────────────────────────────────────────────


@router.get("/organizations", operation_id="listAifxOrganizations")
async def list_organizations(repo: Repo) -> list[OrganizationOut]:
    return [OrganizationOut.model_validate(row) for row in await repo.list_organizations()]


@router.post("/organizations", status_code=201, operation_id="createAifxOrganization")
async def create_organization(
    body: OrganizationWrite, identity: AdminIdentity, repo: Repo, connections: Connections
) -> OrganizationOut:
    _platform_admin(identity, "Onboarding an AIFx organization")
    await _require_global_source_control(connections, body.connection_id)
    try:
        row = await repo.create_organization(body.model_dump())
    except AifxConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    return OrganizationOut.model_validate(row)


@router.get("/organizations/{organization_id}", operation_id="getAifxOrganization")
async def get_organization(organization_id: OrganizationId, repo: Repo) -> OrganizationOut:
    return OrganizationOut.model_validate(await _organization(repo, organization_id))


@router.patch("/organizations/{organization_id}", operation_id="updateAifxOrganization")
async def update_organization(
    organization_id: OrganizationId,
    body: OrganizationUpdate,
    identity: AdminIdentity,
    repo: Repo,
    connections: Connections,
) -> OrganizationOut:
    _platform_admin(identity, "Changing an AIFx organization")
    row = await _organization(repo, organization_id)
    changes = body.model_dump(exclude_unset=True)
    for required in ("name", "connection_id", "ghe_owner", "default_ref", "enabled"):
        if required in changes and changes[required] is None:
            raise HTTPException(422, f"{required} cannot be cleared")
    if "connection_id" in changes:
        await _require_global_source_control(connections, changes["connection_id"])
    try:
        row = await repo.update_organization(row, changes)
    except AifxConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    return OrganizationOut.model_validate(row)


@router.delete(
    "/organizations/{organization_id}", status_code=204, operation_id="deleteAifxOrganization"
)
async def delete_organization(
    organization_id: OrganizationId, identity: AdminIdentity, repo: Repo
) -> Response:
    _platform_admin(identity, "Removing an AIFx organization")
    row = await _organization(repo, organization_id)
    try:
        await repo.delete_organization(row)
    except AifxInUseError as exc:
        raise HTTPException(409, f"This organization is in use; {exc}") from None
    return Response(status_code=204)


# ── application mappings ─────────────────────────────────────────────────────


@router.get("/applications", operation_id="listAifxApplicationSources")
async def list_application_sources(
    identity: AdminIdentity,
    repo: Repo,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> list[ApplicationSourceOut]:
    """Mappings ordered by APM ID; a page holds the visible rows of ``limit`` mappings."""

    rows = await repo.list_sources(limit=limit, offset=offset)
    apps = await repo.applications_by_id([row.application_id for row in rows])
    out: list[ApplicationSourceOut] = []
    for row in rows:
        app = apps.get(row.application_id)
        if app is not None and _can_access(identity, app):
            out.append(_source_out(row, app.name))
    return out


@router.get("/applications/{application_id}", operation_id="getAifxApplicationSource")
async def get_application_source(
    application_id: ApplicationId, identity: AdminIdentity, repo: Repo, catalog: Catalog
) -> ApplicationSourceOut:
    app = await _application(catalog, identity, application_id)
    row = await repo.get_source(application_id)
    if row is None:
        raise HTTPException(404, "this application is not onboarded to AIFx")
    return _source_out(row, app.name)


@router.get("/applications/{application_id}/suggest", operation_id="suggestAifxApplicationSource")
async def suggest_application_source(
    application_id: ApplicationId,
    identity: AdminIdentity,
    repo: Repo,
    catalog: Catalog,
    organization_id: Annotated[RecordId, Query()],
    apm_id: Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[^\x00]*$")],
) -> ApplicationSourceSuggestion:
    await _application(catalog, identity, application_id)
    if _APM_RE.fullmatch(apm_id) is None:
        raise HTTPException(422, "APM IDs use letters, digits, '-' or '_' (max 64)")
    org = await _organization(repo, organization_id)
    return ApplicationSourceSuggestion(
        organization_id=org.id,
        apm_id=apm_id,
        team_documents_repo=suggest_repo(org.team_documents_repo_pattern, apm_id),
        acb_repo=suggest_repo(org.acb_repo_pattern, apm_id),
        ref=org.default_ref,
    )


@router.put("/applications/{application_id}", operation_id="putAifxApplicationSource")
async def put_application_source(
    application_id: ApplicationId,
    body: ApplicationSourceWrite,
    identity: AdminIdentity,
    repo: Repo,
    catalog: Catalog,
) -> ApplicationSourceOut:
    app = await _application(catalog, identity, application_id)
    org = await _check_draft(repo, identity, app, body)
    _require_conventions(identity, org, body)
    try:
        row = await repo.put_source(application_id, body.model_dump())
    except AifxConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    return _source_out(row, app.name)


@router.delete(
    "/applications/{application_id}", status_code=204, operation_id="deleteAifxApplicationSource"
)
async def delete_application_source(
    application_id: ApplicationId, identity: AdminIdentity, repo: Repo, catalog: Catalog
) -> Response:
    app = await _application(catalog, identity, application_id)
    if not _can_change_everywhere(identity, app):
        raise HTTPException(
            403, "Offboarding requires access through every project that uses the application"
        )
    row = await repo.get_source(application_id)
    if row is None:
        raise HTTPException(404, "this application is not onboarded to AIFx")
    await repo.delete_source(row)
    return Response(status_code=204)


@router.post("/applications/{application_id}/preview", operation_id="previewAifxApplicationSource")
async def preview_application_source(
    application_id: ApplicationId,
    identity: AdminIdentity,
    repo: Repo,
    catalog: Catalog,
    resolver: Resolver,
    body: ApplicationSourceWrite | None = None,
) -> AifxPreview:
    """Dry run: pin and validate what a launch for this application would read.

    Send the draft mapping as the body to check it before saving; with no body
    the saved mapping is previewed.
    """

    app = await _application(catalog, identity, application_id)
    if body is not None:
        # A draft can name any repository, so it needs the same authority as saving it.
        org = await _check_draft(repo, identity, app, body)
        _require_conventions(identity, org, body)
        fields = body.model_dump()
    else:
        saved = await repo.get_source(application_id)
        if saved is None:
            raise HTTPException(404, "this application is not onboarded to AIFx")
        fields = {
            "organization_id": saved.organization_id,
            "apm_id": saved.apm_id,
            "team_documents_repo": saved.team_documents_repo,
            "acb_repo": saved.acb_repo,
            "ref": saved.ref,
        }
        org = await _organization(repo, fields["organization_id"])
    settings = get_settings().aifx
    # Detached copies: previewing a disabled or draft mapping must not touch the session.
    preview_mapping = AifxApplicationSource(
        application_id=application_id,
        organization_id=fields["organization_id"],
        apm_id=fields["apm_id"],
        team_documents_repo=fields["team_documents_repo"],
        acb_repo=fields["acb_repo"],
        ref=fields["ref"],
        enabled=True,
    )
    preview_org = AifxOrganization(
        id=org.id,
        name=org.name,
        connection_id=org.connection_id,
        ghe_owner=org.ghe_owner,
        global_rules_repo=org.global_rules_repo,
        default_ref=org.default_ref,
        enabled=True,
    )
    plans = plan_sources(
        application_ids=[application_id],
        primary_application_id=application_id,
        mappings=[preview_mapping],
        organizations={org.id: preview_org},
        application_names={application_id: app.name},
        settings=settings,
    )
    try:
        check_source_count(plans, settings)
    except AifxUnavailableError as exc:
        return _preview([], extra=exc.issues)
    connection_id = org.connection_id
    # GitHub reads can take seconds: hand the pooled connection back first.
    await release_read_transactions(repo, catalog)
    adapters, web_bases, failures = await open_adapters(resolver, [connection_id])
    try:
        reads = await read_sources(
            plans,
            adapters,
            settings=settings,
            web_bases=web_bases,
            connection_failures=failures,
        )
    finally:
        await close_adapters(adapters)
    return _preview(reads, extra=_launch_checks(reads, settings))
