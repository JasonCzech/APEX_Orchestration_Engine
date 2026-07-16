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

from apex.auth.identity import ConsumerType, Role, ScopeRef
from apex.domain.input_limits import MAX_CHILD_ITEMS, MAX_SCOPE_ID_CHARS
from apex.persistence.models import ApiConsumer, ConsumerDeletionRecord, ConsumerKey, ConsumerScope
from apex.persistence.repositories._conflicts import (
    bounded_driver_message,
    driver_constraint_name,
)
from apex.services.connection_credentials import reject_credential_text


class DuplicateConsumerNameError(Exception):
    """The database rejected a duplicate API-consumer name."""


class AmbiguousConsumerKeyExpiryError(Exception):
    """A legacy rotated key's expiry provenance cannot be changed safely."""


_CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE = "_apex_credential_response_key_hash"
_REDACTED = "[REDACTED]"
_CONSUMER_TYPES = frozenset(member.value for member in ConsumerType)
_ROLES = frozenset(member.value for member in Role)


def _validate_consumer_metadata(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> str | None:
    """Validate one non-credential consumer label without inspecting key hashes."""

    if value is None and allow_none:
        return None
    if type(value) is not str or not 1 <= len(value) <= 255 or "\x00" in value:
        optional = " or null" if allow_none else ""
        raise ValueError(f"{label} must be a 1-255 character string{optional} without U+0000")
    reject_credential_text(value, label=label)
    return value


def _validate_key_hash(value: Any) -> str:
    """Accept only the lowercase SHA-256 digest used by the auth lifecycle."""

    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("consumer key hash must be a 64-character lowercase hexadecimal digest")
    return value


def _validate_enum_value(value: Any, *, label: str, allowed: frozenset[str]) -> str:
    """Require the exact string value persisted by the consumer schema."""

    if type(value) is not str or value not in allowed:
        raise ValueError(f"{label} is not supported")
    return value


def _validate_lifecycle_datetime(value: Any, *, label: str) -> datetime | None:
    """Accept only normalized UTC datetimes; routers normalize before this boundary."""

    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise ValueError(f"{label} must be a UTC-aware datetime or null")
    return value


def _validate_scope_id(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_SCOPE_ID_CHARS
        or value != value.strip()
        or "\x00" in value
    ):
        optional = " or null" if allow_none else ""
        raise ValueError(
            f"{label} must be a trimmed 1-{MAX_SCOPE_ID_CHARS} character string"
            f"{optional} without U+0000"
        )
    reject_credential_text(value, label=label)
    return value


def _validated_consumer_scopes(scopes: Any) -> list[tuple[str, str | None]]:
    """Validate a complete scope set without trusting Pydantic construction."""

    if type(scopes) not in {list, tuple} or len(scopes) > MAX_CHILD_ITEMS:
        raise ValueError(f"consumer scopes must be a list of at most {MAX_CHILD_ITEMS} entries")
    validated: list[tuple[str, str | None]] = []
    for scope in scopes:
        if type(scope) is not ScopeRef or type(scope.__dict__) is not dict:
            raise ValueError("consumer scopes must contain exact ScopeRef values")
        project_id = _validate_scope_id(
            scope.__dict__.get("project_id"),
            label="consumer scope project_id",
        )
        app_id = _validate_scope_id(
            scope.__dict__.get("app_id"),
            label="consumer scope app_id",
            allow_none=True,
        )
        assert project_id is not None
        validated.append((project_id, app_id))

    if len(set(validated)) != len(validated):
        raise ValueError("consumer scopes must not contain duplicate project/app entries")
    project_wide = {project_id for project_id, app_id in validated if app_id is None}
    if any(app_id is not None and project_id in project_wide for project_id, app_id in validated):
        raise ValueError("consumer app scopes are redundant when the project is project-wide")
    return validated


def _validate_update_values(
    *,
    role: Any,
    enabled: Any,
    scopes: Any,
    expires_at: Any,
    expires_at_set: Any,
    revoked_at: Any,
    revoked_at_set: Any,
) -> list[tuple[str, str | None]] | None:
    if role is not None:
        _validate_enum_value(role, label="consumer role", allowed=_ROLES)
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("consumer enabled must be a boolean or null")
    if type(expires_at_set) is not bool or type(revoked_at_set) is not bool:
        raise ValueError("consumer lifecycle field-presence flags must be booleans")
    _validate_lifecycle_datetime(expires_at, label="consumer expires_at")
    _validate_lifecycle_datetime(revoked_at, label="consumer revoked_at")
    return _validated_consumer_scopes(scopes) if scopes is not None else None


def _quarantine_tombstone_text(
    value: Any,
    *,
    max_chars: int,
    allow_none: bool = False,
) -> str | None:
    """Keep safe deletion metadata while replacing any legacy secret-bearing value."""

    if value is None and allow_none:
        return None
    if type(value) is not str or not 1 <= len(value) <= max_chars or "\x00" in value:
        return _REDACTED
    try:
        reject_credential_text(value, label="consumer deletion metadata")
    except ValueError:
        return _REDACTED
    return value


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
        _validate_consumer_metadata(name, label="consumer name")
        _validate_enum_value(
            consumer_type,
            label="consumer type",
            allowed=_CONSUMER_TYPES,
        )
        _validate_enum_value(role, label="consumer role", allowed=_ROLES)
        _validate_consumer_metadata(
            created_by,
            label="consumer created_by",
            allow_none=True,
        )
        _validate_key_hash(key_hash)
        _validate_lifecycle_datetime(expires_at, label="consumer expires_at")
        validated_scopes = _validated_consumer_scopes(scopes)
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
                ConsumerScope(project_id=project_id, app_id=app_id)
                for project_id, app_id in validated_scopes
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
        if name is not None:
            _validate_consumer_metadata(name, label="consumer name")
        if updated_by is not None:
            _validate_consumer_metadata(updated_by, label="consumer updated_by")
        _validate_update_values(
            role=role,
            enabled=enabled,
            scopes=scopes,
            expires_at=expires_at,
            expires_at_set=expires_at_set,
            revoked_at=revoked_at,
            revoked_at_set=revoked_at_set,
        )
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
            _validate_consumer_metadata(name, label="consumer name")
        if updated_by is not None:
            _validate_consumer_metadata(updated_by, label="consumer updated_by")
        validated_scopes = _validate_update_values(
            role=role,
            enabled=enabled,
            scopes=scopes,
            expires_at=expires_at,
            expires_at_set=expires_at_set,
            revoked_at=revoked_at,
            revoked_at_set=revoked_at_set,
        )
        if name is not None:
            consumer.name = name
        if role is not None:
            consumer.role = role
        if enabled is not None:
            consumer.enabled = enabled
        if scopes is not None:
            consumer.scopes = [
                ConsumerScope(project_id=project_id, app_id=app_id)
                for project_id, app_id in validated_scopes or []
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
        duplicate_name = False
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_consumer_name(exc):
                duplicate_name = True
            else:
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
        if duplicate_name:
            # Raise outside the handler so the driver exception (which can contain
            # caller-controlled values and database details) is not retained as
            # __context__ on the domain exception or captured by telemetry.
            raise DuplicateConsumerNameError("consumer name already exists")
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
        _validate_key_hash(key_hash)
        _validate_consumer_metadata(
            rotated_by,
            label="consumer rotated_by",
            allow_none=True,
        )
        _validate_lifecycle_datetime(
            grace_expires_at,
            label="consumer grace_expires_at",
        )
        _validate_lifecycle_datetime(expires_at, label="consumer key expires_at")
        if (
            grace_expires_at is not None
            and expires_at is not None
            and expires_at <= grace_expires_at
        ):
            raise ValueError("consumer key expiry must be later than its old-key grace deadline")
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return None
        now = datetime.now(UTC)
        # Older bootstrap writers (and a failed best-effort auth backfill) can
        # leave the currently accepted legacy hash only on api_consumers. Make
        # it an explicit credential under the aggregate lock before applying
        # grace, otherwise rotation silently revokes it immediately.
        if not any(key.key_hash == consumer.key_hash for key in consumer.keys):
            _validate_key_hash(consumer.key_hash)
            _validate_consumer_metadata(
                consumer.created_by,
                label="consumer created_by",
                allow_none=True,
            )
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
        _validate_consumer_metadata(
            deleted_by,
            label="consumer deleted_by",
            allow_none=True,
        )
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return False
        return await self.delete_existing(consumer, deleted_by=deleted_by)

    async def delete_existing(
        self, consumer: ApiConsumer, *, deleted_by: str | None = None
    ) -> bool:
        """Soft-delete an already locked aggregate and its active keys."""

        _validate_consumer_metadata(
            deleted_by,
            label="consumer deleted_by",
            allow_none=True,
        )
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
                # Legacy/direct-SQL rows can predate the credential-free
                # consumer-id contract.  A deletion tombstone must not turn a
                # malformed primary key into a second durable secret copy.
                consumer_id=_quarantine_tombstone_text(
                    consumer.id,
                    max_chars=32,
                ),
                deleted_at=deleted_at,
                deleted_by=_quarantine_tombstone_text(
                    deleted_by,
                    max_chars=255,
                    allow_none=True,
                ),
                name=_quarantine_tombstone_text(consumer.name, max_chars=255),
                consumer_type=_quarantine_tombstone_text(
                    consumer.consumer_type,
                    max_chars=32,
                ),
                role=_quarantine_tombstone_text(consumer.role, max_chars=32),
                scopes={
                    "scopes": [
                        {
                            "project_id": _quarantine_tombstone_text(
                                scope.project_id,
                                max_chars=255,
                            ),
                            "app_id": _quarantine_tombstone_text(
                                scope.app_id,
                                max_chars=255,
                                allow_none=True,
                            ),
                        }
                        for scope in consumer.scopes
                    ]
                },
            )
        )
        await self._session.commit()
        return True


def _is_duplicate_consumer_name(exc: IntegrityError) -> bool:
    constraint_name = driver_constraint_name(exc.orig)
    message = bounded_driver_message(exc.orig)
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
            # ``list_all`` negates this predicate to find any non-delegable
            # child scope. Make the app comparison two-valued: SQL's
            # ``NOT (app_id = :app)`` is UNKNOWN for a project-wide NULL app_id
            # and would otherwise let that wider scope pass an app-only grant.
            ConsumerScope.app_id.is_not(None),
            ConsumerScope.app_id == scope.app_id,
        )
        for scope in scopes
        if scope.app_id is not None and scope.project_id not in project_wide
    )
    return or_(*clauses) if clauses else None
