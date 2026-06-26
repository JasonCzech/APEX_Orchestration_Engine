import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apex.auth.identity import Role
from apex.auth.service import (
    AuthStoreUnavailableError,
    IdentityResolver,
    extract_api_key,
    hash_api_key,
    legacy_hash_api_key,
)
from apex.persistence.models import ApiConsumer, ConsumerKey, ConsumerScope

DEV_KEY = "dev-key-123"


class ExplodingFactory:
    """Session factory standing in for an unreachable database."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise ConnectionError("postgres is down")


class FakeSession:
    def __init__(self, consumer: ApiConsumer | None) -> None:
        self._consumer = consumer
        self.committed = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def scalar(self, _stmt: Any) -> ApiConsumer | None:
        return self._consumer

    async def commit(self) -> None:
        self.committed = True


# ── extract_api_key ──────────────────────────────────────────────────────────


def test_extract_api_key_from_str_headers_case_insensitive() -> None:
    assert extract_api_key({"X-Api-Key": "abc"}) == "abc"


def test_extract_api_key_from_bytes_headers() -> None:
    assert extract_api_key({b"x-api-key": b"abc"}) == "abc"


def test_extract_api_key_bearer_fallback() -> None:
    assert extract_api_key({b"authorization": b"Bearer tok123"}) == "tok123"
    assert extract_api_key({"Authorization": "bearer tok123"}) == "tok123"


def test_extract_api_key_prefers_x_api_key() -> None:
    headers = {"x-api-key": "primary", "authorization": "Bearer secondary"}
    assert extract_api_key(headers) == "primary"


def test_extract_api_key_none_for_missing_or_non_bearer() -> None:
    assert extract_api_key({}) is None
    assert extract_api_key({"authorization": "Basic dXNlcg=="}) is None


def test_extract_api_key_rejects_duplicate_auth_headers() -> None:
    assert extract_api_key([(b"x-api-key", b"first"), (b"x-api-key", b"second")]) is None
    assert (
        extract_api_key([(b"authorization", b"Bearer first"), (b"authorization", b"Bearer second")])
        is None
    )


def test_extract_api_key_rejects_non_utf8_header_bytes() -> None:
    assert extract_api_key({b"x-api-key": b"\xff"}) is None


def test_hash_api_key_uses_peppered_hmac_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "server-pepper")
    expected = hmac.new(
        b"server-pepper",
        b"some-key",
        hashlib.sha256,
    ).hexdigest()

    assert hash_api_key("some-key") == expected
    assert hash_api_key("some-key") != legacy_hash_api_key("some-key")


# ── IdentityResolver ─────────────────────────────────────────────────────────


async def test_dev_key_resolves_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()
    resolver = IdentityResolver(session_factory=factory)
    identity = await resolver.resolve(DEV_KEY)
    assert identity is not None
    assert identity.name == "dev"
    assert identity.role is Role.ADMIN
    assert identity.is_unscoped
    assert factory.calls == 0


async def test_missing_key_resolves_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    resolver = IdentityResolver(session_factory=ExplodingFactory())
    assert await resolver.resolve(None) is None
    assert await resolver.resolve("") is None


async def test_db_errors_surface_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()
    resolver = IdentityResolver(session_factory=factory)
    with pytest.raises(AuthStoreUnavailableError):
        await resolver.resolve("not-the-dev-key")
    assert factory.calls == 1


async def test_auth_disabled_yields_anonymous_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__ENABLED", "false")
    resolver = IdentityResolver(session_factory=ExplodingFactory())
    identity = await resolver.resolve(None)
    assert identity is not None
    assert identity.name == "anonymous"
    assert identity.role is Role.ADMIN
    assert identity.is_unscoped


async def test_auth_disabled_in_locked_down_env_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defense-in-depth: even if a settings object with auth disabled slips past the
    # production lockdown validator, resolve() must never grant anonymous admin there.
    import apex.auth.service as svc
    from apex.settings import ApexSettings, AuthSettings

    unsafe = ApexSettings.model_construct(
        environment="production", auth=AuthSettings(enabled=False)
    )
    monkeypatch.setattr(svc, "get_settings", lambda: unsafe)
    resolver = IdentityResolver(session_factory=ExplodingFactory())
    assert await resolver.resolve(None) is None


async def test_db_lookup_builds_identity_with_scopes() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = [
        ConsumerScope(id="s1", consumer_id="abc123", project_id="p1", app_id=None),
        ConsumerScope(id="s2", consumer_id="abc123", project_id="p2", app_id="a1"),
    ]
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)
    identity = await resolver.resolve("some-key")
    assert identity is not None
    assert identity.consumer_id == "abc123"
    assert identity.role is Role.OPERATOR
    assert identity.scoped_project_ids() == ("p1", "p2")
    assert identity.scopes[1].app_id == "a1"
    # best-effort last_used_at update committed
    assert session.committed
    assert consumer.last_used_at is not None


async def test_db_lookup_resolves_consumer_key_and_updates_key_usage() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = [ConsumerScope(id="s1", consumer_id="abc123", project_id="p1", app_id=None)]
    key = ConsumerKey(id="k1", consumer_id="abc123", key_hash=hash_api_key("some-key"))
    key.consumer = consumer
    session = FakeSession(key)
    resolver = IdentityResolver(session_factory=lambda: session)

    identity = await resolver.resolve("some-key")

    assert identity is not None
    assert identity.consumer_id == "abc123"
    assert key.last_used_at is not None
    assert consumer.last_used_at is not None
    assert session.committed


async def test_db_lookup_rejects_expired_consumer_key() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = []
    key = ConsumerKey(
        id="k1",
        consumer_id="abc123",
        key_hash=hash_api_key("some-key"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    key.consumer = consumer
    session = FakeSession(key)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("some-key") is None
    assert not session.committed


async def test_db_lookup_rehashes_legacy_consumer_key_with_configured_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "server-pepper")
    legacy_hash = legacy_hash_api_key("some-key")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=legacy_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    key = ConsumerKey(
        id="k1",
        consumer_id="abc123",
        key_hash=legacy_hash,
        last_used_at=datetime.now(UTC),
    )
    key.consumer = consumer
    session = FakeSession(key)
    resolver = IdentityResolver(session_factory=lambda: session)

    identity = await resolver.resolve("some-key")

    assert identity is not None
    assert key.key_hash == hash_api_key("some-key")
    assert consumer.key_hash == hash_api_key("some-key")
    assert session.committed


async def test_db_lookup_throttles_last_used_at_write() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)
    identity = await resolver.resolve("some-key")
    assert identity is not None
    assert not session.committed


async def test_db_lookup_unknown_key_returns_none() -> None:
    resolver = IdentityResolver(session_factory=lambda: FakeSession(None))
    assert await resolver.resolve("unknown-key") is None


async def test_db_lookup_rejects_mismatched_hash_from_store() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=legacy_hash_api_key("different-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = []
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("some-key") is None
    assert not session.committed


async def test_db_lookup_rehashes_legacy_sha256_with_configured_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "server-pepper")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=legacy_hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    identity = await resolver.resolve("some-key")

    assert identity is not None
    assert consumer.key_hash == hash_api_key("some-key")
    assert consumer.key_hash != legacy_hash_api_key("some-key")
    assert session.committed


async def test_db_lookup_rejects_expired_consumer() -> None:
    consumer = ApiConsumer(
        id="abc123",
        name="old-bot",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    consumer.scopes = []
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("some-key") is None
    assert not session.committed


async def test_db_lookup_rejects_revoked_or_deleted_consumer() -> None:
    for field in ("revoked_at", "deleted_at"):
        consumer = ApiConsumer(
            id=f"abc-{field}",
            name=f"{field}-bot",
            key_hash=hash_api_key("some-key"),
            consumer_type="headless",
            role="operator",
            enabled=True,
        )
        setattr(consumer, field, datetime.now(UTC))
        consumer.scopes = []
        resolver = IdentityResolver(session_factory=lambda consumer=consumer: FakeSession(consumer))

        assert await resolver.resolve("some-key") is None
