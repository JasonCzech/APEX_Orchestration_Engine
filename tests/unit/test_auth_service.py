import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from apex.auth.handlers import user_payload
from apex.auth.identity import Role
from apex.auth.service import (
    AuthStoreUnavailableError,
    IdentityResolver,
    extract_api_key,
    hash_api_key,
    legacy_hash_api_key,
)
from apex.persistence.models import ApiConsumer, ConsumerKey, ConsumerScope
from apex.settings import get_settings

DEV_KEY = "dev-key-123"


class ExplodingFactory:
    """Session factory standing in for an unreachable database."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise ConnectionError("postgres is down")


class FakeSession:
    def __init__(self, consumer: Any | None) -> None:
        self._consumer = consumer
        self.committed = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def scalar(self, _stmt: Any) -> Any | None:
        entity = _stmt.column_descriptions[0].get("entity")
        if entity is ConsumerKey:
            if isinstance(self._consumer, ConsumerKey):
                return self._consumer
            if isinstance(self._consumer, ApiConsumer) and self._consumer.keys:
                return self._consumer.keys[0]
            return None
        if isinstance(self._consumer, ConsumerKey):
            return self._consumer.consumer
        return self._consumer

    async def scalars(self, _stmt: Any) -> list[ApiConsumer]:
        if self._consumer is None:
            return []
        if isinstance(self._consumer, ConsumerKey):
            return [self._consumer.consumer]
        if isinstance(self._consumer, ApiConsumer):
            return [self._consumer]
        return []

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class CandidateSession(FakeSession):
    """Return a complete candidate set and reject unexpected row locking."""

    def __init__(self, consumers: list[ApiConsumer]) -> None:
        super().__init__(None)
        self._consumers = consumers

    async def scalars(self, _stmt: Any) -> list[ApiConsumer]:
        return self._consumers

    async def scalar(self, _stmt: Any) -> Any | None:
        raise AssertionError("candidate resolution should have failed or completed without repair")


class LockingCandidateSession(CandidateSession):
    """Return all optimistic candidates, then one aggregate for row locking."""

    def __init__(self, consumers: list[ApiConsumer], locked: ApiConsumer) -> None:
        super().__init__(consumers)
        self._locked = locked

    async def scalar(self, _stmt: Any) -> ApiConsumer:
        return self._locked


def _credential_consumer(
    *,
    consumer_id: str,
    key_hash: str,
    role: str = "viewer",
    enabled: bool = True,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    deleted_at: datetime | None = None,
    keys: list[ConsumerKey] | None = None,
) -> ApiConsumer:
    consumer = ApiConsumer(
        id=consumer_id,
        name=consumer_id,
        key_hash=key_hash,
        consumer_type="headless",
        role=role,
        enabled=enabled,
        expires_at=expires_at,
        revoked_at=revoked_at,
        deleted_at=deleted_at,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    consumer.keys = keys or []
    for key in consumer.keys:
        key.consumer = consumer
        key.consumer_id = consumer_id
    return consumer


# ── extract_api_key ──────────────────────────────────────────────────────────


def test_extract_api_key_from_str_headers_case_insensitive() -> None:
    assert extract_api_key({"X-Api-Key": "abc"}) == "abc"


def test_extract_api_key_from_bytes_headers() -> None:
    assert extract_api_key({b"x-api-key": b"abc"}) == "abc"


def test_extract_api_key_bearer_fallback() -> None:
    assert extract_api_key({b"authorization": b"Bearer tok123"}) == "tok123"
    assert extract_api_key({"Authorization": "bearer tok123"}) == "tok123"


def test_extract_api_key_rejects_mixed_credential_headers() -> None:
    headers = {"x-api-key": "primary", "authorization": "Bearer secondary"}
    assert extract_api_key(headers) is None


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


def test_extract_api_key_never_coerces_arbitrary_header_objects() -> None:
    class HostileHeaderPart:
        called = False

        def __str__(self) -> str:
            self.called = True
            raise AssertionError("untrusted header coercion must not run")

    hostile_name = HostileHeaderPart()
    hostile_unknown_value = HostileHeaderPart()
    assert (
        extract_api_key(
            [
                (hostile_name, hostile_unknown_value),
                (b"x-unrelated", hostile_unknown_value),
                (b"x-api-key", b"safe-key"),
            ]
        )
        == "safe-key"
    )
    assert hostile_name.called is False
    assert hostile_unknown_value.called is False

    hostile_credential = HostileHeaderPart()
    assert extract_api_key([(b"x-api-key", hostile_credential)]) is None
    assert hostile_credential.called is False


@pytest.mark.parametrize(
    "headers",
    [
        {"x-api-key": "x" * 4_097},
        {"x-api-key": "é" * 2_049},
        {"authorization": f"Bearer {'é' * 2_049}"},
        {b"authorization": b"Bearer " + (b"x" * 4_097)},
    ],
)
def test_extract_api_key_rejects_oversized_credentials(headers: dict[Any, Any]) -> None:
    assert extract_api_key(headers) is None


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


def test_hash_api_key_requires_pepper_in_locked_down_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apex.auth.service as svc
    from apex.settings import ApexSettings, AuthSettings

    unsafe = ApexSettings.model_construct(
        environment="production", auth=AuthSettings(api_key_hash_pepper=None)
    )
    monkeypatch.setattr(svc, "get_settings", lambda: unsafe)

    with pytest.raises(RuntimeError, match="pepper is required"):
        hash_api_key("some-key")


# ── IdentityResolver ─────────────────────────────────────────────────────────


async def test_dev_key_requires_authoritative_collision_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()
    resolver = IdentityResolver(session_factory=factory)

    with pytest.raises(AuthStoreUnavailableError):
        await resolver.resolve(DEV_KEY)

    assert factory.calls == 1


async def test_dev_key_accepts_injected_authoritative_no_collision_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()

    async def collision_check(_api_key: str) -> bool:
        return False

    resolver = IdentityResolver(
        session_factory=factory,
        dev_key_collision_check=collision_check,
    )
    identity = await resolver.resolve(DEV_KEY)

    assert identity is not None
    assert identity.name == "dev"
    assert identity.role is Role.ADMIN
    assert identity.is_unscoped
    assert factory.calls == 0


async def test_multi_generation_key_matching_two_active_consumers_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "shared-across-pepper-generations"
    current_pepper = "current-pepper"
    previous_pepper = "previous-pepper"
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", current_pepper)
    monkeypatch.setenv(
        "APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS",
        f'["{previous_pepper}"]',
    )
    current_hash = hash_api_key(plaintext)
    previous_hash = hmac.new(
        previous_pepper.encode(),
        plaintext.encode(),
        hashlib.sha256,
    ).hexdigest()
    lifecycle_consumer = _credential_consumer(
        consumer_id="scoped-viewer",
        key_hash=current_hash,
        keys=[
            ConsumerKey(
                id="current-key",
                key_hash=current_hash,
                last_used_at=datetime.now(UTC),
            )
        ],
    )
    # This intentionally models a pre-key-lifecycle parent row. The different
    # pepper digest bypasses both per-column unique constraints.
    legacy_consumer = _credential_consumer(
        consumer_id="unscoped-admin",
        key_hash=previous_hash,
        role="admin",
    )
    session = CandidateSession([lifecycle_consumer, legacy_consumer])

    identity = await IdentityResolver(session_factory=lambda: session).resolve(plaintext)

    assert identity is None
    assert not session.committed


async def test_candidate_resolution_deduplicates_generations_and_ignores_inactive_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "one-consumer-many-generations"
    current_pepper = "current-pepper"
    previous_pepper = "previous-pepper"
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", current_pepper)
    monkeypatch.setenv(
        "APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS",
        f'["{previous_pepper}"]',
    )
    current_hash = hash_api_key(plaintext)
    previous_hash = hmac.new(
        previous_pepper.encode(),
        plaintext.encode(),
        hashlib.sha256,
    ).hexdigest()
    now = datetime.now(UTC)
    expected = _credential_consumer(
        consumer_id="expected-viewer",
        key_hash=current_hash,
        keys=[
            ConsumerKey(id="current", key_hash=current_hash, last_used_at=now),
            # A still-valid grace row for the same consumer is not a second identity.
            ConsumerKey(
                id="grace",
                key_hash=previous_hash,
                expires_at=now + timedelta(minutes=5),
                last_used_at=now,
            ),
        ],
    )
    inactive = [
        _credential_consumer(
            consumer_id="disabled",
            key_hash=previous_hash,
            enabled=False,
        ),
        _credential_consumer(
            consumer_id="expired-parent",
            key_hash=previous_hash,
            expires_at=now - timedelta(seconds=1),
        ),
        _credential_consumer(
            consumer_id="revoked-parent",
            key_hash=previous_hash,
            revoked_at=now,
        ),
        _credential_consumer(
            consumer_id="expired-key",
            key_hash=previous_hash,
            keys=[
                ConsumerKey(
                    id="expired",
                    key_hash=previous_hash,
                    expires_at=now - timedelta(seconds=1),
                )
            ],
        ),
        _credential_consumer(
            consumer_id="revoked-key",
            key_hash=previous_hash,
            keys=[ConsumerKey(id="revoked", key_hash=previous_hash, revoked_at=now)],
        ),
    ]
    session = CandidateSession([expected, *inactive])

    identity = await IdentityResolver(session_factory=lambda: session).resolve(plaintext)

    assert identity is not None
    assert identity.consumer_id == "expected-viewer"
    assert identity.role is Role.VIEWER
    assert not session.committed


async def test_rehash_skips_expired_sibling_current_hash_and_repairs_active_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "old-grace-remains-active"
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "current-pepper")
    current_hash = hash_api_key(plaintext)
    old_hash = legacy_hash_api_key(plaintext)
    now = datetime.now(UTC)
    old_key = ConsumerKey(
        id="old-grace",
        key_hash=old_hash,
        expires_at=now + timedelta(minutes=5),
        last_used_at=now,
    )
    expired_current_key = ConsumerKey(
        id="expired-current",
        key_hash=current_hash,
        expires_at=now - timedelta(seconds=1),
    )
    consumer = _credential_consumer(
        consumer_id="consumer-with-stale-pointer",
        key_hash=current_hash,
        keys=[old_key, expired_current_key],
    )
    session = FakeSession(consumer)

    identity = await IdentityResolver(session_factory=lambda: session).resolve(plaintext)

    assert identity is not None
    assert identity.consumer_id == consumer.id
    assert old_key.key_hash == old_hash
    assert expired_current_key.key_hash == current_hash
    assert consumer.key_hash == old_hash
    assert session.committed


async def test_rehash_skips_current_hash_reserved_by_inactive_other_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "cross-consumer-current-reservation"
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "current-pepper")
    current_hash = hash_api_key(plaintext)
    old_hash = legacy_hash_api_key(plaintext)
    now = datetime.now(UTC)
    active = _credential_consumer(
        consumer_id="active-old-generation",
        key_hash=old_hash,
        keys=[ConsumerKey(id="old", key_hash=old_hash, last_used_at=now)],
    )
    inactive_owner = _credential_consumer(
        consumer_id="inactive-current-owner",
        key_hash=current_hash,
        enabled=False,
        keys=[ConsumerKey(id="current", key_hash=current_hash)],
    )
    session = CandidateSession([active, inactive_owner])

    identity = await IdentityResolver(session_factory=lambda: session).resolve(plaintext)

    assert identity is not None
    assert identity.consumer_id == active.id
    assert active.key_hash == old_hash
    assert active.keys[0].key_hash == old_hash
    assert not session.committed


async def test_rehash_updates_free_child_when_current_parent_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "parent-and-child-occupancy-are-independent"
    current_pepper = "current-pepper"
    previous_pepper = "previous-pepper"
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", current_pepper)
    monkeypatch.setenv(
        "APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS",
        f'["{previous_pepper}"]',
    )
    current_hash = hash_api_key(plaintext)
    previous_hash = hmac.new(
        previous_pepper.encode(),
        plaintext.encode(),
        hashlib.sha256,
    ).hexdigest()
    now = datetime.now(UTC)
    old_key = ConsumerKey(id="previous", key_hash=previous_hash, last_used_at=now)
    active = _credential_consumer(
        consumer_id="active-previous-generation",
        key_hash=previous_hash,
        keys=[old_key],
    )
    inactive_parent_owner = _credential_consumer(
        consumer_id="inactive-current-parent",
        key_hash=current_hash,
        enabled=False,
    )
    session = LockingCandidateSession([active, inactive_parent_owner], active)

    identity = await IdentityResolver(session_factory=lambda: session).resolve(plaintext)

    assert identity is not None
    assert identity.consumer_id == active.id
    assert old_key.key_hash == current_hash
    # api_consumers.key_hash has an independent unique constraint, so keep the
    # old pointer instead of colliding with the inactive parent row.
    assert active.key_hash == previous_hash
    assert session.committed

    # Once the old pepper is retired, the newly rehashed ConsumerKey remains an
    # authoritative match even though the parent pointer could not move.
    monkeypatch.delenv("APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS")
    get_settings.cache_clear()
    post_rotation_session = CandidateSession([active, inactive_parent_owner])
    post_rotation = await IdentityResolver(session_factory=lambda: post_rotation_session).resolve(
        plaintext
    )

    assert post_rotation is not None
    assert post_rotation.consumer_id == active.id
    assert not post_rotation_session.committed


async def test_dev_key_fails_closed_on_active_persisted_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    key_hash = hash_api_key(DEV_KEY)
    persisted = _credential_consumer(
        consumer_id="scoped-consumer",
        key_hash=key_hash,
        keys=[ConsumerKey(id="persisted", key_hash=key_hash)],
    )
    session = CandidateSession([persisted])

    identity = await IdentityResolver(session_factory=lambda: session).resolve(DEV_KEY)

    assert identity is None
    assert not session.committed


async def test_dev_key_fails_closed_on_inactive_or_retired_persisted_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    key_hash = hash_api_key(DEV_KEY)
    now = datetime.now(UTC)
    inactive = [
        _credential_consumer(
            consumer_id="disabled",
            key_hash=key_hash,
            enabled=False,
        ),
        _credential_consumer(
            consumer_id="expired",
            key_hash=key_hash,
            expires_at=now - timedelta(seconds=1),
        ),
        _credential_consumer(
            consumer_id="revoked",
            key_hash=key_hash,
            revoked_at=now,
        ),
        _credential_consumer(
            consumer_id="revoked-key-only",
            key_hash=legacy_hash_api_key("different-parent-key"),
            keys=[
                ConsumerKey(
                    id="revoked-key-only",
                    key_hash=key_hash,
                    revoked_at=now,
                )
            ],
        ),
        _credential_consumer(
            consumer_id="expired-key-only",
            key_hash=legacy_hash_api_key("another-parent-key"),
            keys=[
                ConsumerKey(
                    id="expired-key-only",
                    key_hash=key_hash,
                    expires_at=now - timedelta(seconds=1),
                )
            ],
        ),
    ]
    session = CandidateSession(inactive)

    identity = await IdentityResolver(session_factory=lambda: session).resolve(DEV_KEY)

    assert identity is None
    assert not session.committed


async def test_missing_key_resolves_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    resolver = IdentityResolver(session_factory=ExplodingFactory())
    assert await resolver.resolve(None) is None
    assert await resolver.resolve("") is None


@pytest.mark.parametrize(
    "api_key",
    [
        "x" * 4_097,
        "é" * 2_049,
        "\ud800",
        b"bytes-are-not-a-key",
        123,
    ],
)
async def test_malformed_or_oversized_key_fails_before_authoritative_lookup(
    monkeypatch: pytest.MonkeyPatch,
    api_key: Any,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()
    resolver = IdentityResolver(session_factory=factory)

    assert await resolver.resolve(api_key) is None
    assert factory.calls == 0


async def test_db_errors_surface_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    factory = ExplodingFactory()
    resolver = IdentityResolver(session_factory=factory)
    with pytest.raises(AuthStoreUnavailableError) as exc_info:
        await resolver.resolve("not-the-dev-key")
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert "postgres is down" not in repr(exc_info.value)
    assert factory.calls == 1


async def test_db_error_logging_does_not_invoke_spoofed_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)

    class HostileStoreError(RuntimeError):
        class_accessed = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_accessed = True
                raise AssertionError("exception __class__ hook must not execute")
            return super().__getattribute__(name)

    error = HostileStoreError("postgresql://admin:secret@database.internal/apex")

    class Factory:
        def __call__(self) -> Any:
            raise error

    with pytest.raises(AuthStoreUnavailableError) as exc_info:
        await IdentityResolver(session_factory=Factory()).resolve("not-the-dev-key")

    assert HostileStoreError.class_accessed is False
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert "secret" not in repr(exc_info.value)


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


async def test_legacy_credential_consumer_name_is_quarantined_from_identity_and_logs() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    consumer = ApiConsumer(
        id="legacy-credential-name",
        name=credential,
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = []

    class FailingCommitSession(FakeSession):
        async def commit(self) -> None:
            raise RuntimeError("metadata write failed")

    session = FailingCommitSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    with capture_logs() as logs:
        identity = await resolver.resolve("some-key")

    assert identity is not None
    assert identity.name == "[REDACTED]"
    payload = user_payload(identity)
    assert payload["name"] == "[REDACTED]"
    assert payload["display_name"] == "[REDACTED]"
    assert credential not in repr(logs)


@pytest.mark.parametrize("scope_count", [2, 257])
async def test_db_lookup_fails_closed_for_invalid_legacy_scope_sets(scope_count: int) -> None:
    consumer = ApiConsumer(
        id="legacy-invalid",
        name="legacy-invalid",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    if scope_count == 2:
        consumer.scopes = [
            ConsumerScope(id="s1", project_id="p1", app_id=None),
            ConsumerScope(id="s2", project_id="p1", app_id=None),
        ]
    else:
        consumer.scopes = [
            ConsumerScope(id=f"s{index}", project_id=f"p{index}", app_id=None)
            for index in range(scope_count)
        ]
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("some-key") is None
    assert not session.committed


async def test_db_lookup_does_not_normalize_legacy_scope_authority() -> None:
    consumer = ApiConsumer(
        id="legacy-noncanonical",
        name="legacy-noncanonical",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = [
        ConsumerScope(id="s1", project_id=" p1 ", app_id=None),
    ]
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("some-key") is None
    assert not session.committed


async def test_db_lookup_rejects_credential_bearing_legacy_scope_without_reflection() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    consumer = ApiConsumer(
        id="legacy-credential-scope",
        name="safe-name",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = [ConsumerScope(id="s1", project_id=credential, app_id=None)]
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    with capture_logs() as logs:
        identity = await resolver.resolve("some-key")

    assert identity is None
    assert not session.committed
    assert credential not in repr(logs)


async def test_db_lookup_rejects_credential_bearing_legacy_consumer_id_before_migration() -> None:
    credential = "Authorization: Bearer canary"
    consumer = ApiConsumer(
        id=credential,
        name="safe-name",
        key_hash=hash_api_key("some-key"),
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = []
    session = FakeSession(consumer)

    with capture_logs() as logs:
        identity = await IdentityResolver(session_factory=lambda: session).resolve("some-key")

    assert identity is None
    assert consumer.keys == []
    assert not session.committed
    assert credential not in repr(logs)


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


async def test_db_lookup_resyncs_legacy_hash_pointer_from_active_consumer_key() -> None:
    stale_hash = legacy_hash_api_key("different-key")
    key_hash = hash_api_key("some-key")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=stale_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    key = ConsumerKey(
        id="k1",
        consumer_id="abc123",
        key_hash=key_hash,
        last_used_at=datetime.now(UTC),
    )
    key.consumer = consumer
    consumer.keys = [key]
    session = FakeSession(key)
    resolver = IdentityResolver(session_factory=lambda: session)

    identity = await resolver.resolve("some-key")

    assert identity is not None
    assert consumer.key_hash == key_hash
    assert session.committed


async def test_db_lookup_backfills_consumer_key_for_legacy_consumer_row() -> None:
    key_hash = hash_api_key("some-key")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=key_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    consumer.scopes = []
    consumer.keys = []
    session = FakeSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    identity = await resolver.resolve("some-key")

    assert identity is not None
    assert [key.key_hash for key in consumer.keys] == [key_hash]
    assert consumer.keys[0].expires_at is None
    assert session.committed


async def test_legacy_key_backfill_does_not_copy_credential_bearing_actor() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    key_hash = hash_api_key("some-key")
    consumer = ApiConsumer(
        id="legacy-actor",
        name="safe-name",
        key_hash=key_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
        created_by=credential,
    )
    consumer.scopes = []
    consumer.keys = []
    session = FakeSession(consumer)

    identity = await IdentityResolver(session_factory=lambda: session).resolve("some-key")

    assert identity is not None
    assert len(consumer.keys) == 1
    assert consumer.keys[0].created_by is None
    assert credential not in repr(consumer.keys[0])
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


async def test_key_metadata_repair_revalidates_after_concurrent_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APEX_AUTH__API_KEY_HASH_PEPPER", "new-pepper")
    old_hash = legacy_hash_api_key("old-key")
    new_hash = hash_api_key("new-key")
    stale_consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=old_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    stale_consumer.scopes = []
    stale_key = ConsumerKey(id="old", consumer_id="abc123", key_hash=old_hash)
    stale_key.consumer = stale_consumer

    rotated_consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=new_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    rotated_consumer.scopes = []
    rotated_old_key = ConsumerKey(
        id="old",
        consumer_id="abc123",
        key_hash=old_hash,
        revoked_at=datetime.now(UTC),
    )
    rotated_new_key = ConsumerKey(id="new", consumer_id="abc123", key_hash=new_hash)
    rotated_old_key.consumer = rotated_consumer
    rotated_new_key.consumer = rotated_consumer
    rotated_consumer.keys = [rotated_old_key, rotated_new_key]

    class RotationInterleavingSession(FakeSession):
        async def scalar(self, statement: Any) -> Any | None:
            entity = statement.column_descriptions[0].get("entity")
            if entity is ConsumerKey:
                return stale_key
            return rotated_consumer

    session = RotationInterleavingSession(stale_key)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("old-key") is None
    assert rotated_consumer.key_hash == new_hash
    assert rotated_old_key.revoked_at is not None
    assert not session.committed


async def test_legacy_fallback_rejects_matching_explicit_revoked_key() -> None:
    key_hash = hash_api_key("old-key")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=key_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
    )
    consumer.scopes = []
    revoked_key = ConsumerKey(
        id="old",
        consumer_id="abc123",
        key_hash=key_hash,
        revoked_at=datetime.now(UTC),
    )
    revoked_key.consumer = consumer
    consumer.keys = [revoked_key]

    class RevokedKeyFilteredSession(FakeSession):
        async def scalar(self, statement: Any) -> Any | None:
            entity = statement.column_descriptions[0].get("entity")
            if entity is ConsumerKey:
                return None
            return consumer

    session = RevokedKeyFilteredSession(consumer)
    resolver = IdentityResolver(session_factory=lambda: session)

    assert await resolver.resolve("old-key") is None
    assert not session.committed


async def test_db_lookup_throttles_last_used_at_write() -> None:
    key_hash = hash_api_key("some-key")
    consumer = ApiConsumer(
        id="abc123",
        name="ops-bot",
        key_hash=key_hash,
        consumer_type="headless",
        role="operator",
        enabled=True,
        last_used_at=datetime.now(UTC),
    )
    consumer.scopes = []
    consumer.keys = [
        ConsumerKey(
            id="k1",
            consumer_id="abc123",
            key_hash=key_hash,
            last_used_at=datetime.now(UTC),
        )
    ]
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
