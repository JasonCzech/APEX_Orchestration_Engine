"""API-key -> ConsumerIdentity resolution, shared by the LangGraph and /v1 surfaces."""

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import ApiConsumer
from apex.settings import get_settings

logger = structlog.get_logger(__name__)
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


class AuthStoreUnavailableError(RuntimeError):
    """The API-key backing store could not be queried."""


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


HeaderInput = Mapping[Any, Any] | Iterable[tuple[Any, Any]]


def extract_api_key(headers: HeaderInput) -> str | None:
    """Pull the key from `x-api-key` or a bearer `Authorization` header.

    Accepts str- or bytes-keyed mappings (starlette Headers, raw ASGI header dicts).
    """
    try:
        api_key = _get_unique_header(headers, "x-api-key")
        authorization = _get_unique_header(headers, "authorization")
    except ValueError:
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
            return _anonymous_identity()
        if not api_key:
            return None
        dev_key = settings.auth.dev_api_key
        if dev_key and secrets.compare_digest(api_key, dev_key):
            return _dev_identity()
        try:
            return await self._resolve_from_db(api_key)
        except Exception as exc:
            logger.warning("apex.auth.db_lookup_failed", exc_info=True)
            raise AuthStoreUnavailableError("API key store is unavailable") from exc

    async def _resolve_from_db(self, api_key: str) -> ConsumerIdentity | None:
        session_factory = self._session_factory
        if session_factory is None:
            from apex.persistence.db import get_sessionmaker

            session_factory = get_sessionmaker()
        key_hash = hash_api_key(api_key)
        async with session_factory() as session:
            consumer = await session.scalar(
                select(ApiConsumer).where(
                    ApiConsumer.key_hash == key_hash, ApiConsumer.enabled.is_(True)
                )
            )
            if consumer is None:
                return None
            identity = ConsumerIdentity(
                consumer_id=consumer.id,
                name=consumer.name,
                consumer_type=ConsumerType(consumer.consumer_type),
                role=Role(consumer.role),
                scopes=[
                    ScopeRef(project_id=scope.project_id, app_id=scope.app_id)
                    for scope in consumer.scopes
                ],
            )
            now = datetime.now(UTC)
            should_touch_last_used = (
                consumer.last_used_at is None
                or now - consumer.last_used_at > LAST_USED_WRITE_INTERVAL
            )
            if should_touch_last_used:
                try:
                    consumer.last_used_at = now
                    await session.commit()
                except Exception:
                    logger.warning(
                        "apex.auth.last_used_update_failed", consumer=consumer.name, exc_info=True
                    )
            return identity


default_resolver = IdentityResolver()


def get_default_resolver() -> IdentityResolver:
    """Module-level resolver used by both surfaces; swap `default_resolver` in tests."""
    return default_resolver
