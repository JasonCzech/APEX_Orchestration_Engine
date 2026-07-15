"""API-key -> ConsumerIdentity resolution, shared by the LangGraph and /v1 surfaces."""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import ApiConsumer, ConsumerKey
from apex.settings import get_settings

logger = structlog.get_logger(__name__)
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


class AuthStoreUnavailableError(RuntimeError):
    """The API-key backing store could not be queried."""


class InvalidConsumerIdentityError(ValueError):
    """A persisted credential cannot be represented as a safe identity."""


def hash_api_key(api_key: str) -> str:
    settings = get_settings()
    pepper = settings.auth.api_key_hash_pepper
    if pepper:
        return hmac.new(
            pepper.encode("utf-8"),
            api_key.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    if settings.is_locked_down:
        raise RuntimeError("API key hash pepper is required in locked-down environments")
    return legacy_hash_api_key(api_key)


def legacy_hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _candidate_key_hashes(api_key: str) -> tuple[str, ...]:
    current = hash_api_key(api_key)
    legacy = legacy_hash_api_key(api_key)
    candidates = [current]
    for pepper in get_settings().auth.previous_api_key_hash_peppers:
        candidate = hmac.new(
            pepper.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if candidate not in candidates:
            candidates.append(candidate)
    if legacy not in candidates:
        candidates.append(legacy)
    return tuple(candidates)


HeaderInput = Mapping[Any, Any] | Iterable[tuple[Any, Any]]


def extract_api_key(headers: HeaderInput) -> str | None:
    """Pull the key from `x-api-key` or a bearer `Authorization` header.

    Accepts str- or bytes-keyed mappings (starlette Headers, raw ASGI header dicts).
    """
    try:
        api_key = _get_unique_header(headers, "x-api-key")
        authorization = _get_unique_header(headers, "authorization")
        if api_key is not None and authorization is not None:
            raise ValueError("x-api-key and authorization cannot be combined")
    except (UnicodeError, ValueError):
        return None
    if api_key:
        return api_key
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return None


def _get_unique_header(headers: HeaderInput, name: str) -> str | None:
    matches: list[str] = []
    for key, value in _iter_headers(headers):
        key_str = _decode_header_part(key)
        if key_str.lower() == name:
            matches.append(_decode_header_part(value))
    if len(matches) > 1:
        raise ValueError(f"duplicate {name} headers are not allowed")
    return matches[0] if matches else None


def _iter_headers(headers: HeaderInput) -> Iterable[tuple[Any, Any]]:
    if isinstance(headers, Mapping):
        return headers.items()
    return headers


def _decode_header_part(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _dev_identity() -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="dev",
        name="dev",
        consumer_type=ConsumerType.INTERNAL,
        role=Role.ADMIN,
    )


def _anonymous_identity() -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="anonymous",
        name="anonymous",
        consumer_type=ConsumerType.INTERNAL,
        role=Role.ADMIN,
    )


class IdentityResolver:
    """Resolves API keys to consumer identities.

    Order: auth disabled -> anonymous admin; dev key match -> synthetic admin (no DB);
    otherwise `apex.api_consumers` lookup by sha256 hash. DB errors surface as
    AuthStoreUnavailableError so callers return 503 instead of credential-shaped 401.
    """

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        # Defaults to apex.persistence.db.get_sessionmaker() at resolve time; injectable
        # so tests never touch a real database.
        self._session_factory = session_factory

    async def resolve(self, api_key: str | None) -> ConsumerIdentity | None:
        settings = get_settings()
        if not settings.auth.enabled:
            # Defense-in-depth: validate_production_lockdown already refuses to boot with
            # auth disabled in a locked-down env, but never hand out anonymous admin there
            # even if that guard is somehow bypassed (e.g. settings constructed directly).
            if settings.is_locked_down:
                logger.error(
                    "apex.auth.disabled_in_locked_down_env",
                    environment=settings.environment,
                )
                return None
            return _anonymous_identity()
        if not api_key:
            return None
        dev_key = settings.auth.dev_api_key
        if dev_key and secrets.compare_digest(api_key, dev_key):
            return _dev_identity()
        try:
            return await self._resolve_from_db(api_key)
        except InvalidConsumerIdentityError:
            # Treat malformed legacy rows exactly like a disabled credential. A
            # bad scope/role must never become either an authorization bypass or
            # a credential-shaped 500/503 response.
            logger.warning("apex.auth.invalid_consumer_identity")
            return None
        except Exception as exc:
            logger.warning(
                "apex.auth.db_lookup_failed",
                error_type=exc.__class__.__name__,
            )
            raise AuthStoreUnavailableError("API key store is unavailable") from exc

    async def _resolve_from_db(self, api_key: str) -> ConsumerIdentity | None:
        session_factory = self._session_factory
        if session_factory is None:
            from apex.persistence.db import get_sessionmaker

            session_factory = get_sessionmaker()
        current_hash = hash_api_key(api_key)
        key_hashes = _candidate_key_hashes(api_key)
        async with session_factory() as session:
            credential = await session.scalar(
                select(ConsumerKey)
                .options(
                    selectinload(ConsumerKey.consumer).selectinload(ApiConsumer.scopes),
                    selectinload(ConsumerKey.consumer).selectinload(ApiConsumer.keys),
                )
                .where(
                    ConsumerKey.key_hash.in_(key_hashes),
                    ConsumerKey.revoked_at.is_(None),
                )
            )
            if isinstance(credential, ConsumerKey):
                return await self._identity_from_key(
                    session=session,
                    key=credential,
                    key_hashes=key_hashes,
                    current_hash=current_hash,
                )

            consumer = credential if isinstance(credential, ApiConsumer) else None
            if consumer is None:
                consumer = await session.scalar(
                    select(ApiConsumer)
                    .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
                    .where(
                        ApiConsumer.key_hash.in_(key_hashes),
                        ApiConsumer.enabled.is_(True),
                        ApiConsumer.revoked_at.is_(None),
                        ApiConsumer.deleted_at.is_(None),
                    )
                )
            return await self._identity_from_legacy_consumer(
                session=session,
                consumer=consumer,
                key_hashes=key_hashes,
                current_hash=current_hash,
            )

    async def _identity_from_key(
        self,
        *,
        session: Any,
        key: ConsumerKey,
        key_hashes: tuple[str, ...],
        current_hash: str,
    ) -> ConsumerIdentity | None:
        if not any(secrets.compare_digest(key.key_hash, value) for value in key_hashes):
            return None
        consumer = key.consumer
        now = datetime.now(UTC)
        if not _consumer_is_active(consumer, now):
            return None
        if key.revoked_at is not None:
            return None
        if key.expires_at is not None and key.expires_at <= now:
            return None
        identity = _identity_from_consumer(consumer)
        should_rehash_key = get_settings().auth.api_key_hash_pepper is not None and not (
            secrets.compare_digest(key.key_hash, current_hash)
        )
        should_touch_key = (
            key.last_used_at is None or now - key.last_used_at > LAST_USED_WRITE_INTERVAL
        )
        should_touch_consumer = (
            consumer.last_used_at is None or now - consumer.last_used_at > LAST_USED_WRITE_INTERVAL
        )
        should_sync_consumer_hash = not _active_key_hashes_include(consumer, consumer.key_hash, now)
        if not (
            should_rehash_key
            or should_sync_consumer_hash
            or should_touch_key
            or should_touch_consumer
        ):
            return identity

        # Metadata repairs must never be applied from the stale objects loaded by
        # the optimistic lookup above. Credential rotation locks this same
        # aggregate; taking that lock and re-reading lets either authentication or
        # rotation linearize first without authentication resurrecting the old key.
        locked_consumer = await _lock_consumer_for_auth(session, consumer.id)
        if locked_consumer is None:
            return None
        locked_key = next(
            (
                candidate
                for candidate in locked_consumer.keys
                if candidate.id == key.id
                and any(
                    secrets.compare_digest(candidate.key_hash, candidate_hash)
                    for candidate_hash in key_hashes
                )
            ),
            None,
        )
        locked_now = datetime.now(UTC)
        if (
            locked_key is None
            or not _consumer_is_active(locked_consumer, locked_now)
            or locked_key.revoked_at is not None
            or (locked_key.expires_at is not None and locked_key.expires_at <= locked_now)
        ):
            return None

        locked_identity = _identity_from_consumer(locked_consumer)
        old_key_hash = locked_key.key_hash
        should_rehash_key = get_settings().auth.api_key_hash_pepper is not None and not (
            secrets.compare_digest(old_key_hash, current_hash)
        )
        parent_tracked_authenticated_key = secrets.compare_digest(
            locked_consumer.key_hash, old_key_hash
        )
        should_sync_consumer_hash = not _active_key_hashes_include(
            locked_consumer, locked_consumer.key_hash, locked_now
        )
        if should_rehash_key:
            locked_key.key_hash = current_hash
        # Preserve a newer rotation pointer when the authenticated old key is
        # still valid only for a grace window.
        if parent_tracked_authenticated_key or should_sync_consumer_hash:
            locked_consumer.key_hash = locked_key.key_hash
        if locked_key.last_used_at is None or (
            locked_now - locked_key.last_used_at > LAST_USED_WRITE_INTERVAL
        ):
            locked_key.last_used_at = locked_now
        if locked_consumer.last_used_at is None or (
            locked_now - locked_consumer.last_used_at > LAST_USED_WRITE_INTERVAL
        ):
            locked_consumer.last_used_at = locked_now
        try:
            await session.commit()
        except Exception as exc:
            await _best_effort_rollback(session)
            logger.warning(
                "apex.auth.key_metadata_update_failed",
                consumer=locked_consumer.name,
                error_type=exc.__class__.__name__,
            )
        return locked_identity

    async def _identity_from_legacy_consumer(
        self,
        *,
        session: Any,
        consumer: ApiConsumer | None,
        key_hashes: tuple[str, ...],
        current_hash: str,
    ) -> ConsumerIdentity | None:
        if consumer is None:
            return None
        if not any(secrets.compare_digest(consumer.key_hash, value) for value in key_hashes):
            return None
        now = datetime.now(UTC)
        if not _consumer_is_active(consumer, now):
            return None
        # Once a credential has an explicit row, that row is authoritative even
        # when revoked or expired. Falling back to the denormalized parent hash
        # would otherwise turn an explicit revocation into successful legacy auth.
        if _consumer_keys_include_any_hash(consumer, key_hashes):
            return None

        locked_consumer = await _lock_consumer_for_auth(session, consumer.id)
        locked_now = datetime.now(UTC)
        if (
            locked_consumer is None
            or not _consumer_is_active(locked_consumer, locked_now)
            or not any(
                secrets.compare_digest(locked_consumer.key_hash, value) for value in key_hashes
            )
            or _consumer_keys_include_any_hash(locked_consumer, key_hashes)
        ):
            return None

        identity = _identity_from_consumer(locked_consumer)
        should_rehash_key = get_settings().auth.api_key_hash_pepper is not None and not (
            secrets.compare_digest(locked_consumer.key_hash, current_hash)
        )
        target_hash = current_hash if should_rehash_key else locked_consumer.key_hash
        if should_rehash_key:
            locked_consumer.key_hash = current_hash
        locked_consumer.keys.append(
            ConsumerKey(
                key_hash=target_hash,
                expiry_source="independent",
                created_by=locked_consumer.created_by,
            )
        )
        try:
            locked_consumer.last_used_at = locked_now
            await session.commit()
        except Exception as exc:
            await _best_effort_rollback(session)
            logger.warning(
                "apex.auth.last_used_update_failed",
                consumer=locked_consumer.name,
                error_type=exc.__class__.__name__,
            )
        return identity


async def _lock_consumer_for_auth(session: Any, consumer_id: str) -> ApiConsumer | None:
    consumer = await session.scalar(
        select(ApiConsumer)
        .where(ApiConsumer.id == consumer_id)
        .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return consumer if isinstance(consumer, ApiConsumer) else None


async def _best_effort_rollback(session: Any) -> None:
    try:
        await session.rollback()
    except Exception as exc:
        logger.warning(
            "apex.auth.metadata_rollback_failed",
            error_type=exc.__class__.__name__,
        )


def _consumer_is_active(consumer: ApiConsumer, now: datetime) -> bool:
    if not consumer.enabled:
        return False
    if consumer.revoked_at is not None or consumer.deleted_at is not None:
        return False
    return not (consumer.expires_at is not None and consumer.expires_at <= now)


def _active_key_hashes_include(consumer: ApiConsumer, key_hash: str, now: datetime) -> bool:
    return any(
        secrets.compare_digest(key.key_hash, key_hash)
        for key in consumer.keys
        if key.revoked_at is None and (key.expires_at is None or key.expires_at > now)
    )


def _consumer_keys_include_any_hash(
    consumer: ApiConsumer, candidate_hashes: tuple[str, ...]
) -> bool:
    return any(
        secrets.compare_digest(key.key_hash, candidate_hash)
        for key in consumer.keys
        for candidate_hash in candidate_hashes
    )


def _identity_from_consumer(consumer: ApiConsumer) -> ConsumerIdentity:
    for scope in consumer.scopes:
        raw_values = (scope.project_id, scope.app_id)
        if any(value is not None and (not value or value != value.strip()) for value in raw_values):
            # Request models normalize whitespace before persistence, but legacy
            # rows must not be normalized while authenticating: silently turning
            # ``" project-a "`` into ``"project-a"`` could widen its authority.
            raise InvalidConsumerIdentityError(
                "persisted consumer scope contains non-canonical identifiers"
            )
    scope_keys = [(scope.project_id, scope.app_id) for scope in consumer.scopes]
    if len(scope_keys) != len(set(scope_keys)):
        raise InvalidConsumerIdentityError("persisted consumer has duplicate scopes")
    try:
        return ConsumerIdentity(
            consumer_id=consumer.id,
            name=consumer.name,
            consumer_type=ConsumerType(consumer.consumer_type),
            role=Role(consumer.role),
            scopes=[
                ScopeRef(project_id=scope.project_id, app_id=scope.app_id)
                for scope in consumer.scopes
            ],
        )
    except (ValidationError, ValueError) as exc:
        raise InvalidConsumerIdentityError("persisted consumer identity is invalid") from exc


default_resolver = IdentityResolver()


def get_default_resolver() -> IdentityResolver:
    """Module-level resolver used by both surfaces; swap `default_resolver` in tests."""
    return default_resolver
