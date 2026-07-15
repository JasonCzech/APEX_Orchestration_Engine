"""API-consumer administration (`/admin/consumers`, admin-only).

Key handling: the raw API key is generated server-side and returned exactly once —
in the `api_key` field of the create/rotate responses. Only its configured hash
is stored, so `key_fingerprint` is the first 8 hex chars of the stored *hash*
(not of the key); it identifies a credential in the UI but can never reconstruct it.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import require_role
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import hash_api_key
from apex.domain.input_limits import MAX_CHILD_ITEMS, MAX_DB_LIST_OFFSET, NoNulStr, RecordId
from apex.persistence.db import get_session
from apex.persistence.models import ApiConsumer
from apex.persistence.repositories.consumers import (
    AmbiguousConsumerKeyExpiryError,
    ConsumersRepository,
    DuplicateConsumerNameError,
    consume_credential_response_key_hash,
)
from apex.services.audit import append_audit_event_best_effort, event_from_identity

router = APIRouter(prefix="/admin/consumers", tags=["consumers"])

FINGERPRINT_LENGTH = 8


def get_consumers_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConsumersRepository:
    return ConsumersRepository(session)


ConsumersRepo = Annotated[ConsumersRepository, Depends(get_consumers_repository)]
AdminIdentity = Annotated[ConsumerIdentity, Depends(require_role(Role.ADMIN))]
ConsumerId = Annotated[RecordId, Path(description="Consumer id")]


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
    model_config = ConfigDict(extra="forbid")

    name: NoNulStr = Field(min_length=1, max_length=255)
    consumer_type: ConsumerType
    role: Role
    scopes: list[ScopeRef] = Field(default_factory=list, max_length=MAX_CHILD_ITEMS)
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[ScopeRef]) -> list[ScopeRef]:
        return _validate_scope_set(scopes)


class ConsumerUpdateRequest(BaseModel):
    """Partial update; omitted fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: NoNulStr | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    enabled: bool | None = None
    scopes: list[ScopeRef] | None = Field(default=None, max_length=MAX_CHILD_ITEMS)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[ScopeRef] | None) -> list[ScopeRef] | None:
        return _validate_scope_set(scopes) if scopes is not None else None


class RotateConsumerKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_period_seconds: int = Field(default=0, ge=0, le=604800)
    expires_at: datetime | None = None


MIN_SELF_ROTATION_GRACE_SECONDS = 60


# ── Helpers ──────────────────────────────────────────────────────────────────


def _as_utc(value: datetime) -> datetime:
    """Normalize lifecycle timestamps; legacy clients may omit an offset."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise HTTPException(
            status_code=422,
            detail="lifecycle timestamp is outside the representable UTC range",
        ) from exc


def _validate_scope_set(scopes: list[ScopeRef]) -> list[ScopeRef]:
    keys = [(scope.project_id, scope.app_id) for scope in scopes]
    if len(set(keys)) != len(keys):
        raise ValueError("scopes must not contain duplicate project/app entries")
    project_wide = {scope.project_id for scope in scopes if scope.app_id is None}
    if any(scope.app_id is not None and scope.project_id in project_wide for scope in scopes):
        raise ValueError("app scopes are redundant when the same project is project-wide")
    return scopes


def _read_model(consumer: ApiConsumer) -> ConsumerRead:
    response_key_hash = consume_credential_response_key_hash(consumer)
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
        key_fingerprint=response_key_hash[:FINGERPRINT_LENGTH],
    )


def _created_model(consumer: ApiConsumer, api_key: str) -> ConsumerCreated:
    return ConsumerCreated(**_read_model(consumer).model_dump(), api_key=api_key)


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


async def _get_or_404(repo: ConsumersRepository, consumer_id: str) -> ApiConsumer:
    consumer = await repo.get(consumer_id)
    if consumer is None:
        raise HTTPException(status_code=404, detail="consumer not found")
    return consumer


async def _get_for_update_or_404(repo: ConsumersRepository, consumer_id: str) -> ApiConsumer:
    consumer = await repo.get_for_update(consumer_id)
    if consumer is None:
        raise HTTPException(status_code=404, detail="consumer not found")
    return consumer


def _scope_refs(consumer: ApiConsumer) -> list[ScopeRef]:
    return [ScopeRef(project_id=scope.project_id, app_id=scope.app_id) for scope in consumer.scopes]


def _consumer_is_unscoped_admin(role: Role, scopes: list[ScopeRef]) -> bool:
    return role is Role.ADMIN and not scopes


def _scopes_inside_admin(identity: ConsumerIdentity, scopes: list[ScopeRef]) -> bool:
    return (
        identity.is_unscoped
        or bool(scopes)
        and all(identity.contains_scope(scope) for scope in scopes)
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
    raise HTTPException(status_code=404, detail="consumer not found")


# ── Routes (all admin-only) ──────────────────────────────────────────────────


@router.get("", operation_id="listConsumers")
async def list_consumers(
    identity: AdminIdentity,
    repo: ConsumersRepo,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)] = 0,
) -> list[ConsumerRead]:
    return [
        _read_model(consumer)
        for consumer in await repo.list_all(
            allowed_scopes=None if identity.is_unscoped else identity.scopes,
            limit=limit,
            offset=offset,
        )
        if _can_manage_consumer(identity, consumer)
    ]


@router.post("", operation_id="createConsumer", status_code=201)
async def create_consumer(
    body: ConsumerCreateRequest,
    identity: AdminIdentity,
    repo: ConsumersRepo,
    response: Response,
) -> ConsumerCreated:
    expires_at = _as_utc(body.expires_at) if body.expires_at is not None else None
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    await _ensure_can_grant(identity, body.role, body.scopes, action="consumer.create")
    if await repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="consumer name already exists")
    api_key = _generate_api_key()
    try:
        consumer = await repo.create(
            name=body.name,
            consumer_type=body.consumer_type.value,
            role=body.role.value,
            key_hash=hash_api_key(api_key),
            scopes=body.scopes,
            expires_at=expires_at,
            created_by=identity.consumer_id,
        )
    except DuplicateConsumerNameError as exc:
        raise HTTPException(status_code=409, detail="consumer name already exists") from exc
    await _audit_consumer_action(identity, "consumer.create", consumer.id)
    # This response is the only time the raw credential exists outside the
    # caller. Prevent browsers and intermediaries from retaining a reusable key.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
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
    expires_at = _as_utc(body.expires_at) if body.expires_at is not None else None
    revoked_at = _as_utc(body.revoked_at) if body.revoked_at is not None else None
    now = datetime.now(UTC)
    if revoked_at is not None and revoked_at > now:
        raise HTTPException(status_code=422, detail="revoked_at must not be in the future")
    if consumer_id == identity.consumer_id and body.revoked_at is not None:
        await _audit_consumer_action(
            identity,
            "consumer.update",
            consumer_id,
            decision="denied",
            reason="A consumer cannot revoke itself",
        )
        raise HTTPException(status_code=409, detail="A consumer cannot revoke itself")
    if (
        consumer_id == identity.consumer_id
        and body.expires_at is not None
        and expires_at is not None
        and expires_at < now + timedelta(seconds=MIN_SELF_ROTATION_GRACE_SECONDS)
    ):
        await _audit_consumer_action(
            identity,
            "consumer.update",
            consumer_id,
            decision="denied",
            reason="A consumer cannot expire itself inside the response retry window",
        )
        raise HTTPException(
            status_code=409,
            detail="A consumer cannot expire itself inside the response retry window",
        )
    if expires_at is not None and expires_at <= now:
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
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
            raise HTTPException(status_code=409, detail="consumer name already exists")
    next_role = body.role or Role(existing.role)
    next_scopes = body.scopes if body.scopes is not None else _scope_refs(existing)
    await _ensure_can_grant(
        identity,
        next_role,
        next_scopes,
        action="consumer.update",
        consumer_id=consumer_id,
    )
    try:
        updated = await repo.update_existing(
            existing,
            name=body.name,
            role=body.role.value if body.role is not None else None,
            enabled=body.enabled,
            scopes=body.scopes,
            expires_at=expires_at,
            expires_at_set="expires_at" in body.model_fields_set,
            revoked_at=revoked_at,
            revoked_at_set="revoked_at" in body.model_fields_set,
            updated_by=identity.consumer_id,
        )
    except AmbiguousConsumerKeyExpiryError as exc:
        raise HTTPException(
            status_code=409, detail="consumer key expiry update is ambiguous"
        ) from exc
    except DuplicateConsumerNameError as exc:
        raise HTTPException(status_code=409, detail="consumer name already exists") from exc
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
    consumer = await _get_for_update_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, consumer):
        await _raise_unmanageable_consumer(identity, "consumer.delete", consumer_id)
    if not await repo.delete_existing(consumer, deleted_by=identity.consumer_id):
        raise HTTPException(status_code=404, detail="consumer not found")
    await _audit_consumer_action(identity, "consumer.delete", consumer_id)


@router.post("/{consumer_id}/rotate", operation_id="rotateConsumerKey")
async def rotate_consumer_key(
    consumer_id: ConsumerId,
    identity: AdminIdentity,
    repo: ConsumersRepo,
    response: Response,
    body: RotateConsumerKeyRequest | None = None,
) -> ConsumerCreated:
    body = body or RotateConsumerKeyRequest()
    existing = await _get_for_update_or_404(repo, consumer_id)
    if not _can_manage_consumer(identity, existing):
        await _raise_unmanageable_consumer(identity, "consumer.rotate_key", consumer_id)
    now = datetime.now(UTC)
    expires_at = _as_utc(body.expires_at) if body.expires_at is not None else None
    if expires_at is not None and expires_at <= now:
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    if consumer_id == identity.consumer_id:
        if body.grace_period_seconds < MIN_SELF_ROTATION_GRACE_SECONDS:
            reason = (
                "Rotating your own key requires a grace period of at least "
                f"{MIN_SELF_ROTATION_GRACE_SECONDS} seconds"
            )
            await _audit_consumer_action(
                identity,
                "consumer.rotate_key",
                consumer_id,
                decision="denied",
                reason=reason,
            )
            raise HTTPException(status_code=409, detail=reason)
        grace_deadline = now + timedelta(seconds=body.grace_period_seconds)
        if expires_at is not None and expires_at <= grace_deadline:
            reason = "A self-rotated key must remain valid beyond the old key's grace period"
            await _audit_consumer_action(
                identity,
                "consumer.rotate_key",
                consumer_id,
                decision="denied",
                reason=reason,
            )
            raise HTTPException(status_code=409, detail=reason)
    api_key = _generate_api_key()
    grace_expires_at = None
    if body.grace_period_seconds > 0:
        grace_expires_at = now + timedelta(seconds=body.grace_period_seconds)
    consumer = await repo.replace_key_hash(
        consumer_id,
        hash_api_key(api_key),
        rotated_by=identity.consumer_id,
        grace_expires_at=grace_expires_at,
        expires_at=expires_at,
    )
    if consumer is None:
        raise HTTPException(status_code=404, detail="consumer not found")
    await _audit_consumer_action(identity, "consumer.rotate_key", consumer_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return _created_model(consumer, api_key)
