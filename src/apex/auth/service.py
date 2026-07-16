"""API-key -> ConsumerIdentity resolution, shared by the LangGraph and /v1 surfaces."""

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.diagnostics import safe_type_name
from apex.persistence.models import ApiConsumer, ConsumerKey
from apex.services.connection_credentials import (
    reject_credential_text,
    sanitize_credential_text_for_output,
)
from apex.settings import (
    MAX_API_KEY_SECRET_BYTES,
    MAX_API_KEY_SECRET_CHARS,
    get_settings,
)

logger = structlog.get_logger(__name__)
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


class AuthStoreUnavailableError(RuntimeError):
    """The API-key backing store could not be queried."""


class InvalidConsumerIdentityError(ValueError):
    """A persisted credential cannot be represented as a safe identity."""


@dataclass(frozen=True)
class _CredentialMatch:
    """One unambiguous active consumer and the credential path that matched it."""

    consumer: ApiConsumer
    key: ConsumerKey | None
    current_consumer_hash_occupied: bool
    current_key_hash_occupied: bool
    legacy_key_hash_occupied: bool


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
_MAX_HTTP_HEADER_NAME_BYTES = 128
_MAX_AUTHORIZATION_HEADER_CHARS = MAX_API_KEY_SECRET_CHARS + 16
_MAX_AUTHORIZATION_HEADER_BYTES = MAX_API_KEY_SECRET_BYTES + 16


def extract_api_key(headers: HeaderInput) -> str | None:
    """Pull the key from `x-api-key` or a bearer `Authorization` header.

    Accepts str- or bytes-keyed mappings (starlette Headers, raw ASGI header dicts).
    """
    try:
        api_key = _get_unique_header(
            headers,
            "x-api-key",
            max_chars=MAX_API_KEY_SECRET_CHARS,
            max_bytes=MAX_API_KEY_SECRET_BYTES,
        )
        authorization = _get_unique_header(
            headers,
            "authorization",
            max_chars=_MAX_AUTHORIZATION_HEADER_CHARS,
            max_bytes=_MAX_AUTHORIZATION_HEADER_BYTES,
        )
        if api_key is not None and authorization is not None:
            raise ValueError("x-api-key and authorization cannot be combined")
    except (TypeError, UnicodeError, ValueError):
        return None
    if api_key:
        return api_key if _api_key_input_is_bounded(api_key) else None
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            candidate = token.strip()
            return candidate if _api_key_input_is_bounded(candidate) else None
    return None


def _get_unique_header(
    headers: HeaderInput,
    name: str,
    *,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    matches: list[str] = []
    for key, value in _iter_headers(headers):
        key_str = _decode_header_name(key)
        if key_str == name:
            matches.append(
                _decode_bounded_header_value(
                    value,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                )
            )
    if len(matches) > 1:
        raise ValueError(f"duplicate {name} headers are not allowed")
    return matches[0] if matches else None


def _iter_headers(headers: HeaderInput) -> Iterable[tuple[Any, Any]]:
    if isinstance(headers, Mapping):
        return headers.items()
    return headers


def _decode_header_name(value: Any) -> str | None:
    """Return a small ASCII name without invoking caller-defined coercion hooks."""

    if type(value) is bytes:
        if not 1 <= len(value) <= _MAX_HTTP_HEADER_NAME_BYTES:
            return None
        try:
            return value.decode("ascii", errors="strict").lower()
        except UnicodeDecodeError:
            return None
    if type(value) is str:
        if not 1 <= len(value) <= _MAX_HTTP_HEADER_NAME_BYTES:
            return None
        try:
            if len(value.encode("ascii", errors="strict")) > _MAX_HTTP_HEADER_NAME_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        return value.lower()
    return None


def _decode_bounded_header_value(value: Any, *, max_chars: int, max_bytes: int) -> str:
    """Decode only exact built-ins, applying byte limits before bytes decoding."""

    if type(value) is bytes:
        if not 1 <= len(value) <= max_bytes:
            raise ValueError("credential header is empty or oversized")
        decoded = value.decode("utf-8", errors="strict")
        if len(decoded) > max_chars:
            raise ValueError("credential header is oversized")
        return decoded
    if type(value) is str:
        if not 1 <= len(value) <= max_chars:
            raise ValueError("credential header is empty or oversized")
        if len(value.encode("utf-8", errors="strict")) > max_bytes:
            raise ValueError("credential header is oversized")
        return value
    raise TypeError("credential header must be text or bytes")


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


def _api_key_input_is_bounded(value: Any) -> bool:
    """Reject malformed or oversized caller credentials before compare/hash work."""

    if type(value) is not str or not 1 <= len(value) <= MAX_API_KEY_SECRET_CHARS:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_API_KEY_SECRET_BYTES
    except UnicodeEncodeError:
        return False


class IdentityResolver:
    """Resolves API keys to consumer identities.

    Order: auth disabled -> anonymous admin; otherwise enumerate every supported
    key-hash generation in the authoritative store. A dev-key match becomes the
    synthetic admin only after that lookup (or an injected verified collision
    guard) proves no active persisted credential owns the plaintext. DB errors
    surface as AuthStoreUnavailableError so callers return 503 instead of a
    credential-shaped 401.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        dev_key_collision_check: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        # Defaults to apex.persistence.db.get_sessionmaker() at resolve time; injectable
        # so tests never touch a real database.
        self._session_factory = session_factory
        # A deployment may inject a startup-verified collision guard for an
        # intentionally database-free local dev shortcut. Without one, matching
        # the dev key probes the authoritative store and fails closed if it is down.
        self._dev_key_collision_check = dev_key_collision_check

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
        if not _api_key_input_is_bounded(api_key):
            return None
        assert type(api_key) is str
        dev_key = settings.auth.dev_api_key
        dev_key_matches = bool(dev_key and secrets.compare_digest(api_key, dev_key))
        try:
            if dev_key_matches and self._dev_key_collision_check is not None:
                if await self._dev_key_collision_check(api_key):
                    raise InvalidConsumerIdentityError(
                        "dev API key collides with a persisted credential"
                    )
                return _dev_identity()
            identity = await self._resolve_from_db(
                api_key,
                reject_persisted_match=dev_key_matches,
            )
            return _dev_identity() if dev_key_matches else identity
        except InvalidConsumerIdentityError:
            # Treat malformed legacy rows exactly like a disabled credential. A
            # bad scope/role must never become either an authorization bypass or
            # a credential-shaped 500/503 response.
            logger.warning("apex.auth.invalid_consumer_identity")
            return None
        except Exception as exc:
            logger.warning(
                "apex.auth.db_lookup_failed",
                error_type=safe_type_name(exc),
            )
        # Raise after leaving the raw driver handler. A chained or contextual
        # exception can retain DSNs, endpoints, and credential values even when
        # its outer message is stable.
        raise AuthStoreUnavailableError("API key store is unavailable")

    async def _resolve_from_db(
        self,
        api_key: str,
        *,
        reject_persisted_match: bool = False,
    ) -> ConsumerIdentity | None:
        session_factory = self._session_factory
        if session_factory is None:
            from apex.persistence.db import get_sessionmaker

            session_factory = get_sessionmaker()
        current_hash = hash_api_key(api_key)
        key_hashes = _candidate_key_hashes(api_key)
        async with session_factory() as session:
            match, persisted_candidate_occupied = await _active_credential_match(
                session,
                key_hashes=key_hashes,
                current_hash=current_hash,
            )
            if reject_persisted_match and persisted_candidate_occupied:
                raise InvalidConsumerIdentityError(
                    "dev API key collides with a persisted credential"
                )
            if match is None:
                return None
            if match.key is not None:
                return await self._identity_from_key(
                    session=session,
                    key=match.key,
                    consumer=match.consumer,
                    key_hashes=key_hashes,
                    current_hash=current_hash,
                    current_consumer_hash_occupied=(match.current_consumer_hash_occupied),
                    current_key_hash_occupied=match.current_key_hash_occupied,
                )
            return await self._identity_from_legacy_consumer(
                session=session,
                consumer=match.consumer,
                key_hashes=key_hashes,
                current_hash=current_hash,
                current_consumer_hash_occupied=match.current_consumer_hash_occupied,
                current_key_hash_occupied=match.current_key_hash_occupied,
                legacy_key_hash_occupied=match.legacy_key_hash_occupied,
            )

    async def _identity_from_key(
        self,
        *,
        session: Any,
        key: ConsumerKey,
        consumer: ApiConsumer | None = None,
        key_hashes: tuple[str, ...],
        current_hash: str,
        current_consumer_hash_occupied: bool,
        current_key_hash_occupied: bool,
    ) -> ConsumerIdentity | None:
        if not any(secrets.compare_digest(key.key_hash, value) for value in key_hashes):
            return None
        consumer = consumer or key.consumer
        now = datetime.now(UTC)
        if not _consumer_is_active(consumer, now):
            return None
        if key.revoked_at is not None:
            return None
        if key.expires_at is not None and key.expires_at <= now:
            return None
        identity = _identity_from_consumer(consumer)
        should_rehash_key = (
            get_settings().auth.api_key_hash_pepper is not None
            and not secrets.compare_digest(key.key_hash, current_hash)
            and not current_key_hash_occupied
        )
        should_touch_key = (
            key.last_used_at is None or now - key.last_used_at > LAST_USED_WRITE_INTERVAL
        )
        should_touch_consumer = (
            consumer.last_used_at is None or now - consumer.last_used_at > LAST_USED_WRITE_INTERVAL
        )
        prospective_key_hash = current_hash if should_rehash_key else key.key_hash
        parent_sync_blocked = (
            current_consumer_hash_occupied
            and secrets.compare_digest(prospective_key_hash, current_hash)
            and not secrets.compare_digest(consumer.key_hash, current_hash)
        )
        should_sync_consumer_hash = (
            not _active_key_hashes_include(consumer, consumer.key_hash, now)
            and not parent_sync_blocked
        )
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
        should_rehash_key = (
            get_settings().auth.api_key_hash_pepper is not None
            and not secrets.compare_digest(old_key_hash, current_hash)
            and not current_key_hash_occupied
            and not any(
                candidate.id != locked_key.id
                and secrets.compare_digest(candidate.key_hash, current_hash)
                for candidate in locked_consumer.keys
            )
        )
        parent_tracked_authenticated_key = secrets.compare_digest(
            locked_consumer.key_hash, old_key_hash
        )
        prospective_key_hash = current_hash if should_rehash_key else old_key_hash
        parent_sync_blocked = (
            current_consumer_hash_occupied
            and secrets.compare_digest(prospective_key_hash, current_hash)
            and not secrets.compare_digest(locked_consumer.key_hash, current_hash)
        )
        should_sync_consumer_hash = (
            not _active_key_hashes_include(locked_consumer, locked_consumer.key_hash, locked_now)
            and not parent_sync_blocked
        )
        if should_rehash_key:
            locked_key.key_hash = current_hash
        # Preserve a newer rotation pointer when the authenticated old key is
        # still valid only for a grace window.
        if (
            parent_tracked_authenticated_key or should_sync_consumer_hash
        ) and not parent_sync_blocked:
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
                consumer_id=locked_consumer.id,
                error_type=safe_type_name(exc),
            )
        return locked_identity

    async def _identity_from_legacy_consumer(
        self,
        *,
        session: Any,
        consumer: ApiConsumer | None,
        key_hashes: tuple[str, ...],
        current_hash: str,
        current_consumer_hash_occupied: bool,
        current_key_hash_occupied: bool,
        legacy_key_hash_occupied: bool,
    ) -> ConsumerIdentity | None:
        if consumer is None:
            return None
        _validate_persisted_consumer_id(consumer.id)
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
        wants_current_hash = (
            get_settings().auth.api_key_hash_pepper is not None
            and not secrets.compare_digest(locked_consumer.key_hash, current_hash)
        )
        should_rehash_key = wants_current_hash and not current_key_hash_occupied
        should_rehash_consumer = wants_current_hash and not current_consumer_hash_occupied
        target_hash = current_hash if should_rehash_key else locked_consumer.key_hash
        target_key_hash_occupied = (
            current_key_hash_occupied
            if secrets.compare_digest(target_hash, current_hash)
            else legacy_key_hash_occupied
        )
        if not should_rehash_consumer and target_key_hash_occupied:
            # A revoked/expired row may legitimately reserve this globally
            # unique ConsumerKey digest while another parent may reserve the
            # current parent digest. Keep legacy authentication read-only
            # instead of retrying doomed repairs on every request.
            return identity
        if should_rehash_consumer:
            locked_consumer.key_hash = current_hash
        if not target_key_hash_occupied:
            locked_consumer.keys.append(
                ConsumerKey(
                    key_hash=target_hash,
                    expiry_source="independent",
                    created_by=_safe_legacy_actor(locked_consumer.created_by),
                )
            )
        try:
            locked_consumer.last_used_at = locked_now
            await session.commit()
        except Exception as exc:
            await _best_effort_rollback(session)
            logger.warning(
                "apex.auth.last_used_update_failed",
                consumer_id=locked_consumer.id,
                error_type=safe_type_name(exc),
            )
        return identity


async def _active_credential_match(
    session: Any,
    *,
    key_hashes: tuple[str, ...],
    current_hash: str,
) -> tuple[_CredentialMatch | None, bool]:
    """Resolve one active identity and report any persisted hash occupancy.

    A single plaintext API key has a different digest under the current pepper,
    each previous pepper, and the legacy unpeppered scheme. Database uniqueness
    applies to each digest separately, so selecting the first matching row can
    silently choose the wrong tenant when two consumers reuse one plaintext.
    Load the complete bounded candidate set, collapse multiple generations for
    one consumer, and reject more than one distinct active consumer. The
    occupancy result deliberately includes disabled, deleted, revoked, and
    expired rows so none can alias the synthetic unscoped development admin.
    """

    result = await session.scalars(
        select(ApiConsumer)
        .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
        .where(
            or_(
                ApiConsumer.key_hash.in_(key_hashes),
                ApiConsumer.keys.any(ConsumerKey.key_hash.in_(key_hashes)),
            )
        )
    )
    consumers = [consumer for consumer in result if isinstance(consumer, ApiConsumer)]
    persisted_candidate_occupied = bool(consumers)
    now = datetime.now(UTC)
    current_consumer_hash_occupied = any(
        secrets.compare_digest(consumer.key_hash, current_hash) for consumer in consumers
    )
    current_key_hash_occupied = any(
        secrets.compare_digest(key.key_hash, current_hash)
        for consumer in consumers
        for key in consumer.keys
    )
    matches: dict[str, _CredentialMatch] = {}
    for consumer in consumers:
        if not _consumer_is_active(consumer, now):
            continue

        explicit_matches = [key for key in consumer.keys if _hash_matches(key.key_hash, key_hashes)]
        active_keys = [
            key
            for key in explicit_matches
            if key.revoked_at is None and (key.expires_at is None or key.expires_at > now)
        ]
        legacy_match = _hash_matches(consumer.key_hash, key_hashes) and not explicit_matches
        if not active_keys and not legacy_match:
            continue

        consumer_id = _validate_persisted_consumer_id(consumer.id)
        preferred_key = (
            _preferred_matching_key(
                active_keys,
                current_hash=current_hash,
                key_hashes=key_hashes,
            )
            if active_keys
            else None
        )
        existing = matches.get(consumer_id)
        if existing is None or (existing.key is None and preferred_key is not None):
            legacy_key_hash_occupied = legacy_match and any(
                any(
                    secrets.compare_digest(key.key_hash, consumer.key_hash)
                    for key in candidate.keys
                )
                for candidate in consumers
            )
            matches[consumer_id] = _CredentialMatch(
                consumer=consumer,
                key=preferred_key,
                current_consumer_hash_occupied=current_consumer_hash_occupied,
                current_key_hash_occupied=current_key_hash_occupied,
                legacy_key_hash_occupied=legacy_key_hash_occupied,
            )

    if len(matches) > 1:
        raise InvalidConsumerIdentityError(
            "API key matches more than one active persisted consumer"
        )
    return next(iter(matches.values()), None), persisted_candidate_occupied


def _hash_matches(value: Any, candidate_hashes: tuple[str, ...]) -> bool:
    """Compare persisted credential digests without early-exit string equality."""

    return type(value) is str and any(
        secrets.compare_digest(value, candidate_hash) for candidate_hash in candidate_hashes
    )


def _preferred_matching_key(
    keys: list[ConsumerKey],
    *,
    current_hash: str,
    key_hashes: tuple[str, ...],
) -> ConsumerKey:
    """Prefer the current digest so lazy rehash never collides with that same row."""

    for candidate_hash in (current_hash, *key_hashes):
        for key in keys:
            if secrets.compare_digest(key.key_hash, candidate_hash):
                return key
    raise InvalidConsumerIdentityError("active credential candidate has an invalid hash")


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
            error_type=safe_type_name(exc),
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


def _safe_legacy_actor(value: Any) -> str | None:
    """Return only canonical actor metadata safe to copy into a new key row."""

    if (
        type(value) is not str
        or not value
        or len(value) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return None
    try:
        reject_credential_text(value, label="consumer actor")
    except ValueError:
        return None
    return value


def _validate_persisted_consumer_id(value: Any) -> str:
    """Validate an authorization identity key without reflecting legacy input."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 32
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise InvalidConsumerIdentityError("persisted consumer id is invalid")
    invalid_consumer_id = False
    try:
        reject_credential_text(value, label="consumer id")
    except ValueError:
        invalid_consumer_id = True
    if invalid_consumer_id:
        raise InvalidConsumerIdentityError("persisted consumer id is invalid")
    return value


def _identity_from_consumer(consumer: ApiConsumer) -> ConsumerIdentity:
    consumer_id = _validate_persisted_consumer_id(consumer.id)
    for scope in consumer.scopes:
        raw_values = (scope.project_id, scope.app_id)
        if any(value is not None and (not value or value != value.strip()) for value in raw_values):
            # Request models normalize whitespace before persistence, but legacy
            # rows must not be normalized while authenticating: silently turning
            # ``" project-a "`` into ``"project-a"`` could widen its authority.
            raise InvalidConsumerIdentityError(
                "persisted consumer scope contains non-canonical identifiers"
            )
        invalid_scope = False
        try:
            for value in raw_values:
                if value is not None:
                    reject_credential_text(value, label="consumer scope")
        except ValueError:
            invalid_scope = True
        if invalid_scope:
            raise InvalidConsumerIdentityError(
                "persisted consumer scope contains invalid identifiers"
            )
    scope_keys = [(scope.project_id, scope.app_id) for scope in consumer.scopes]
    if len(scope_keys) != len(set(scope_keys)):
        raise InvalidConsumerIdentityError("persisted consumer has duplicate scopes")
    identity: ConsumerIdentity | None = None
    try:
        safe_name = sanitize_credential_text_for_output(consumer.name)
        if (
            safe_name is None
            or not safe_name
            or len(safe_name) > 255
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in safe_name)
        ):
            safe_name = "[REDACTED]"
        identity = ConsumerIdentity(
            consumer_id=consumer_id,
            name=safe_name,
            consumer_type=ConsumerType(consumer.consumer_type),
            role=Role(consumer.role),
            scopes=[
                ScopeRef(project_id=scope.project_id, app_id=scope.app_id)
                for scope in consumer.scopes
            ],
        )
    except (ValidationError, ValueError):
        pass
    if identity is None:
        raise InvalidConsumerIdentityError("persisted consumer identity is invalid")
    return identity


default_resolver = IdentityResolver()


def get_default_resolver() -> IdentityResolver:
    """Module-level resolver used by both surfaces; swap `default_resolver` in tests."""
    return default_resolver
