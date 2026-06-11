"""Prompt catalog routes (`/prompts`, mounted under `/v1` by the app).

Role gating per convention: GET = any authenticated consumer, mutations =
operator+. Prompts are global (no project_id column), so no project scoping
applies. testPrompt launches a stateless background run on the `playground`
assistant via the loopback client, forwarding the caller's API key so authz
and attribution apply exactly as for direct LangGraph calls.
"""

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, require_role
from apex.auth.identity import ConsumerIdentity, Role
from apex.auth.service import extract_api_key
from apex.persistence.db import get_session
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

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/prompts", tags=["prompts"])

PLAYGROUND_ASSISTANT = "playground"


def get_catalog(session: Annotated[AsyncSession, Depends(get_session)]) -> PromptCatalogService:
    return PromptCatalogService(PromptRepository(session))


CatalogDep = Annotated[PromptCatalogService, Depends(get_catalog)]
OperatorIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.OPERATOR))]


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
    namespace: str
    key: str
    description: str | None = None
    content: str
    note: str | None = None


class SaveVersionRequest(BaseModel):
    content: str
    note: str | None = None


class RollbackRequest(BaseModel):
    version_id: str


class TestPromptRequest(BaseModel):
    version_id: str | None = None
    content: str | None = None
    sample_input: dict[str, Any] | None = None


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


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", operation_id="listPrompts")
async def list_prompts(
    identity: CurrentIdentity,
    catalog: CatalogDep,
    namespace: str | None = None,
    include_archived: bool = False,
    q: str | None = None,
) -> list[PromptSummary]:
    rows = await catalog.list_prompts(namespace=namespace, include_archived=include_archived, q=q)
    return [_summary(prompt, active) for prompt, active in rows]


@router.post("", operation_id="createPrompt", status_code=201)
async def create_prompt(
    body: CreatePromptRequest, identity: OperatorIdentity, catalog: CatalogDep
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _detail(prompt, version)


@router.get("/{prompt_id}", operation_id="getPrompt")
async def get_prompt(
    prompt_id: str, identity: CurrentIdentity, catalog: CatalogDep
) -> PromptDetail:
    try:
        prompt, active = await catalog.get_prompt(prompt_id)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _detail(prompt, active)


@router.post("/{prompt_id}/versions", operation_id="savePromptVersion", status_code=201)
async def save_prompt_version(
    prompt_id: str, body: SaveVersionRequest, identity: OperatorIdentity, catalog: CatalogDep
) -> PromptVersionDetail:
    try:
        _, version = await catalog.save_version(
            prompt_id, content=body.content, note=body.note, created_by=identity.name
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _version_detail(version)


@router.get("/{prompt_id}/versions", operation_id="listPromptVersions")
async def list_prompt_versions(
    prompt_id: str, identity: CurrentIdentity, catalog: CatalogDep
) -> list[PromptVersionInfo]:
    try:
        versions = await catalog.list_versions(prompt_id)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_version_info(version) for version in versions]


@router.get("/{prompt_id}/versions/{version_id}", operation_id="getPromptVersion")
async def get_prompt_version(
    prompt_id: str, version_id: str, identity: CurrentIdentity, catalog: CatalogDep
) -> PromptVersionDetail:
    try:
        version = await catalog.get_version(prompt_id, version_id)
    except PromptVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _version_detail(version)


@router.post("/{prompt_id}/rollback", operation_id="rollbackPrompt")
async def rollback_prompt(
    prompt_id: str, body: RollbackRequest, identity: OperatorIdentity, catalog: CatalogDep
) -> PromptDetail:
    try:
        prompt, version = await catalog.rollback(prompt_id, body.version_id)
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PromptVersionMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _detail(prompt, version)


@router.post("/{prompt_id}/archive", operation_id="archivePrompt")
async def archive_prompt(
    prompt_id: str, identity: OperatorIdentity, catalog: CatalogDep
) -> PromptSummary:
    return await _set_archived(catalog, prompt_id, archived=True)


@router.post("/{prompt_id}/unarchive", operation_id="unarchivePrompt")
async def unarchive_prompt(
    prompt_id: str, identity: OperatorIdentity, catalog: CatalogDep
) -> PromptSummary:
    return await _set_archived(catalog, prompt_id, archived=False)


async def _set_archived(
    catalog: PromptCatalogService, prompt_id: str, *, archived: bool
) -> PromptSummary:
    try:
        await catalog.set_archived(prompt_id, archived)
        prompt, active = await catalog.get_prompt(prompt_id)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _summary(prompt, active)


@router.post("/{prompt_id}/test", operation_id="testPrompt", status_code=202)
async def test_prompt(
    prompt_id: str,
    body: TestPromptRequest,
    request: Request,
    identity: OperatorIdentity,
    catalog: CatalogDep,
) -> TestPromptResponse:
    """Run the prompt on the `playground` assistant as a stateless background run.

    Content precedence: explicit body.content -> body.version_id -> active version.
    Responds 202 with the run/thread ids; results are fetched via the LangGraph API.
    """
    try:
        _, active = await catalog.get_prompt(prompt_id)
        if body.content is not None:
            content = body.content
        elif body.version_id is not None:
            content = (await catalog.get_version(prompt_id, body.version_id)).content
        elif active is not None:
            content = active.content
        else:
            raise HTTPException(
                status_code=409,
                detail="prompt has no active version; provide content or version_id",
            )
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    sample = dict(body.sample_input or {})
    run_input = {
        "prompt": {"system": content, "user": str(sample.get("user") or "")},
        "sample_input": sample,
    }
    client = loopback_client(extract_api_key(request.headers))
    try:
        # thread_id=None creates a stateless run; keep the scratch thread so the
        # operator can inspect the output after completion.
        run = await client.runs.create(
            None,
            PLAYGROUND_ASSISTANT,
            input=run_input,
            metadata={
                "purpose": "prompt_test",
                "prompt_id": prompt_id,
                "requested_by": identity.name,
            },
            on_completion="keep",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("apex.prompts.playground_run_failed", prompt_id=prompt_id, exc_info=True)
        raise HTTPException(status_code=502, detail="failed to create playground run") from exc
    thread_id = run.get("thread_id")
    return TestPromptResponse(
        run_id=str(run["run_id"]), thread_id=str(thread_id) if thread_id else None
    )
