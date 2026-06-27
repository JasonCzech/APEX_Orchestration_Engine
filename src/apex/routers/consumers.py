"""API-consumer administration (`/admin/consumers`, admin-only).

Key handling: the raw API key is generated server-side and returned exactly once —
in the `api_key` field of the create/rotate responses. Only its configured hash
is stored, so `key_fingerprint` is the first 8 hex chars of the stored *hash*
(not of the key); it identifies a credential in the UI but can never reconstruct it.
"""

import secrets
from datetime import UTC, datetime, timedelta
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
from apex.services.audit import append_audit_event_best_effort, event_from_identity

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
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_by: str | None = None
    updated_by: str | None = None
    rotated_at: datetime | None = None
    rotation_count: int = 0
    deleted_at: datetime | None = None
    key_fingerprint: str = Field(
        description="First 8 hex chars of the stored key hash (NOT of the raw "
        "key, which is never persisted). Stable identifier for a credential."
    )


class ConsumerCreated(ConsumerRead):
    api_key: str = Field(
        description="The raw API key. Shown exactly once — it is stored only as a "
        "configured hash and can never be retrieved again."
    )


class ConsumerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list)
    expires_at: datetime | None = None


class ConsumerUpdateRequest(BaseModel):
    """Partial update; omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    enabled: bool | None = None
    scopes: list[ScopeRef] | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class RotateConsumerKeyRequest(BaseModel):
    grace_period_seconds: int = Field(default=0, ge=0, le=604800)
    expires_at: datetime | None = None


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
        expires_at=consumer.expires_at,
        revoked_at=consumer.revoked_at,
        created_by=consumer.created_by,
        updated_by=consumer.updated_by,
        rotated_at=consumer.rotated_at,
        rotation_count=int(consumer.rotation_count or 0),
        deleted_at=consumer.deleted_at,
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


async def _get_for_update_or_404(repo: ConsumersRepository, consumer_id: str) -> ApiConsumer:
    consumer = await repo.get_for_update(consumer_id)
    if consumer is None:
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    return consumer


def _scope_refs(consumer: ApiConsumer) -> list[ScopeRef]:
    return [ScopeRef(project_id=scope.project_id, app_id=scope.app_id) for scope in consumer.scopes]


def _consumer_is_unscoped_admin(role: Role, scopes: list[ScopeRef]) -> bool:
    return role is Role.ADMIN and not scopes


def _scopes_inside_admin(identity: ConsumerIdentity, scopes: list[ScopeRef]) -> bool:
    return (
        identity.is_unscoped
        or bool(scopes)
        and all(
            identity.allows_scope(project_id=scope.project_id, app_id=scope.app_id)
            for scope in scopes
        )
    )


def _can_manage_consumer(identity: ConsumerIdentity, consumer: ApiConsumer) -> bool:
    if identity.is_unscoped:
        return True
    role = Role(consumer.role)
    scopes = _scope_refs(consumer)
    return not _consumer_is_unscoped_admin(role, scopes) and _scopes_inside_admin(identity, scopes)


def _grant_denial_detail(
    identity: ConsumerIdentity, role: Role, scopes: list[ScopeRef]
) -> str | None:
    if identity.is_unscoped:
        return None
    if _consumer_is_unscoped_admin(role, scopes):
        return "Scoped admins cannot grant platform admin"
    if not _scopes_inside_admin(identity, scopes):
        return "Scoped admins cannot grant out-of-scope access"
    return None


async def _ensure_can_grant(
    identity: ConsumerIdentity,
    role: Role,
    scopes: list[ScopeRef],
    *,
    action: str,
    consumer_id: str | None = None,
) -> None:
    detail = _grant_denial_detail(identity, role, scopes)
    if detail is None:
        return
    await _audit_consumer_action(
        identity,
        action,
        consumer_id,
        decision="denied",
        reason=detail,
    )
    raise HTTPException(status_code=403, detail=detail)


async def _audit_consumer_action(
    identity: ConsumerIdentity,
    action: str,
    consumer_id: str | None,
    *,
    decision: str = "allowed",
    reason: str | None = None,
) -> None:
    await append_audit_event_best_effort(
        event_from_identity(
            identity=identity,
            category="security_event",
            action=action,
            decision=decision,
            reason=reason,
            resource_type="api_consumer",
            resource_id=consumer_id,
        )
    )


async def _raise_unmanageable_consumer(
    identity: ConsumerIdentity, action: str, consumer_id: str
) -> None:
    await _audit_consumer_action(
        identity,
        action,
        consumer_id,
        decision="denied",
        reason="Consumer is outside this admin's manageable scope",
    )
    raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")


# ── Routes (all admin-only) ──────────────────────────────────────────────────


@router.get("", operation_id="listConsumers")
async def list_consumers(identity: AdminIdentity, repo: ConsumersRepo) -> list[ConsumerRead]:
    return [
        _read_model(consumer)
        for consumer in await repo.list_all()
        if _can_manage_consumer(identity, consumer)
    ]


@router.post("", operation_id="createConsumer", status_code=201)
async def create_consumer(
    body: ConsumerCreateRequest, identity: AdminIdentity, repo: ConsumersRepo
) -> ConsumerCreated:
    await _ensure_can_grant(identity, body.role, body.scopes, action="consumer.create")
    if await repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail=f"Consumer name '{body.name}' already exists")
    api_key = _generate_api_key()
    consumer = await repo.create(
        name=body.name,
        consumer_type=body.consumer_type.value,
        role=body.role.value,
        key_hash=hash_api_key(api_key),
        scopes=body.scopes,
        expires_at=body.expires_at,
        created_by=identity.consumer_id,
    )
    await _audit_consumer_action(identity, "consumer.create", consumer.id)
    return _created_model(consumer, api_key)


@router.get("/{consumer_id}", operation_id="getConsumer")
async def get_consumer(
    consumer_id: ConsumerId, identity: AdminIdentity, repo: ConsumersRepo
) -> ConsumerRead:
    consumer = await _get_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, consumer):
        await _raise_unmanageable_consumer(identity, "consumer.get", consumer_id)
    return _read_model(consumer)


@router.patch("/{consumer_id}", operation_id="updateConsumer")
async def update_consumer(
    consumer_id: ConsumerId,
    body: ConsumerUpdateRequest,
    identity: AdminIdentity,
    repo: ConsumersRepo,
) -> ConsumerRead:
    if body.enabled is False and consumer_id == identity.consumer_id:
        await _audit_consumer_action(
            identity,
            "consumer.update",
            consumer_id,
            decision="denied",
            reason="A consumer cannot disable itself",
        )
        raise HTTPException(status_code=409, detail="A consumer cannot disable itself")
    if consumer_id == identity.consumer_id and (body.role is not None or body.scopes is not None):
        await _audit_consumer_action(
            identity,
            "consumer.update",
            consumer_id,
            decision="denied",
            reason="A consumer cannot change its own role or scopes",
        )
        raise HTTPException(
            status_code=409, detail="A consumer cannot change its own role or scopes"
        )
    existing = await _get_for_update_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, existing):
        await _raise_unmanageable_consumer(identity, "consumer.update", consumer_id)
    if body.name is not None and body.name != existing.name:
        if await repo.get_by_name(body.name) is not None:
            raise HTTPException(
                status_code=409, detail=f"Consumer name '{body.name}' already exists"
            )
    next_role = body.role or Role(existing.role)
    next_scopes = body.scopes if body.scopes is not None else _scope_refs(existing)
    await _ensure_can_grant(
        identity,
        next_role,
        next_scopes,
        action="consumer.update",
        consumer_id=consumer_id,
    )
    updated = await repo.update_existing(
        existing,
        name=body.name,
        role=body.role.value if body.role is not None else None,
        enabled=body.enabled,
        scopes=body.scopes,
        expires_at=body.expires_at,
        revoked_at=body.revoked_at,
        updated_by=identity.consumer_id,
    )
    await _audit_consumer_action(identity, "consumer.update", consumer_id)
    return _read_model(updated)


@router.delete("/{consumer_id}", operation_id="deleteConsumer", status_code=204)
async def delete_consumer(
    consumer_id: ConsumerId, identity: AdminIdentity, repo: ConsumersRepo
) -> None:
    if consumer_id == identity.consumer_id:
        await _audit_consumer_action(
            identity,
            "consumer.delete",
            consumer_id,
            decision="denied",
            reason="A consumer cannot delete itself",
        )
        raise HTTPException(status_code=409, detail="A consumer cannot delete itself")
    consumer = await _get_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, consumer):
        await _raise_unmanageable_consumer(identity, "consumer.delete", consumer_id)
    if not await repo.delete(consumer_id, deleted_by=identity.consumer_id):
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    await _audit_consumer_action(identity, "consumer.delete", consumer_id)


@router.post("/{consumer_id}/rotate", operation_id="rotateConsumerKey")
async def rotate_consumer_key(
    consumer_id: ConsumerId,
    identity: AdminIdentity,
    repo: ConsumersRepo,
    body: RotateConsumerKeyRequest | None = None,
) -> ConsumerCreated:
    api_key = _generate_api_key()
    existing = await _get_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, existing):
        await _raise_unmanageable_consumer(identity, "consumer.rotate_key", consumer_id)
    grace_expires_at = None
    if body is not None and body.grace_period_seconds > 0:
        grace_expires_at = datetime.now(UTC) + timedelta(seconds=body.grace_period_seconds)
    consumer = await repo.replace_key_hash(
        consumer_id,
        hash_api_key(api_key),
        rotated_by=identity.consumer_id,
        grace_expires_at=grace_expires_at,
        expires_at=body.expires_at if body is not None else None,
    )
    if consumer is None:
        raise HTTPException(status_code=404, detail=f"Consumer '{consumer_id}' not found")
    await _audit_consumer_action(identity, "consumer.rotate_key", consumer_id)
    return _created_model(consumer, api_key)
