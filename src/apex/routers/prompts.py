"""Prompt catalog routes (`/prompts`, mounted under `/v1` by the app).

Role gating per convention: deployment-global prompt GETs are available to any
authenticated consumer. Application prompt rows and all catalog mutations require
an unscoped platform admin because application prompts do not yet carry project
ownership. testPrompt remains operator-accessible for readable prompts and launches
a background run on a bounded scratch thread with the `playground` assistant via
the loopback client, forwarding the caller's API key so authz
and attribution apply exactly as for direct LangGraph calls. Scratch threads use
a deployment-owned one-day delete TTL so inspectable test output cannot accumulate
without bound.
"""

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from langgraph_sdk.errors import APIStatusError
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.domain.input_limits import (
    MAX_DB_LIST_OFFSET,
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    RecordId,
    ScopeId,
)
from apex.persistence.db import get_session, release_read_transactions
from apex.persistence.models import Prompt, PromptVersion
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.langgraph_client import loopback_client
from apex.services.prompts import (
    DuplicatePromptError,
    PromptCatalogService,
    PromptNotFoundError,
    PromptVersionMismatchError,
    PromptVersionNotFoundError,
)
from apex.services.run_validation import (
    MAX_PROMPT_PART_CHARS_HARD,
    validate_playground_run_input,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/prompts", tags=["prompts"])

PLAYGROUND_ASSISTANT = "playground"
PROMPT_TEST_TTL_MINUTES = 24 * 60
_AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES = frozenset({408, 409, 425, 429})


def get_catalog(session: Annotated[AsyncSession, Depends(get_session)]) -> PromptCatalogService:
    return PromptCatalogService(PromptRepository(session))


CatalogDep = Annotated[PromptCatalogService, Depends(get_catalog)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]
AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]
PromptId = Annotated[RecordId, Path(description="Prompt id")]
VersionId = Annotated[RecordId, Path(description="Prompt version id")]


async def require_global_prompt_admin(identity: AdminIdentity) -> ConsumerIdentity:
    """Global prompt writes must not be delegated to a tenant-scoped admin."""

    if not identity.is_unscoped:
        raise HTTPException(
            status_code=403,
            detail="Global prompt mutations require an unscoped admin",
        )
    return identity


GlobalPromptAdmin = Annotated[ConsumerIdentity, Depends(require_global_prompt_admin)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class ActiveVersionRef(BaseModel):
    id: str
    version: int


class PromptSummary(BaseModel):
    id: str
    namespace: str
    key: str
    description: str | None = None
    active_version: ActiveVersionRef | None = None
    archived_at: datetime | None = None
    updated_at: datetime | None = None


class PromptDetail(PromptSummary):
    content: str | None = None  # active version content
    note: str | None = None  # active version note


class PromptVersionInfo(BaseModel):
    id: str
    version: int
    note: str | None = None
    parent_version_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None


class PromptVersionDetail(PromptVersionInfo):
    content: str


class CreatePromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: NoNulStr = Field(min_length=1, max_length=255)
    key: NoNulStr = Field(min_length=1, max_length=255)
    description: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    content: NoNulStr = Field(min_length=1, max_length=MAX_PROMPT_PART_CHARS_HARD)
    note: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


class SaveVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: NoNulStr = Field(min_length=1, max_length=MAX_PROMPT_PART_CHARS_HARD)
    note: NoNulStr | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: NoNulStr = Field(min_length=1, max_length=32)


class TestPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: NoNulStr | None = Field(default=None, min_length=1, max_length=32)
    content: NoNulStr | None = Field(default=None, max_length=MAX_PROMPT_PART_CHARS_HARD)
    sample_input: dict[str, JsonValue] | None = None
    project_id: ScopeId | None = None
    app_id: ScopeId | None = None


class TestPromptResponse(BaseModel):
    run_id: str
    thread_id: str | None = None


# ── Serialization helpers ────────────────────────────────────────────────────


def _summary(prompt: Prompt, active: PromptVersion | None) -> PromptSummary:
    return PromptSummary(
        id=prompt.id,
        namespace=prompt.namespace,
        key=prompt.key,
        description=prompt.description,
        active_version=(ActiveVersionRef(id=active.id, version=active.version) if active else None),
        archived_at=prompt.archived_at,
        updated_at=prompt.updated_at,
    )


def _detail(prompt: Prompt, active: PromptVersion | None) -> PromptDetail:
    summary = _summary(prompt, active)
    return PromptDetail(
        **summary.model_dump(),
        content=active.content if active else None,
        note=active.note if active else None,
    )


def _version_info(version: PromptVersion) -> PromptVersionInfo:
    return PromptVersionInfo(
        id=version.id,
        version=version.version,
        note=version.note,
        parent_version_id=version.parent_version_id,
        created_by=version.created_by,
        created_at=version.created_at,
    )


def _version_detail(version: PromptVersion) -> PromptVersionDetail:
    return PromptVersionDetail(**_version_info(version).model_dump(), content=version.content)


def _prompt_test_scope(
    identity: ConsumerIdentity,
    *,
    project_id: str | None,
    app_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve one exact scratch-thread audience without guessing a tenant."""

    if app_id is not None and project_id is None:
        raise HTTPException(status_code=422, detail="app_id requires project_id")
    if identity.is_unscoped:
        return project_id, app_id

    projects = identity.scoped_project_ids()
    if project_id is None:
        if len(projects) != 1:
            raise HTTPException(
                status_code=422,
                detail="project_id is required when the consumer has multiple project scopes",
            )
        project_id = projects[0]
    if project_id not in projects:
        raise HTTPException(status_code=403, detail="prompt test scope is not authorized")

    if app_id is not None:
        if not identity.allows_scope(project_id=project_id, app_id=app_id):
            raise HTTPException(status_code=403, detail="prompt test scope is not authorized")
        return project_id, app_id

    if any(scope.project_id == project_id and scope.app_id is None for scope in identity.scopes):
        return project_id, None
    app_ids = tuple(
        dict.fromkeys(
            scope.app_id
            for scope in identity.scopes
            if scope.project_id == project_id and scope.app_id is not None
        )
    )
    if len(app_ids) == 1:
        return project_id, app_ids[0]
    raise HTTPException(
        status_code=422,
        detail="app_id is required when the consumer has multiple app scopes",
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listPrompts")
async def list_prompts(
    identity: CurrentIdentity,
    catalog: CatalogDep,
    namespace: Annotated[NoNulStr | None, Query(max_length=255)] = None,
    include_archived: bool = False,
    q: Annotated[NoNulStr | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[PromptSummary]:
    rows = await catalog.list_prompts(
        namespace=namespace,
        include_archived=include_archived,
        q=q,
        allow_application=identity.is_unscoped,
        limit=limit,
        offset=offset,
    )
    return [_summary(prompt, active) for prompt, active in rows]


@router.post("", operation_id="createPrompt", status_code=201)
async def create_prompt(
    body: CreatePromptRequest, identity: GlobalPromptAdmin, catalog: CatalogDep
) -> PromptDetail:
    try:
        prompt, version = await catalog.create_prompt(
            namespace=body.namespace,
            key=body.key,
            content=body.content,
            description=body.description,
            note=body.note,
            created_by=identity.name,
        )
    except DuplicatePromptError as exc:
        raise HTTPException(status_code=409, detail="prompt already exists") from exc
    return _detail(prompt, version)


@router.get("/{prompt_id}", operation_id="getPrompt")
async def get_prompt(
    prompt_id: PromptId, identity: CurrentIdentity, catalog: CatalogDep
) -> PromptDetail:
    try:
        prompt, active = await catalog.get_prompt(
            prompt_id,
            allow_application=identity.is_unscoped,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail="prompt not found") from exc
    return _detail(prompt, active)


@router.post("/{prompt_id}/versions", operation_id="savePromptVersion", status_code=201)
async def save_prompt_version(
    prompt_id: PromptId,
    body: SaveVersionRequest,
    identity: GlobalPromptAdmin,
    catalog: CatalogDep,
) -> PromptVersionDetail:
    try:
        _, version = await catalog.save_version(
            prompt_id, content=body.content, note=body.note, created_by=identity.name
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail="prompt not found") from exc
    return _version_detail(version)


@router.get("/{prompt_id}/versions", operation_id="listPromptVersions")
async def list_prompt_versions(
    prompt_id: PromptId,
    identity: CurrentIdentity,
    catalog: CatalogDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[PromptVersionInfo]:
    try:
        versions = await catalog.list_versions(
            prompt_id,
            allow_application=identity.is_unscoped,
            limit=limit,
            offset=offset,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail="prompt not found") from exc
    return [_version_info(version) for version in versions]


@router.get("/{prompt_id}/versions/{version_id}", operation_id="getPromptVersion")
async def get_prompt_version(
    prompt_id: PromptId,
    version_id: VersionId,
    identity: CurrentIdentity,
    catalog: CatalogDep,
) -> PromptVersionDetail:
    try:
        version = await catalog.get_version(
            prompt_id,
            version_id,
            allow_application=identity.is_unscoped,
        )
    except PromptVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="prompt version not found") from exc
    return _version_detail(version)


@router.post("/{prompt_id}/rollback", operation_id="rollbackPrompt")
async def rollback_prompt(
    prompt_id: PromptId,
    body: RollbackRequest,
    identity: GlobalPromptAdmin,
    catalog: CatalogDep,
) -> PromptDetail:
    try:
        prompt, version = await catalog.rollback(prompt_id, body.version_id)
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="prompt or version not found") from exc
    except PromptVersionMismatchError as exc:
        raise HTTPException(status_code=409, detail="prompt version conflict") from exc
    return _detail(prompt, version)


@router.post("/{prompt_id}/archive", operation_id="archivePrompt")
async def archive_prompt(
    prompt_id: PromptId, identity: GlobalPromptAdmin, catalog: CatalogDep
) -> PromptSummary:
    return await _set_archived(catalog, prompt_id, archived=True)


@router.post("/{prompt_id}/unarchive", operation_id="unarchivePrompt")
async def unarchive_prompt(
    prompt_id: PromptId, identity: GlobalPromptAdmin, catalog: CatalogDep
) -> PromptSummary:
    return await _set_archived(catalog, prompt_id, archived=False)


async def _set_archived(
    catalog: PromptCatalogService, prompt_id: str, *, archived: bool
) -> PromptSummary:
    try:
        await catalog.set_archived(prompt_id, archived)
        prompt, active = await catalog.get_prompt(prompt_id, allow_application=True)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail="prompt not found") from exc
    return _summary(prompt, active)


@router.post("/{prompt_id}/test", operation_id="testPrompt", status_code=202)
async def test_prompt(
    prompt_id: PromptId,
    body: TestPromptRequest,
    request: Request,
    identity: OperatorIdentity,
    catalog: CatalogDep,
) -> TestPromptResponse:
    """Run the prompt on the `playground` assistant as a stateless background run.

    Content precedence: explicit body.content -> body.version_id -> active version.
    Responds 202 with the run/thread ids; results are fetched via the LangGraph API.
    """
    project_id, app_id = _prompt_test_scope(
        identity,
        project_id=body.project_id,
        app_id=body.app_id,
    )
    sample = dict(body.sample_input or {})
    try:
        # Reject oversized/deep samples before reading the catalog. The complete
        # prompt is validated again below once its selected content is known.
        validate_playground_run_input({"sample_input": sample})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid prompt test input") from exc
    try:
        _, active = await catalog.get_prompt(
            prompt_id,
            allow_application=identity.is_unscoped,
        )
        if body.content is not None:
            content = body.content
        elif body.version_id is not None:
            content = (
                await catalog.get_version(
                    prompt_id,
                    body.version_id,
                    allow_application=identity.is_unscoped,
                )
            ).content
        elif active is not None:
            content = active.content
        else:
            raise HTTPException(
                status_code=409,
                detail="prompt has no active version; provide content or version_id",
            )
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="prompt or version not found") from exc

    await release_read_transactions(catalog)

    run_input = {
        "prompt": {"system": content, "user": str(sample.get("user") or "")},
        "sample_input": sample,
        **({"project_id": project_id} if project_id is not None else {}),
        **({"app_id": app_id} if app_id is not None else {}),
    }
    try:
        validated_input = validate_playground_run_input(run_input)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid prompt test input") from exc
    run_input = validated_input.model_dump(mode="json", exclude_none=True)
    client = loopback_client(extract_api_key(request.headers))
    scratch_metadata = {
        "purpose": "prompt_test",
        "prompt_id": prompt_id,
        "requested_by": identity.name,
        **({"project_id": project_id} if project_id is not None else {}),
        **({"app_id": app_id} if app_id is not None else {}),
    }
    scope_config = {
        **({"project_id": project_id} if project_id is not None else {}),
        **({"app_id": app_id} if app_id is not None else {}),
    }
    try:
        # Keep prompt-test output inspectable for one day, then let the server
        # sweep the deployment-owned scratch thread. A stateless keep run has no
        # TTL and would otherwise accumulate forever while public deletion is
        # intentionally disabled.
        thread = await client.threads.create(
            metadata=scratch_metadata,
            ttl={"strategy": "delete", "ttl": PROMPT_TEST_TTL_MINUTES},
        )
        thread_id = str(thread["thread_id"])
    except Exception as exc:
        logger.warning(
            "apex.prompts.playground_thread_failed",
            prompt_id=prompt_id,
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=502, detail="failed to create playground thread") from exc

    try:
        run = await client.runs.create(
            thread_id,
            PLAYGROUND_ASSISTANT,
            input=run_input,
            metadata=scratch_metadata,
            config={"configurable": scope_config},
        )
    except APIStatusError as exc:
        definitive_rejection = (
            400 <= exc.status_code < 500
            and exc.status_code not in _AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES
        )
        if definitive_rejection:
            try:
                await client.threads.delete(thread_id)
            except Exception as cleanup_exc:
                # The short server-owned TTL remains the cleanup fallback.
                logger.warning(
                    "apex.prompts.playground_thread_cleanup_failed",
                    prompt_id=prompt_id,
                    thread_id=thread_id,
                    error_type=cleanup_exc.__class__.__name__,
                )
        logger.warning(
            "apex.prompts.playground_run_failed",
            prompt_id=prompt_id,
            status_code=exc.status_code,
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=502, detail="failed to create playground run") from exc
    except HTTPException:
        raise
    except Exception as exc:
        # A transport/5xx failure can happen after the run commits. Preserve the
        # thread for reconciliation; its bounded TTL prevents a permanent leak.
        logger.warning(
            "apex.prompts.playground_run_failed",
            prompt_id=prompt_id,
            error_type=exc.__class__.__name__,
        )
        raise HTTPException(status_code=502, detail="failed to create playground run") from exc
    return TestPromptResponse(run_id=str(run["run_id"]), thread_id=thread_id)
