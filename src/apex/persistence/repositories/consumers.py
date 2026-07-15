"""Async repository for the ApiConsumer aggregate (consumer + scopes).

Raw API keys are never persisted: callers hash with `apex.auth.service.hash_api_key`
before handing the digest to this repository.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, false, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.auth.identity import ScopeRef
from apex.persistence.models import ApiConsumer, ConsumerDeletionRecord, ConsumerKey, ConsumerScope


class DuplicateConsumerNameError(Exception):
    """The database rejected a duplicate API-consumer name."""


class AmbiguousConsumerKeyExpiryError(Exception):
    """A legacy rotated key's expiry provenance cannot be changed safely."""


_CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE = "_apex_credential_response_key_hash"


def consume_credential_response_key_hash(consumer: ApiConsumer) -> str:
    """Return a recovered one-time credential hash without changing mapped state."""

    value = getattr(consumer, _CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE, consumer.key_hash)
    if hasattr(consumer, _CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE):
        delattr(consumer, _CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE)
    return str(value)


class ConsumersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(
        self,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ApiConsumer]:
        stmt = select(ApiConsumer).where(ApiConsumer.deleted_at.is_(None))
        if allowed_scopes is not None:
            allowed_scope = _delegable_scope_predicate(allowed_scopes)
            if allowed_scope is None:
                stmt = stmt.where(false())
            else:
                # A scoped administrator may manage only consumers with at least
                # one scope, every one of which is delegable by the administrator.
                stmt = stmt.where(
                    ApiConsumer.scopes.any(),
                    ~ApiConsumer.scopes.any(not_(allowed_scope)),
                )
        result = await self._session.scalars(
            stmt.order_by(ApiConsumer.created_at, ApiConsumer.id).limit(limit).offset(offset)
        )
        return list(result)

    async def get(self, consumer_id: str) -> ApiConsumer | None:
        consumer = await self._session.get(ApiConsumer, consumer_id)
        if consumer is None or consumer.deleted_at is not None:
            return None
        return consumer

    async def get_for_update(self, consumer_id: str) -> ApiConsumer | None:
        result = await self._session.scalars(
            select(ApiConsumer)
            .where(ApiConsumer.id == consumer_id)
            .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        consumer = result.first()
        if consumer is None or consumer.deleted_at is not None:
            return None
        return consumer

    async def get_by_name(self, name: str) -> ApiConsumer | None:
        return await self._session.scalar(select(ApiConsumer).where(ApiConsumer.name == name))

    async def create(
        self,
        *,
        name: str,
        consumer_type: str,
        role: str,
        key_hash: str,
        scopes: Sequence[ScopeRef] = (),
        expires_at: datetime | None = None,
        created_by: str | None = None,
    ) -> ApiConsumer:
        consumer = ApiConsumer(
            id=uuid4().hex,
            name=name,
            consumer_type=consumer_type,
            role=role,
            key_hash=key_hash,
            enabled=True,
            expires_at=expires_at,
            created_by=created_by,
            updated_by=created_by,
            scopes=[
                ConsumerScope(project_id=scope.project_id, app_id=scope.app_id) for scope in scopes
            ],
            keys=[
                ConsumerKey(
                    key_hash=key_hash,
                    expiry_source="independent",
                    created_by=created_by,
                )
            ],
        )
        self._session.add(consumer)
        return await self._commit_name_write(consumer, resolve_key_hash=key_hash)

    async def update(
        self,
        consumer_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        scopes: Sequence[ScopeRef] | None = None,
        expires_at: datetime | None = None,
        expires_at_set: bool = False,
        revoked_at: datetime | None = None,
        revoked_at_set: bool = False,
        updated_by: str | None = None,
    ) -> ApiConsumer | None:
        """Partially update a consumer, preserving omitted nullable fields.

        The ``*_set`` flags distinguish an omitted field from an explicit JSON
        ``null``.  Non-null values remain updates for backwards compatibility
        with repository callers that predate those flags.
        """
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return None
        return await self.update_existing(
            consumer,
            name=name,
            role=role,
            enabled=enabled,
            scopes=scopes,
            expires_at=expires_at,
            expires_at_set=expires_at_set,
            revoked_at=revoked_at,
            revoked_at_set=revoked_at_set,
            updated_by=updated_by,
        )

    async def update_existing(
        self,
        consumer: ApiConsumer,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        scopes: Sequence[ScopeRef] | None = None,
        expires_at: datetime | None = None,
        expires_at_set: bool = False,
        revoked_at: datetime | None = None,
        revoked_at_set: bool = False,
        updated_by: str | None = None,
    ) -> ApiConsumer:
        """Partial update of an already-loaded consumer row."""
        if name is not None:
            consumer.name = name
        if role is not None:
            consumer.role = role
        if enabled is not None:
            consumer.enabled = enabled
        if scopes is not None:
            consumer.scopes = [
                ConsumerScope(project_id=scope.project_id, app_id=scope.app_id) for scope in scopes
            ]
        if expires_at_set or expires_at is not None:
            # During a rolling upgrade, an old pod can still create a legacy
            # initial credential whose expiry was copied from the consumer after
            # migration 0017 took its snapshot.  Only rotation_count=0 plus an
            # exact match proves that inheritance.  Clear that key lazily before
            # changing the independent consumer lifetime.
            old_consumer_expiry = consumer.expires_at
            current_key = next(
                (key for key in consumer.keys if key.key_hash == consumer.key_hash),
                None,
            )
            if (
                current_key is not None
                and old_consumer_expiry is not None
                and current_key.expires_at == old_consumer_expiry
            ):
                source = current_key.expiry_source or (
                    "inherited" if int(consumer.rotation_count or 0) == 0 else "legacy_ambiguous"
                )
                if source == "inherited" or (
                    source == "legacy_ambiguous" and int(consumer.rotation_count or 0) == 0
                ):
                    current_key.expires_at = None
                    current_key.expiry_source = "independent"
                elif source == "legacy_ambiguous":
                    raise AmbiguousConsumerKeyExpiryError(
                        "rotate the current credential before changing the consumer expiry"
                    )
            consumer.expires_at = expires_at
        if revoked_at_set or revoked_at is not None:
            consumer.revoked_at = revoked_at
        if updated_by is not None:
            consumer.updated_by = updated_by
        return await self._commit_name_write(consumer)

    async def _commit_name_write(
        self,
        consumer: ApiConsumer,
        *,
        resolve_key_hash: str | None = None,
    ) -> ApiConsumer:
        # Capture before commit: an ambiguous driver failure can expire ORM state.
        expected_consumer_id = consumer.id
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_consumer_name(exc):
                raise DuplicateConsumerNameError(str(exc.orig)) from exc
            raise
        except Exception:
            # A transport failure can arrive after PostgreSQL committed. For a
            # one-time credential response, the globally unique expected hash is
            # an authoritative commit witness; returning it prevents a successful
            # create/rotation from discarding the only plaintext key copy.
            if resolve_key_hash is not None:
                resolved = await self._resolve_credential_commit(
                    resolve_key_hash,
                    expected_consumer_id=expected_consumer_id,
                )
                if resolved is not None:
                    return resolved
            raise
        # Session factories use expire_on_commit=False and INSERT/UPDATE RETURNING
        # populates server-generated values. A post-commit refresh is not
        # authoritative and must not turn a durable credential write into a 5xx.
        return consumer

    async def _resolve_credential_commit(
        self,
        expected_key_hash: str,
        *,
        expected_consumer_id: str | None,
    ) -> ApiConsumer | None:
        try:
            await self._session.rollback()
            now = datetime.now(UTC)
            stmt = (
                select(ApiConsumer)
                .join(
                    ConsumerKey,
                    ConsumerKey.consumer_id == ApiConsumer.id,
                )
                .where(
                    ConsumerKey.key_hash == expected_key_hash,
                    ConsumerKey.revoked_at.is_(None),
                    or_(ConsumerKey.expires_at.is_(None), ConsumerKey.expires_at > now),
                    ApiConsumer.enabled.is_(True),
                    ApiConsumer.revoked_at.is_(None),
                    or_(ApiConsumer.expires_at.is_(None), ApiConsumer.expires_at > now),
                    ApiConsumer.deleted_at.is_(None),
                )
            )
            if expected_consumer_id is not None:
                stmt = stmt.where(ApiConsumer.id == expected_consumer_id)
            consumer = await self._session.scalar(
                stmt.options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
            )
            if consumer is not None:
                # A later rotation may already have changed ApiConsumer.key_hash.
                # Preserve mapped current state while letting the one-time response
                # fingerprint the exact plaintext credential this request created.
                setattr(
                    consumer,
                    _CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE,
                    expected_key_hash,
                )
            return consumer
        except Exception:
            return None

    async def replace_key_hash(
        self,
        consumer_id: str,
        key_hash: str,
        *,
        rotated_by: str | None = None,
        grace_expires_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApiConsumer | None:
        """Rotate: issue a new key; old active keys may survive until grace_expires_at."""
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return None
        now = datetime.now(UTC)
        # Older bootstrap writers (and a failed best-effort auth backfill) can
        # leave the currently accepted legacy hash only on api_consumers. Make
        # it an explicit credential under the aggregate lock before applying
        # grace, otherwise rotation silently revokes it immediately.
        if not any(key.key_hash == consumer.key_hash for key in consumer.keys):
            consumer.keys.append(
                ConsumerKey(
                    key_hash=consumer.key_hash,
                    expiry_source="independent",
                    created_by=consumer.created_by,
                )
            )
        active_keys = [
            key
            for key in consumer.keys
            if key.revoked_at is None and (key.expires_at is None or key.expires_at > now)
        ]
        for key in active_keys:
            if grace_expires_at is None or grace_expires_at <= now:
                key.revoked_at = now
            elif key.expires_at is None or key.expires_at > grace_expires_at:
                key.expires_at = grace_expires_at
                key.expiry_source = "grace"
        rotated_from_id = active_keys[0].id if active_keys else None
        consumer.keys.append(
            ConsumerKey(
                key_hash=key_hash,
                # Consumer and credential lifetimes are independent gates.  An
                # omitted key expiry must not copy the consumer's current expiry,
                # otherwise extending the consumer later leaves this key dead at
                # the old timestamp.
                expires_at=expires_at,
                expiry_source="explicit" if expires_at is not None else "independent",
                rotated_from_id=rotated_from_id,
                created_by=rotated_by,
            )
        )
        consumer.key_hash = key_hash
        consumer.rotated_at = now
        consumer.rotation_count = int(consumer.rotation_count or 0) + 1
        if rotated_by is not None:
            consumer.updated_by = rotated_by
        return await self._commit_name_write(consumer, resolve_key_hash=key_hash)

    async def delete(self, consumer_id: str, *, deleted_by: str | None = None) -> bool:
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return False
        return await self.delete_existing(consumer, deleted_by=deleted_by)

    async def delete_existing(
        self, consumer: ApiConsumer, *, deleted_by: str | None = None
    ) -> bool:
        """Soft-delete an already locked aggregate and its active keys."""

        if consumer.deleted_at is not None:
            return False
        deleted_at = datetime.now(UTC)
        consumer.deleted_at = deleted_at
        consumer.revoked_at = consumer.revoked_at or deleted_at
        consumer.enabled = False
        consumer.updated_by = deleted_by
        for key in consumer.keys:
            key.revoked_at = key.revoked_at or deleted_at
        self._session.add(
            ConsumerDeletionRecord(
                consumer_id=consumer.id,
                deleted_at=deleted_at,
                deleted_by=deleted_by,
                name=consumer.name,
                consumer_type=consumer.consumer_type,
                role=consumer.role,
                scopes={
                    "scopes": [
                        {"project_id": scope.project_id, "app_id": scope.app_id}
                        for scope in consumer.scopes
                    ]
                },
            )
        )
        await self._session.commit()
        return True


def _is_duplicate_consumer_name(exc: IntegrityError) -> bool:
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    message = str(exc.orig).lower()
    return constraint_name == "uq_api_consumers_name" or (
        "uq_api_consumers_name" in message
        or ("unique constraint failed" in message and "api_consumers.name" in message)
    )


def _delegable_scope_predicate(scopes: Sequence[ScopeRef]) -> Any | None:
    project_wide = {scope.project_id for scope in scopes if scope.app_id is None}
    clauses = [ConsumerScope.project_id == project_id for project_id in sorted(project_wide)]
    clauses.extend(
        and_(
            ConsumerScope.project_id == scope.project_id,
            ConsumerScope.app_id == scope.app_id,
        )
        for scope in scopes
        if scope.app_id is not None and scope.project_id not in project_wide
    )
    return or_(*clauses) if clauses else None
