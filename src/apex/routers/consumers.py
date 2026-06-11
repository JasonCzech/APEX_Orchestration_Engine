"""API-consumer administration (`/admin/consumers`, admin-only).

Key handling: the raw API key is generated server-side and returned exactly once —
in the `api_key` field of the create/rotate responses. Only its sha256 hash is
stored, so `key_fingerprint` is the first 8 hex chars of the stored *hash* (not of
the key); it identifies a credential in the UI but can never reconstruct it.
"""

import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import require_role
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import hash_api_key
from apex.persistence.db import get_session
from apex.persistence.models import ApiConsumer
from apex.persistence.repositories.consumers import ConsumersRepository

router = APIRouter(prefix="/admin/consumers", tags=["consumers"])

FINGERPRINT_LENGTH = 8


def get_consumers_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConsumersRepository:
    return ConsumersRepository(session)


ConsumersRepo = Annotated[ConsumersRepository, Depends(get_consumers_repository)]
AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]
ConsumerId = Annotated[str, Path(description="Consumer id")]


# ── Schemas ──────────────────────────────────────────────────────────────────


class ConsumerRead(BaseModel):
    id: str
    name: str
    consumer_type: ConsumerType
    role: Role
    enabled: bool
    scopes: list[ScopeRef]
    created_at: datetime | None
    last_used_at: datetime | None
    key_fingerprint: str = Field(
        description="First 8 hex chars of the stored sha256 key hash (NOT of the raw "
        "key, which is never persisted). Stable identifier for a credential."
    )


class ConsumerCreated(ConsumerRead):
    api_key: str = Field(
        description="The raw API key. Shown exactly once — it is stored only as a "
        "sha256 hash and can never be retrieved again."
    )


class ConsumerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list)


class ConsumerUpdateRequest(BaseModel):
    """Partial update; omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    enabled: bool | None = None
    scopes: list[ScopeRef] | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read_model(consumer: ApiConsumer) -> ConsumerRead:
    return ConsumerRead(
        id=consumer.id,
        name=consumer.name,
        consumer_type=ConsumerType(consumer.consumer_type),
        role=Role(consumer.role),
        enabled=consumer.enabled,
        scopes=[
            ScopeRef(project_id=scope.project_id, app_id=scope.app_id) for scope in consumer.scopes
        ],
        created_at=consumer.created_at,
        last_used_at=consumer.last_used_at,
        key_fingerprint=consumer.key_hash[:FINGERPRINT_LENGTH],
    )


def _created_model(consumer: ApiConsumer, api_key: str) -> ConsumerCreated:
    return ConsumerCreated(**_read_model(consumer).model_dump(), api_key=api_key)


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


async def _get_or_404(repo: ConsumersRepository, consumer_id: str) -> ApiConsumer:
    consumer = await repo.get(consumer_id)
    if consumer is None:
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    return consumer


# ── Routes (all admin-only) ──────────────────────────────────────────────────


@router.get("", operation_id="listConsumers")
async def list_consumers(identity: AdminIdentity, repo: ConsumersRepo) -> list[ConsumerRead]:
    return [_read_model(consumer) for consumer in await repo.list_all()]


@router.post("", operation_id="createConsumer", status_code=201)
async def create_consumer(
    body: ConsumerCreateRequest, identity: AdminIdentity, repo: ConsumersRepo
) -> ConsumerCreated:
    if await repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail=f"Consumer name '{body.name}' already exists")
    api_key = _generate_api_key()
    consumer = await repo.create(
        name=body.name,
        consumer_type=body.consumer_type.value,
        role=body.role.value,
        key_hash=hash_api_key(api_key),
        scopes=body.scopes,
    )
    return _created_model(consumer, api_key)


@router.get("/{consumer_id}", operation_id="getConsumer")
async def get_consumer(
    consumer_id: ConsumerId, identity: AdminIdentity, repo: ConsumersRepo
) -> ConsumerRead:
    return _read_model(await _get_or_404(repo, consumer_id))


@router.patch("/{consumer_id}", operation_id="updateConsumer")
async def update_consumer(
    consumer_id: ConsumerId,
    body: ConsumerUpdateRequest,
    identity: AdminIdentity,
    repo: ConsumersRepo,
) -> ConsumerRead:
    if body.enabled is False and consumer_id == identity.consumer_id:
        raise HTTPException(status_code=409, detail="A consumer cannot disable itself")
    existing = await _get_or_404(repo, consumer_id)
    if body.name is not None and body.name != existing.name:
        if await repo.get_by_name(body.name) is not None:
            raise HTTPException(
                status_code=409, detail=f"Consumer name '{body.name}' already exists"
            )
    updated = await repo.update(
        consumer_id,
        name=body.name,
        role=body.role.value if body.role is not None else None,
        enabled=body.enabled,
        scopes=body.scopes,
    )
    if updated is None:  # deleted between the two lookups
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    return _read_model(updated)


@router.delete("/{consumer_id}", operation_id="deleteConsumer", status_code=204)
async def delete_consumer(
    consumer_id: ConsumerId, identity: AdminIdentity, repo: ConsumersRepo
) -> None:
    if consumer_id == identity.consumer_id:
        raise HTTPException(status_code=409, detail="A consumer cannot delete itself")
    if not await repo.delete(consumer_id):
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")


@router.post("/{consumer_id}/rotate", operation_id="rotateConsumerKey")
async def rotate_consumer_key(
    consumer_id: ConsumerId, identity: AdminIdentity, repo: ConsumersRepo
) -> ConsumerCreated:
    api_key = _generate_api_key()
    consumer = await repo.replace_key_hash(consumer_id, hash_api_key(api_key))
    if consumer is None:
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    return _created_model(consumer, api_key)
