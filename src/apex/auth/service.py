"""API-key -> ConsumerIdentity resolution, shared by the LangGraph and /v1 surfaces."""

import hashlib
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import ApiConsumer
from apex.settings import get_settings

logger = structlog.get_logger(__name__)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def extract_api_key(headers: Mapping[Any, Any]) -> str | None:
    """Pull the key from `x-api-key` or a bearer `Authorization` header.

    Accepts str- or bytes-keyed mappings (starlette Headers, raw ASGI header dicts).
    """
    api_key = _get_header(headers, "x-api-key")
    if api_key:
        return api_key
    authorization = _get_header(headers, "authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return None


def _get_header(headers: Mapping[Any, Any], name: str) -> str | None:
    for key, value in headers.items():
        key_str = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        if key_str.lower() == name:
            return value.decode("latin-1") if isinstance(value, bytes) else str(value)
    return None


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
    otherwise `apex.api_consumers` lookup by sha256 hash. DB errors are swallowed
    (logged) so the dev key keeps working while Postgres is down.
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
        except Exception:
            logger.warning("apex.auth.db_lookup_failed", exc_info=True)
            return None

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
            try:
                consumer.last_used_at = datetime.now(UTC)
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
