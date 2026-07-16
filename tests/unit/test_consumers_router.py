"""/admin/consumers routes against an in-memory fake repository."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.auth.service import hash_api_key
from apex.persistence.models import ApiConsumer, ConsumerDeletionRecord, ConsumerKey, ConsumerScope
from apex.persistence.repositories.consumers import (
    AmbiguousConsumerKeyExpiryError,
    DuplicateConsumerNameError,
)
from apex.routers.consumers import ConsumerCreated, get_consumers_repository, router

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
OPERATOR = ConsumerIdentity(
    consumer_id="op-1", name="op", consumer_type=ConsumerType.DASHBOARD, role=Role.OPERATOR
)
SCOPED_ADMIN = ConsumerIdentity(
    consumer_id="tenant-admin",
    name="tenant-admin",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.ADMIN,
    scopes=[ScopeRef(project_id="proj-a")],
)
APP_SCOPED_ADMIN = ConsumerIdentity(
    consumer_id="app-admin",
    name="app-admin",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.ADMIN,
    scopes=[ScopeRef(project_id="proj-a", app_id="app-a")],
)


def test_one_time_api_key_is_serialized_but_hidden_from_model_repr() -> None:
    secret = "one-time-api-key-secret-canary"
    response = ConsumerCreated(
        id="consumer-1",
        name="consumer",
        consumer_type=ConsumerType.HEADLESS,
        role=Role.VIEWER,
        enabled=True,
        scopes=[],
        created_at=None,
        last_used_at=None,
        key_fingerprint="12345678",
        api_key=secret,
    )

    assert response.model_dump()["api_key"] == secret
    assert secret not in repr(response)


class FakeConsumersRepository:
    """In-memory stand-in matching ConsumersRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, ApiConsumer] = {}
        self.deletion_records: list[ConsumerDeletionRecord] = []
        self.for_update_calls: list[str] = []
        self.list_calls = 0
        self.create_name_race = False
        self.update_name_race = False

    def seed(self, *, consumer_id: str, name: str, key_hash: str) -> ApiConsumer:
        consumer = ApiConsumer(
            id=consumer_id,
            name=name,
            consumer_type="headless",
            role="viewer",
            key_hash=key_hash,
            enabled=True,
            created_at=datetime.now(UTC),
            last_used_at=None,
        )
        consumer.scopes = []
        consumer.keys = [
            ConsumerKey(
                id=uuid4().hex,
                consumer_id=consumer.id,
                key_hash=key_hash,
                expiry_source="independent",
                created_at=datetime.now(UTC),
            )
        ]
        self.rows[consumer_id] = consumer
        return consumer

    async def list_all(
        self,
        *,
        allowed_scopes: Sequence[ScopeRef] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ApiConsumer]:
        self.list_calls += 1
        rows = [consumer for consumer in self.rows.values() if consumer.deleted_at is None]
        if allowed_scopes is not None:
            project_wide = {scope.project_id for scope in allowed_scopes if scope.app_id is None}
            exact = {(scope.project_id, scope.app_id) for scope in allowed_scopes}
            rows = [
                consumer
                for consumer in rows
                if consumer.scopes
                and all(
                    scope.project_id in project_wide or (scope.project_id, scope.app_id) in exact
                    for scope in consumer.scopes
                )
            ]
        rows.sort(key=lambda consumer: (consumer.created_at, consumer.id))
        return rows[offset : offset + limit]

    async def get(self, consumer_id: str) -> ApiConsumer | None:
        consumer = self.rows.get(consumer_id)
        if consumer is None or consumer.deleted_at is not None:
            return None
        return consumer

    async def get_for_update(self, consumer_id: str) -> ApiConsumer | None:
        self.for_update_calls.append(consumer_id)
        return await self.get(consumer_id)

    async def get_by_name(self, name: str) -> ApiConsumer | None:
        return next((c for c in self.rows.values() if c.name == name), None)

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
        if self.create_name_race:
            raise DuplicateConsumerNameError("concurrent duplicate")
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
            created_at=datetime.now(UTC),
            last_used_at=None,
        )
        consumer.scopes = [
            ConsumerScope(id=uuid4().hex, project_id=s.project_id, app_id=s.app_id) for s in scopes
        ]
        consumer.keys = [
            ConsumerKey(
                id=uuid4().hex,
                consumer_id=consumer.id,
                key_hash=key_hash,
                expiry_source="independent",
                created_at=datetime.now(UTC),
                created_by=created_by,
            )
        ]
        self.rows[consumer.id] = consumer
        return consumer

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
        consumer = self.rows.get(consumer_id)
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
        if self.update_name_race:
            raise DuplicateConsumerNameError("concurrent duplicate")
        if name is not None:
            consumer.name = name
        if role is not None:
            consumer.role = role
        if enabled is not None:
            consumer.enabled = enabled
        if scopes is not None:
            consumer.scopes = [
                ConsumerScope(id=uuid4().hex, project_id=s.project_id, app_id=s.app_id)
                for s in scopes
            ]
        if expires_at_set or expires_at is not None:
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
        return consumer

    async def replace_key_hash(
        self,
        consumer_id: str,
        key_hash: str,
        *,
        rotated_by: str | None = None,
        grace_expires_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApiConsumer | None:
        consumer = self.rows.get(consumer_id)
        if consumer is None:
            return None
        now = datetime.now(UTC)
        if not any(key.key_hash == consumer.key_hash for key in consumer.keys):
            consumer.keys.append(
                ConsumerKey(
                    id=uuid4().hex,
                    consumer_id=consumer.id,
                    key_hash=consumer.key_hash,
                    expiry_source="independent",
                    created_at=now,
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
                id=uuid4().hex,
                consumer_id=consumer.id,
                key_hash=key_hash,
                expires_at=expires_at,
                expiry_source="explicit" if expires_at is not None else "independent",
                rotated_from_id=rotated_from_id,
                created_at=now,
                created_by=rotated_by,
            )
        )
        consumer.key_hash = key_hash
        consumer.rotated_at = now
        consumer.rotation_count = int(consumer.rotation_count or 0) + 1
        consumer.updated_by = rotated_by
        return consumer

    async def delete(self, consumer_id: str, *, deleted_by: str | None = None) -> bool:
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return False
        return await self.delete_existing(consumer, deleted_by=deleted_by)

    async def delete_existing(
        self, consumer: ApiConsumer, *, deleted_by: str | None = None
    ) -> bool:
        if consumer.deleted_at is not None:
            return False
        deleted_at = datetime.now(UTC)
        consumer.deleted_at = deleted_at
        consumer.revoked_at = consumer.revoked_at or deleted_at
        consumer.enabled = False
        consumer.updated_by = deleted_by
        for key in consumer.keys:
            key.revoked_at = key.revoked_at or deleted_at
        self.deletion_records.append(
            ConsumerDeletionRecord(
                id=uuid4().hex,
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
        return True


def make_client(repo: FakeConsumersRepository, identity: ConsumerIdentity = ADMIN) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_consumers_repository] = lambda: repo
    app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


@pytest.fixture(autouse=True)
def audit_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr("apex.routers.consumers.append_audit_event_best_effort", capture)
    return events


CREATE_BODY = {
    "name": "dashboard-ui",
    "consumer_type": "dashboard",
    "role": "operator",
    "scopes": [{"project_id": "proj-a", "app_id": None}],
}


def test_create_returns_raw_key_exactly_once_and_stores_only_hash(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        response = client.post("/v1/admin/consumers", json=CREATE_BODY)
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        body = response.json()
        api_key = body["api_key"]
        assert len(api_key) >= 32
        stored = repo.rows[body["id"]]
        assert stored.key_hash == hash_api_key(api_key)
        assert [key.key_hash for key in stored.keys] == [stored.key_hash]
        assert api_key != stored.key_hash  # raw key is not what's persisted
        assert body["key_fingerprint"] == stored.key_hash[:8]
        assert body["scopes"] == [{"project_id": "proj-a", "app_id": None}]
        assert body["created_by"] == ADMIN.consumer_id
        # Never retrievable again: subsequent reads expose no api_key.
        read = client.get(f"/v1/admin/consumers/{body['id']}")
        assert read.status_code == 200
        assert "api_key" not in read.json()
        listing = client.get("/v1/admin/consumers")
        assert all("api_key" not in row for row in listing.json())
    assert [(event.action, event.resource_id) for event in audit_events] == [
        ("consumer.create", body["id"])
    ]


def test_list_rejects_huge_offset_before_repository() -> None:
    repo = FakeConsumersRepository()

    with make_client(repo) as client:
        response = client.get("/v1/admin/consumers", params={"offset": 10_001})

    assert response.status_code == 422
    assert repo.list_calls == 0


def test_create_duplicate_name_conflicts() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 201
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 409


def test_consumer_name_writes_reject_credentials_without_reflection() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    repo = FakeConsumersRepository()
    target = repo.seed(consumer_id="target", name="safe-name", key_hash=hash_api_key("target"))

    with make_client(repo) as client:
        created = client.post(
            "/v1/admin/consumers",
            json={**CREATE_BODY, "name": credential},
        )
        updated = client.patch(
            "/v1/admin/consumers/target",
            json={"name": credential},
        )

    assert created.status_code == 422
    assert updated.status_code == 422
    assert credential.encode() not in created.content
    assert credential.encode() not in updated.content
    assert target.name == "safe-name"


def test_consumer_legacy_metadata_and_malformed_key_hash_are_quarantined() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="safe-name", key_hash=hash_api_key("target"))
    row.name = credential
    row.id = credential
    row.created_by = credential
    row.updated_by = credential
    row.key_hash = credential
    row.scopes = [
        ConsumerScope(
            id=uuid4().hex,
            consumer_id=row.id,
            project_id=credential,
            app_id=credential,
        )
    ]

    with make_client(repo) as client:
        response = client.get("/v1/admin/consumers/target")

    assert response.status_code == 200
    assert credential.encode() not in response.content
    assert response.json()["name"] == "[REDACTED]"
    assert response.json()["id"] == "[REDACTED]"
    assert response.json()["created_by"] == "[REDACTED]"
    assert response.json()["updated_by"] == "[REDACTED]"
    assert response.json()["key_fingerprint"] == "invalid"
    assert response.json()["scopes"] == [{"project_id": "[REDACTED]", "app_id": "[REDACTED]"}]


@pytest.mark.parametrize(
    "scopes",
    [
        [{"project_id": "proj-a"}, {"project_id": "proj-a"}],
        [
            {"project_id": "proj-a"},
            {"project_id": "proj-a", "app_id": "app-a"},
        ],
    ],
)
def test_create_rejects_duplicate_or_redundant_scopes_before_write(
    scopes: list[dict[str, str]],
) -> None:
    repo = FakeConsumersRepository()

    with make_client(repo) as client:
        response = client.post("/v1/admin/consumers", json={**CREATE_BODY, "scopes": scopes})

    assert response.status_code == 422
    assert repo.rows == {}


def test_create_name_race_conflicts() -> None:
    repo = FakeConsumersRepository()
    repo.create_name_race = True

    with make_client(repo) as client:
        response = client.post("/v1/admin/consumers", json=CREATE_BODY)

    assert response.status_code == 409
    assert response.json()["title"] == "consumer name already exists"
    assert "dashboard-ui" not in response.text


def test_rotate_replaces_hash_and_returns_new_key_once(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        old_hash = repo.rows[created["id"]].key_hash
        old_key = repo.rows[created["id"]].keys[0]
        old_last_used = datetime(2025, 1, 2, tzinfo=UTC)
        repo.rows[created["id"]].last_used_at = old_last_used
        response = client.post(f"/v1/admin/consumers/{created['id']}/rotate")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        body = response.json()
        assert body["api_key"] != created["api_key"]
        new_hash = repo.rows[created["id"]].key_hash
        assert new_hash == hash_api_key(body["api_key"])
        assert new_hash != old_hash
        assert repo.rows[created["id"]].last_used_at == old_last_used
        assert body["key_fingerprint"] == new_hash[:8]
        assert body["rotation_count"] == 1
        assert body["rotated_at"] is not None
        assert old_key.revoked_at is not None
        assert len(repo.rows[created["id"]].keys) == 2
        assert repo.rows[created["id"]].keys[-1].rotated_from_id == old_key.id
        assert repo.for_update_calls == [created["id"]]
    assert [event.action for event in audit_events] == [
        "consumer.create",
        "consumer.rotate_key",
    ]


def test_rotate_with_grace_keeps_old_key_temporarily() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        old_key = repo.rows[created["id"]].keys[0]
        response = client.post(
            f"/v1/admin/consumers/{created['id']}/rotate",
            json={"grace_period_seconds": 120},
        )
        assert response.status_code == 200
        assert old_key.revoked_at is None
        assert old_key.expires_at is not None
        assert old_key.expires_at > datetime.now(UTC)
        assert repo.rows[created["id"]].keys[-1].created_by == ADMIN.consumer_id


def test_rotate_materializes_missing_legacy_key_before_applying_grace() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="legacy", name="legacy", key_hash=hash_api_key("old-key"))
    row.keys = []

    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers/legacy/rotate",
            json={"grace_period_seconds": 120},
        )

    assert response.status_code == 200
    old = next(key for key in row.keys if key.key_hash == hash_api_key("old-key"))
    assert old.revoked_at is None
    assert old.expires_at is not None
    assert old.expires_at > datetime.now(UTC)


def test_create_accepts_expires_at() -> None:
    repo = FakeConsumersRepository()
    expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={**CREATE_BODY, "expires_at": expires_at},
        )
        assert response.status_code == 201
        assert response.json()["expires_at"] is not None


def test_create_normalizes_naive_expiry_to_utc() -> None:
    repo = FakeConsumersRepository()
    naive_expiry = (datetime.now(UTC) + timedelta(days=30)).replace(tzinfo=None)

    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={**CREATE_BODY, "expires_at": naive_expiry.isoformat()},
        )

    assert response.status_code == 201
    row = repo.rows[response.json()["id"]]
    assert row.expires_at == naive_expiry.replace(tzinfo=UTC)


def test_create_rejects_expired_consumer() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={
                **CREATE_BODY,
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )

    assert response.status_code == 422
    assert repo.rows == {}


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
def test_create_rejects_lifecycle_timestamp_outside_utc_range(timestamp: str) -> None:
    repo = FakeConsumersRepository()

    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={**CREATE_BODY, "expires_at": timestamp},
        )

    assert response.status_code == 422
    assert "representable UTC range" in response.json()["title"]
    assert repo.rows == {}


def test_rotate_rejects_expired_new_key_without_revoking_old_key() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        old_key = repo.rows[created["id"]].keys[0]
        response = client.post(
            f"/v1/admin/consumers/{created['id']}/rotate",
            json={"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        )

    assert response.status_code == 422
    assert old_key.revoked_at is None
    assert len(repo.rows[created["id"]].keys) == 1


def test_target_rotation_rejects_grace_longer_than_new_key_lifetime(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("old"))
    old_key = row.keys[0]
    old_hash = row.key_hash

    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers/target/rotate",
            json={
                "grace_period_seconds": 7 * 24 * 60 * 60,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    assert response.status_code == 409
    assert "beyond the old key's grace period" in response.json()["title"]
    assert row.key_hash == old_hash
    assert row.keys == [old_key]
    assert [(event.action, event.decision) for event in audit_events] == [
        ("consumer.rotate_key", "denied")
    ]


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
def test_rotate_rejects_lifecycle_timestamp_outside_utc_range(timestamp: str) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("old"))
    old_hash = row.key_hash
    old_key = row.keys[0]

    with make_client(repo) as client:
        response = client.post(
            "/v1/admin/consumers/target/rotate",
            json={"expires_at": timestamp},
        )

    assert response.status_code == 422
    assert "representable UTC range" in response.json()["title"]
    assert row.key_hash == old_hash
    assert row.keys == [old_key]


def test_self_rotation_requires_retry_grace_and_preserves_current_key(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    old_key = row.keys[0]

    with make_client(repo) as client:
        response = client.post(f"/v1/admin/consumers/{ADMIN.consumer_id}/rotate")

    assert response.status_code == 409
    assert "grace period of at least 60 seconds" in response.json()["title"]
    assert old_key.revoked_at is None
    assert old_key.expires_at is None
    assert len(row.keys) == 1
    assert [(event.action, event.decision) for event in audit_events] == [
        ("consumer.rotate_key", "denied")
    ]


def test_self_rotation_keeps_old_key_valid_through_response_retry_window() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    old_key = row.keys[0]
    new_expiry = datetime.now(UTC) + timedelta(minutes=5)

    with make_client(repo) as client:
        response = client.post(
            f"/v1/admin/consumers/{ADMIN.consumer_id}/rotate",
            json={"grace_period_seconds": 60, "expires_at": new_expiry.isoformat()},
        )

    assert response.status_code == 200
    assert old_key.revoked_at is None
    assert old_key.expires_at is not None
    assert old_key.expires_at > datetime.now(UTC)
    assert row.keys[-1].expires_at == new_expiry


def test_rotate_unknown_consumer_404() -> None:
    with make_client(FakeConsumersRepository()) as client:
        assert client.post("/v1/admin/consumers/nope/rotate").status_code == 404


def test_update_role_scopes_and_enabled(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        response = client.patch(
            f"/v1/admin/consumers/{created['id']}",
            json={"role": "viewer", "enabled": False, "scopes": [{"project_id": "proj-b"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "viewer"
        assert body["enabled"] is False
        assert body["scopes"] == [{"project_id": "proj-b", "app_id": None}]
        assert body["updated_by"] == ADMIN.consumer_id
    assert [event.action for event in audit_events] == ["consumer.create", "consumer.update"]


def test_update_name_race_conflicts() -> None:
    repo = FakeConsumersRepository()
    target = repo.seed(consumer_id="target", name="old-name", key_hash=hash_api_key("target"))
    repo.update_name_race = True

    with make_client(repo) as client:
        response = client.patch("/v1/admin/consumers/target", json={"name": "taken-name"})

    assert response.status_code == 409
    assert response.json()["title"] == "consumer name already exists"
    assert "taken-name" not in response.text
    assert target.name == "old-name"


def test_update_can_revoke_consumer() -> None:
    repo = FakeConsumersRepository()
    revoked_at = datetime.now(UTC).isoformat()
    with make_client(repo) as client:
        created = client.post("/v1/admin/consumers", json=CREATE_BODY).json()
        response = client.patch(
            f"/v1/admin/consumers/{created['id']}", json={"revoked_at": revoked_at}
        )
        assert response.status_code == 200
        assert response.json()["revoked_at"] is not None


def test_update_rejects_future_revocation_before_write() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("target"))

    with make_client(repo) as client:
        response = client.patch(
            "/v1/admin/consumers/target",
            json={"revoked_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        )

    assert response.status_code == 422
    assert row.revoked_at is None


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
def test_update_rejects_lifecycle_timestamp_outside_utc_range(timestamp: str) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("target"))

    with make_client(repo) as client:
        response = client.patch(
            "/v1/admin/consumers/target",
            json={"expires_at": timestamp},
        )

    assert response.status_code == 422
    assert "representable UTC range" in response.json()["title"]
    assert row.expires_at is None
    assert repo.for_update_calls == []


def test_update_explicit_null_clears_nullable_lifecycle_fields() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("target"))
    row.expires_at = datetime.now(UTC) + timedelta(days=3)
    row.revoked_at = datetime.now(UTC) + timedelta(days=1)

    with make_client(repo) as client:
        response = client.patch(
            "/v1/admin/consumers/target",
            json={"expires_at": None, "revoked_at": None},
        )

    assert response.status_code == 200
    assert response.json()["expires_at"] is None
    assert response.json()["revoked_at"] is None
    assert row.expires_at is None
    assert row.revoked_at is None


def test_update_lazily_repairs_legacy_inherited_initial_key_expiry() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("target"))
    inherited_expiry = datetime.now(UTC) + timedelta(days=3)
    row.expires_at = inherited_expiry
    row.keys[0].expires_at = inherited_expiry
    row.keys[0].expiry_source = "inherited"
    row.rotation_count = 0
    replacement = datetime.now(UTC) + timedelta(days=7)

    with make_client(repo) as client:
        response = client.patch(
            "/v1/admin/consumers/target",
            json={"expires_at": replacement.isoformat()},
        )

    assert response.status_code == 200
    assert row.expires_at == replacement
    assert row.keys[0].expires_at is None


def test_update_rejects_ambiguous_old_writer_rotated_key_expiry() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id="target", name="target", key_hash=hash_api_key("current"))
    old_expiry = datetime.now(UTC) + timedelta(days=3)
    row.expires_at = old_expiry
    row.rotation_count = 1
    row.keys[0].expires_at = old_expiry
    row.keys[0].expiry_source = "legacy_ambiguous"

    with make_client(repo) as client:
        response = client.patch(
            "/v1/admin/consumers/target",
            json={"expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat()},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "consumer key expiry update is ambiguous"
    assert row.expires_at == old_expiry


def test_scoped_admin_cannot_create_unscoped_admin(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo, identity=SCOPED_ADMIN) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={"name": "platform", "consumer_type": "headless", "role": "admin", "scopes": []},
        )
    assert response.status_code == 403
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.create", "denied", "Scoped admins cannot grant platform admin")
    ]


def test_scoped_admin_cannot_grant_out_of_scope_project(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo, identity=SCOPED_ADMIN) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={
                "name": "other-project",
                "consumer_type": "headless",
                "role": "operator",
                "scopes": [{"project_id": "proj-b"}],
            },
        )
    assert response.status_code == 403
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.create", "denied", "Scoped admins cannot grant out-of-scope access")
    ]


def test_app_scoped_admin_cannot_grant_project_wide_scope(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    with make_client(repo, identity=APP_SCOPED_ADMIN) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={
                "name": "project-wide",
                "consumer_type": "headless",
                "role": "operator",
                "scopes": [{"project_id": "proj-a", "app_id": None}],
            },
        )
    assert response.status_code == 403
    assert repo.rows == {}
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.create", "denied", "Scoped admins cannot grant out-of-scope access")
    ]


def test_app_scoped_admin_can_grant_same_app_scope() -> None:
    repo = FakeConsumersRepository()
    with make_client(repo, identity=APP_SCOPED_ADMIN) as client:
        response = client.post(
            "/v1/admin/consumers",
            json={
                "name": "same-app",
                "consumer_type": "headless",
                "role": "operator",
                "scopes": [{"project_id": "proj-a", "app_id": "app-a"}],
            },
        )
    assert response.status_code == 201


def test_app_scoped_admin_cannot_manage_or_rotate_project_wide_consumer() -> None:
    repo = FakeConsumersRepository()
    target = repo.seed(consumer_id="project-wide", name="wide", key_hash=hash_api_key("wide"))
    target.scopes = [ConsumerScope(id="scope-wide", project_id="proj-a", app_id=None)]
    original_hash = target.key_hash

    with make_client(repo, identity=APP_SCOPED_ADMIN) as client:
        assert client.get("/v1/admin/consumers/project-wide").status_code == 404
        assert client.post("/v1/admin/consumers/project-wide/rotate").status_code == 404

    assert target.key_hash == original_hash
    assert len(target.keys) == 1


def test_scoped_admin_can_manage_in_scope_consumer_only(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    in_scope = repo.seed(consumer_id="in", name="in", key_hash=hash_api_key("k-in"))
    in_scope.scopes = [ConsumerScope(id="s-in", project_id="proj-a", app_id=None)]
    out_scope = repo.seed(consumer_id="out", name="out", key_hash=hash_api_key("k-out"))
    out_scope.scopes = [ConsumerScope(id="s-out", project_id="proj-b", app_id=None)]

    with make_client(repo, identity=SCOPED_ADMIN) as client:
        listed = client.get("/v1/admin/consumers").json()
        assert [row["id"] for row in listed] == ["in"]
        assert client.get("/v1/admin/consumers/in").status_code == 200
        assert client.get("/v1/admin/consumers/out").status_code == 404
    assert [(event.action, event.decision, event.resource_id) for event in audit_events] == [
        ("consumer.get", "denied", "out")
    ]


def test_consumer_audit_quarantines_legacy_credential_shaped_target_id(
    audit_events: list[Any],
) -> None:
    credential_id = "password=legacy-id-secret"
    repo = FakeConsumersRepository()
    target = repo.seed(
        consumer_id=credential_id,
        name="legacy-target",
        key_hash=hash_api_key("legacy-target"),
    )
    target.scopes = [ConsumerScope(id="legacy-scope", project_id="proj-b", app_id=None)]

    with make_client(repo, identity=SCOPED_ADMIN) as client:
        response = client.get(f"/v1/admin/consumers/{credential_id}")

    assert response.status_code == 404
    assert len(audit_events) == 1
    assert audit_events[0].resource_id == "[REDACTED]"
    assert credential_id not in repr(audit_events[0])


def test_self_delete_conflicts_409(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    with make_client(repo) as client:
        response = client.delete(f"/v1/admin/consumers/{ADMIN.consumer_id}")
        assert response.status_code == 409
        assert ADMIN.consumer_id in repo.rows
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.delete", "denied", "A consumer cannot delete itself")
    ]


def test_self_disable_conflicts_409(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    with make_client(repo) as client:
        response = client.patch(f"/v1/admin/consumers/{ADMIN.consumer_id}", json={"enabled": False})
        assert response.status_code == 409
        # Other field updates on yourself remain allowed.
        ok = client.patch(f"/v1/admin/consumers/{ADMIN.consumer_id}", json={"name": "root2"})
        assert ok.status_code == 200
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.update", "denied", "A consumer cannot disable itself"),
        ("consumer.update", "allowed", None),
    ]


def test_self_revoke_conflicts_409(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))

    with make_client(repo) as client:
        response = client.patch(
            f"/v1/admin/consumers/{ADMIN.consumer_id}",
            json={"revoked_at": datetime.now(UTC).isoformat()},
        )

    assert response.status_code == 409
    assert row.revoked_at is None
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.update", "denied", "A consumer cannot revoke itself")
    ]


def test_self_immediate_expiry_conflicts_but_future_expiry_is_allowed(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))

    with make_client(repo) as client:
        denied = client.patch(
            f"/v1/admin/consumers/{ADMIN.consumer_id}",
            json={"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        )
        future = datetime.now(UTC) + timedelta(days=1)
        allowed = client.patch(
            f"/v1/admin/consumers/{ADMIN.consumer_id}",
            json={"expires_at": future.isoformat()},
        )

    assert denied.status_code == 409
    assert allowed.status_code == 200
    assert row.expires_at == future
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        (
            "consumer.update",
            "denied",
            "A consumer cannot expire itself inside the response retry window",
        ),
        ("consumer.update", "allowed", None),
    ]


def test_self_near_future_expiry_cannot_lock_out_response_retry() -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))

    with make_client(repo) as client:
        response = client.patch(
            f"/v1/admin/consumers/{ADMIN.consumer_id}",
            json={"expires_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()},
        )

    assert response.status_code == 409
    assert row.expires_at is None


def test_self_role_change_conflicts_409(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    row.role = "admin"
    with make_client(repo) as client:
        response = client.patch(f"/v1/admin/consumers/{ADMIN.consumer_id}", json={"role": "viewer"})
        assert response.status_code == 409
        assert repo.rows[ADMIN.consumer_id].role == "admin"
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.update", "denied", "A consumer cannot change its own role or scopes")
    ]


def test_self_scope_change_conflicts_409(audit_events: list[Any]) -> None:
    repo = FakeConsumersRepository()
    row = repo.seed(consumer_id=ADMIN.consumer_id, name="root", key_hash=hash_api_key("k1"))
    row.role = "admin"
    row.scopes = []
    with make_client(repo) as client:
        response = client.patch(
            f"/v1/admin/consumers/{ADMIN.consumer_id}",
            json={"scopes": [{"project_id": "proj-a"}]},
        )
        assert response.status_code == 409
        assert repo.rows[ADMIN.consumer_id].scopes == []
    assert [(event.action, event.decision, event.reason) for event in audit_events] == [
        ("consumer.update", "denied", "A consumer cannot change its own role or scopes")
    ]


def test_delete_other_consumer_soft_deletes_and_404_when_missing(
    audit_events: list[Any],
) -> None:
    repo = FakeConsumersRepository()
    victim = repo.seed(consumer_id="victim", name="victim", key_hash=hash_api_key("k2"))
    victim.scopes = [ConsumerScope(id="s-victim", project_id="proj-a", app_id=None)]
    with make_client(repo) as client:
        assert client.delete("/v1/admin/consumers/victim").status_code == 204
        assert "victim" in repo.rows
        assert repo.rows["victim"].deleted_at is not None
        assert repo.rows["victim"].revoked_at is not None
        assert repo.rows["victim"].enabled is False
        assert repo.rows["victim"].updated_by == ADMIN.consumer_id
        assert client.get("/v1/admin/consumers/victim").status_code == 404
        assert [row["id"] for row in client.get("/v1/admin/consumers").json()] == []
        assert client.delete("/v1/admin/consumers/victim").status_code == 404
    assert len(repo.deletion_records) == 1
    record = repo.deletion_records[0]
    assert record.consumer_id == "victim"
    assert record.deleted_by == ADMIN.consumer_id
    assert record.scopes == {"scopes": [{"project_id": "proj-a", "app_id": None}]}
    assert [(event.action, event.resource_id) for event in audit_events] == [
        ("consumer.delete", "victim")
    ]
    assert repo.for_update_calls == ["victim", "victim"]


def test_consumer_not_found_does_not_reflect_credential_shaped_id() -> None:
    canary = "Bearer-secret-canary"
    with make_client(FakeConsumersRepository()) as client:
        response = client.get(f"/v1/admin/consumers/{canary}")

    assert response.status_code == 404
    assert response.json()["title"] == "consumer not found"
    assert canary not in response.text


def test_get_unknown_consumer_404() -> None:
    with make_client(FakeConsumersRepository()) as client:
        assert client.get("/v1/admin/consumers/nope").status_code == 404


def test_all_routes_are_admin_only() -> None:
    repo = FakeConsumersRepository()
    repo.seed(consumer_id="x", name="x", key_hash=hash_api_key("k3"))
    with make_client(repo, identity=OPERATOR) as client:
        assert client.get("/v1/admin/consumers").status_code == 403
        assert client.post("/v1/admin/consumers", json=CREATE_BODY).status_code == 403
        assert client.get("/v1/admin/consumers/x").status_code == 403
        assert client.patch("/v1/admin/consumers/x", json={"enabled": False}).status_code == 403
        assert client.delete("/v1/admin/consumers/x").status_code == 403
        assert client.post("/v1/admin/consumers/x/rotate").status_code == 403
