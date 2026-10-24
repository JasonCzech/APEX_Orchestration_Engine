"""Team and personal test presets (`/test-presets`).

A preset is saved new-test wizard settings: a partial draft whose sections
(systems, execution, phases, prompts, text, context) fill the wizard. Applying
one never launches anything; launch authorizes and validates the resulting test
like any other, so presets need no privilege beyond the caller's own scope.
Organization presets remain LangGraph assistants (platform admins only).

Visibility:
- ``personal`` presets are visible to their creator only;
- ``project`` presets are visible to anyone who may act on the preset's
  application (or its project, when it names none);
- every system named in the payload must be within the caller's scope.
Writes need the operator role; editing a project preset needs project-wide scope
(the creator may always edit their own). Out-of-scope rows answer 404.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, ensure_scope, require_role
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.input_limits import MAX_DB_LIST_OFFSET, NoNulStr, RecordId, ScopeId
from apex.persistence.db import get_session
from apex.persistence.models import TestPreset, TestPresetVersion
from apex.persistence.repositories.catalog import CatalogRepository
from apex.persistence.repositories.test_presets import (
    PresetVersionConflictError,
    TestPresetsRepository,
)
from apex.routers.drafts import (
    _draft_participants_allowed,
    _public_draft_payload,
    _reject_draft_credential_text,
    _validate_draft_payload,
)

router = APIRouter(prefix="/test-presets", tags=["test-presets"])


def get_presets_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TestPresetsRepository:
    return TestPresetsRepository(session)


def get_catalog_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CatalogRepository:
    return CatalogRepository(session)


PresetsRepo = Annotated[TestPresetsRepository, Depends(get_presets_repository)]
CatalogRepo = Annotated[CatalogRepository, Depends(get_catalog_repository)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
PresetId = Annotated[RecordId, Path(description="Test preset id")]
Visibility = Literal["personal", "project"]
_LIST_SCAN_BATCH = 500


# ── Schemas ──────────────────────────────────────────────────────────────────


# Systems belong to the systems section (where the scope check sees them), and
# prompt choices to the prompts section, never to the execution bundle.
_EXECUTION_SCOPE_KEYS = (
    "test_scope",
    "test_scope_snapshot",
    "project_id",
    "app_id",
    "environment_id",
)
_EXECUTION_PROMPT_KEYS = ("prompt_overrides", "prompt_selections")


def _validate_payload(value: dict[str, Any]) -> dict[str, Any]:
    _validate_draft_payload(value)
    sections = value.get("sections")
    if value.get("schema_version") != 1 or not isinstance(sections, dict) or not sections:
        raise ValueError("preset payload needs schema_version 1 and at least one section")
    unknown = set(sections) - {"systems", "execution", "phases", "prompts", "text", "context"}
    if unknown:
        raise ValueError(f"unknown preset sections: {', '.join(sorted(unknown))}")
    execution = sections.get("execution")
    bundle = execution.get("golden_configurable") if isinstance(execution, dict) else None
    if isinstance(execution, dict) and isinstance(bundle, dict):
        dropped = _EXECUTION_SCOPE_KEYS + (() if "prompts" in sections else _EXECUTION_PROMPT_KEYS)
        execution["golden_configurable"] = {
            key: item for key, item in bundle.items() if key not in dropped
        }
    return value


class _PresetText(BaseModel):
    @field_validator("name", "description", "note", check_fields=False)
    @classmethod
    def reject_credential_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reject_draft_credential_text(value, label="preset text")


class TestPresetCreate(_PresetText):
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=2000)
    project_id: ScopeId
    application_id: RecordId | None = None
    visibility: Visibility = "project"
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_payload(value)


class TestPresetUpdate(_PresetText):
    """Replaces name, description and payload. ``application_id`` and
    ``visibility`` change only when sent. ``expected_version`` guards lost updates."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=2000)
    application_id: RecordId | None = None
    visibility: Visibility | None = None
    payload: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)
    note: NoNulStr | None = Field(default=None, max_length=500)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_payload(value)


class TestPresetRead(BaseModel):
    __test__ = False

    id: str
    name: str
    description: str | None
    project_id: str
    application_id: str | None
    visibility: Visibility
    payload: dict[str, Any]
    version: int
    created_by: str | None
    updated_by: str | None
    created_at: datetime | None
    updated_at: datetime | None
    archived_at: datetime | None
    mine: bool
    editable: bool


class TestPresetVersionRead(BaseModel):
    __test__ = False

    version: int
    name: str
    description: str | None
    payload: dict[str, Any]
    note: str | None
    created_by: str | None
    created_at: datetime | None


class PresetUsage(BaseModel):
    kind: Literal["team", "organization"]
    preset_id: str
    tests: int
    last_used_at: datetime | None


# ── Authorization ────────────────────────────────────────────────────────────


def _systems_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The draft-shaped view the drafts participant check understands."""
    sections = payload.get("sections")
    systems = sections.get("systems") if isinstance(sections, dict) else None
    scope = systems.get("scope") if isinstance(systems, dict) else None
    return {"scope": scope} if isinstance(scope, dict) else {}


def _systems_allowed(identity: ConsumerIdentity, payload: dict[str, Any]) -> bool:
    """Every system the payload names, participants and the legacy primary fields alike."""
    systems = _systems_payload(payload)
    if not _draft_participants_allowed(identity, systems):
        return False
    if identity.is_unscoped:
        return True
    scope = systems.get("scope", {})
    project = scope.get("project_id")
    app = scope.get("app_id")
    if (app and not isinstance(app, str)) or (project and not isinstance(project, str)):
        return False
    if project and app:
        return identity.allows_scope(project_id=project, app_id=app)
    if project:
        return identity.allows_project(project)
    return not app


def _audience_allowed(identity: ConsumerIdentity, preset: TestPreset) -> bool:
    if preset.application_id:
        return identity.allows_scope(project_id=preset.project_id, app_id=preset.application_id)
    return identity.allows_project(preset.project_id)


def _visible(identity: ConsumerIdentity, preset: TestPreset) -> bool:
    if preset.visibility == "personal" and preset.created_by_consumer_id != identity.consumer_id:
        return False
    if not _systems_allowed(identity, preset.payload):
        return False
    return _audience_allowed(identity, preset)


def _editable(identity: ConsumerIdentity, preset: TestPreset) -> bool:
    if not identity.role.at_least(Role.OPERATOR) or not _visible(identity, preset):
        return False
    if preset.created_by_consumer_id == identity.consumer_id:
        return True
    return preset.visibility == "project" and identity.contains_scope(
        ScopeRef(project_id=preset.project_id)
    )


def _read(identity: ConsumerIdentity, preset: TestPreset) -> TestPresetRead:
    return TestPresetRead(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        project_id=preset.project_id,
        application_id=preset.application_id,
        visibility=preset.visibility,  # type: ignore[arg-type]
        payload=_public_draft_payload(preset.payload),
        version=preset.version,
        created_by=preset.created_by,
        updated_by=preset.updated_by,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
        archived_at=preset.archived_at,
        mine=preset.created_by_consumer_id == identity.consumer_id,
        editable=_editable(identity, preset),
    )


async def _check_audience(
    identity: ConsumerIdentity,
    catalog: CatalogRepository,
    *,
    project_id: str,
    application_id: str | None,
    visibility: str,
    payload: dict[str, Any],
) -> None:
    """A preset is shared with the audience it names, so writers must be able to reach it."""
    if application_id is not None:
        app = await catalog.get_application(application_id)
        if app is None or project_id not in app.project_ids:
            raise HTTPException(422, "application is not linked to the preset's project")
    if visibility == "project":
        allowed = identity.contains_scope(ScopeRef(project_id=project_id))
    elif application_id is not None:
        allowed = identity.allows_scope(project_id=project_id, app_id=application_id)
    else:
        allowed = identity.allows_project(project_id)
    if not allowed:
        audience = "project-wide" if visibility == "project" else "project"
        raise HTTPException(403, f"saving this preset requires {audience} scope")
    if not _systems_allowed(identity, payload):
        raise HTTPException(403, "Access to every system in the preset is required")


async def _visible_or_404(
    repo: TestPresetsRepository, identity: ConsumerIdentity, preset_id: str
) -> TestPreset:
    preset = await repo.get(preset_id)
    if preset is None or not _visible(identity, preset):
        raise HTTPException(404, "test preset not found")
    return preset


async def _editable_or_404(
    repo: TestPresetsRepository, identity: ConsumerIdentity, preset_id: str
) -> TestPreset:
    preset = await repo.get_for_update(preset_id)
    if preset is None or not _visible(identity, preset):
        raise HTTPException(404, "test preset not found")
    if not _editable(identity, preset):
        raise HTTPException(403, "editing this preset requires project-wide scope")
    return preset


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listTestPresets")
async def list_test_presets(
    identity: CurrentIdentity,
    repo: PresetsRepo,
    project: Annotated[ScopeId | None, Query(description="Filter to one project")] = None,
    application: Annotated[RecordId | None, Query(description="Filter to one application")] = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[TestPresetRead]:
    ensure_scope(identity, project_id=project)
    # Storage filters (projects, others' personal presets) run in SQL; the rest
    # of visibility needs the payload, so page over visible rows, not raw ones.
    visible: list[TestPreset] = []
    scanned = 0
    while len(visible) < offset + limit:
        rows = await repo.list_all(
            project_ids=None if identity.is_unscoped else identity.scoped_project_ids(),
            viewer_consumer_id=identity.consumer_id,
            project_id=project,
            application_id=application,
            include_archived=include_archived,
            limit=_LIST_SCAN_BATCH,
            offset=scanned,
        )
        visible.extend(row for row in rows if _visible(identity, row))
        scanned += len(rows)
        if len(rows) < _LIST_SCAN_BATCH:
            break
    return [_read(identity, row) for row in visible[offset : offset + limit]]


@router.get("/usage", operation_id="listTestPresetUsage")
async def list_test_preset_usage(
    identity: CurrentIdentity,
    repo: PresetsRepo,
    kind: Literal["team", "organization"],
    ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00]*$")]],
        Query(max_length=200),
    ],
) -> list[PresetUsage]:
    """Tests started from these presets, counted over the tests the caller may see."""
    stats = await repo.usage(
        kind=kind,
        preset_ids=ids,
        visible_scopes=None if identity.is_unscoped else tuple(identity.scopes),
    )
    return [
        PresetUsage(kind=kind, preset_id=preset_id, tests=count, last_used_at=last)
        for preset_id, (count, last) in stats.items()
    ]


@router.post("", operation_id="createTestPreset", status_code=201)
async def create_test_preset(
    body: TestPresetCreate,
    identity: OperatorIdentity,
    repo: PresetsRepo,
    catalog: CatalogRepo,
) -> TestPresetRead:
    await _check_audience(
        identity,
        catalog,
        project_id=body.project_id,
        application_id=body.application_id,
        visibility=body.visibility,
        payload=body.payload,
    )
    preset = await repo.create(
        name=body.name,
        description=body.description,
        project_id=body.project_id,
        application_id=body.application_id,
        visibility=body.visibility,
        payload=body.payload,
        created_by=identity.name,
        created_by_consumer_id=identity.consumer_id,
    )
    return _read(identity, preset)


@router.get("/{preset_id}", operation_id="getTestPreset")
async def get_test_preset(
    preset_id: PresetId, identity: CurrentIdentity, repo: PresetsRepo
) -> TestPresetRead:
    return _read(identity, await _visible_or_404(repo, identity, preset_id))


@router.get("/{preset_id}/versions", operation_id="listTestPresetVersions")
async def list_test_preset_versions(
    preset_id: PresetId, identity: CurrentIdentity, repo: PresetsRepo
) -> list[TestPresetVersionRead]:
    await _visible_or_404(repo, identity, preset_id)
    return [
        _version_read(row)
        for row in await repo.versions(preset_id)
        if _systems_allowed(identity, row.payload)
    ]


@router.put("/{preset_id}", operation_id="updateTestPreset")
async def update_test_preset(
    preset_id: PresetId,
    body: TestPresetUpdate,
    identity: OperatorIdentity,
    repo: PresetsRepo,
    catalog: CatalogRepo,
) -> TestPresetRead:
    preset = await _editable_or_404(repo, identity, preset_id)
    visibility = body.visibility or preset.visibility
    application_id = (
        body.application_id if "application_id" in body.model_fields_set else preset.application_id
    )
    await _check_audience(
        identity,
        catalog,
        project_id=preset.project_id,
        application_id=application_id,
        visibility=visibility,
        payload=body.payload,
    )
    # Sharing or un-sharing someone else's preset is the creator's decision.
    if visibility != preset.visibility and preset.created_by_consumer_id != identity.consumer_id:
        raise HTTPException(403, "only the preset's creator can change who sees it")
    try:
        updated = await repo.update(
            preset,
            expected_version=body.expected_version,
            updated_by=identity.name,
            note=body.note,
            name=body.name,
            description=body.description,
            application_id=application_id,
            visibility=visibility,
            payload=body.payload,
        )
    except PresetVersionConflictError:
        raise HTTPException(
            409, "This preset was changed by someone else; reload it and try again"
        ) from None
    return _read(identity, updated)


@router.post("/{preset_id}/archive", operation_id="archiveTestPreset")
async def archive_test_preset(
    preset_id: PresetId, identity: OperatorIdentity, repo: PresetsRepo
) -> TestPresetRead:
    preset = await _editable_or_404(repo, identity, preset_id)
    return _read(identity, await repo.set_archived(preset, True))


@router.post("/{preset_id}/unarchive", operation_id="unarchiveTestPreset")
async def unarchive_test_preset(
    preset_id: PresetId, identity: OperatorIdentity, repo: PresetsRepo
) -> TestPresetRead:
    preset = await _editable_or_404(repo, identity, preset_id)
    return _read(identity, await repo.set_archived(preset, False))


@router.delete("/{preset_id}", operation_id="deleteTestPreset", status_code=204)
async def delete_test_preset(
    preset_id: PresetId, identity: OperatorIdentity, repo: PresetsRepo
) -> None:
    await repo.delete(await _editable_or_404(repo, identity, preset_id))


def _version_read(row: TestPresetVersion) -> TestPresetVersionRead:
    return TestPresetVersionRead(
        version=row.version,
        name=row.name,
        description=row.description,
        payload=_public_draft_payload(row.payload),
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )
