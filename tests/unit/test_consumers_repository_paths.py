"""Database-backed aggregate and authorization paths for API consumers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.auth.identity import ConsumerType, Role, ScopeRef
from apex.domain.input_limits import MAX_CHILD_ITEMS
from apex.persistence.models import (
    ApiConsumer,
    Base,
    ConsumerDeletionRecord,
    ConsumerKey,
    ConsumerScope,
)
from apex.persistence.repositories import consumers as consumers_module
from apex.persistence.repositories.consumers import (
    AmbiguousConsumerKeyExpiryError,
    ConsumersRepository,
    consume_credential_response_key_hash,
)


class _AsyncFacade:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    async def get(self, model: Any, key: Any) -> Any:
        return self._session.get(model, key)

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._session.scalars(statement)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()


async def test_consumers_crud_scoping_rotation_and_soft_delete() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for table in (
            "apex.api_consumers",
            "apex.consumer_scopes",
            "apex.consumer_keys",
            "apex.consumer_deletion_records",
        ):
            Base.metadata.tables[table].create(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = ConsumersRepository(cast(AsyncSession, _AsyncFacade(session)))
            app_consumer = await repository.create(
                name="app-consumer",
                consumer_type="headless",
                role="viewer",
                key_hash="a" * 64,
                scopes=[ScopeRef(project_id="project-1", app_id="app-a")],
                created_by="admin",
            )
            project_consumer = await repository.create(
                name="project-consumer",
                consumer_type="dashboard",
                role="operator",
                key_hash="b" * 64,
                scopes=[ScopeRef(project_id="project-1")],
            )
            unscoped = await repository.create(
                name="unscoped",
                consumer_type="internal",
                role="admin",
                key_hash="c" * 64,
            )
            mixed = await repository.create(
                name="mixed",
                consumer_type="headless",
                role="viewer",
                key_hash="d" * 64,
                scopes=[
                    ScopeRef(project_id="project-1", app_id="app-a"),
                    ScopeRef(project_id="project-2", app_id="app-b"),
                ],
            )

            assert {row.id for row in await repository.list_all()} == {
                app_consumer.id,
                project_consumer.id,
                unscoped.id,
                mixed.id,
            }
            assert await repository.list_all(allowed_scopes=[]) == []
            assert [
                row.id
                for row in await repository.list_all(
                    allowed_scopes=[ScopeRef(project_id="project-1", app_id="app-a")]
                )
            ] == [app_consumer.id]
            assert {
                row.id
                for row in await repository.list_all(
                    allowed_scopes=[ScopeRef(project_id="project-1")]
                )
            } == {app_consumer.id, project_consumer.id}

            assert await repository.get(app_consumer.id) is app_consumer
            assert await repository.get("missing") is None
            assert (await repository.get_for_update(app_consumer.id)).id == app_consumer.id  # type: ignore[union-attr]
            assert await repository.get_for_update("missing") is None
            assert (await repository.get_by_name("app-consumer")).id == app_consumer.id  # type: ignore[union-attr]

            updated = await repository.update(
                app_consumer.id,
                name="renamed",
                role="operator",
                enabled=False,
                scopes=[ScopeRef(project_id="project-1")],
                expires_at=datetime.now(UTC) + timedelta(days=1),
                expires_at_set=True,
                revoked_at=datetime.now(UTC),
                revoked_at_set=True,
                updated_by="updater",
            )
            assert updated is not None
            assert (updated.name, updated.role, updated.enabled, updated.updated_by) == (
                "renamed",
                "operator",
                False,
                "updater",
            )
            assert [(scope.project_id, scope.app_id) for scope in updated.scopes] == [
                ("project-1", None)
            ]
            assert await repository.update("missing", role="admin") is None

            rotated = await repository.replace_key_hash(
                project_consumer.id,
                "e" * 64,
                rotated_by="rotator",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            assert rotated is not None
            assert rotated.key_hash == "e" * 64
            assert rotated.updated_by == "rotator"
            assert rotated.keys[0].revoked_at is not None
            assert rotated.keys[-1].expiry_source == "explicit"
            assert await repository.replace_key_hash("missing", "f" * 64) is None

            assert await repository.delete("missing") is False
            assert await repository.delete(project_consumer.id, deleted_by="deleter") is True
            assert await repository.get(project_consumer.id) is None
            assert await repository.get_for_update(project_consumer.id) is None
            assert await repository.delete_existing(project_consumer) is False
            tombstone = session.scalar(
                select(ConsumerDeletionRecord).where(
                    ConsumerDeletionRecord.consumer_id == project_consumer.id
                )
            )
            assert tombstone is not None
            assert tombstone.deleted_by == "deleter"
            assert tombstone.scopes == {"scopes": [{"project_id": "project-1", "app_id": None}]}
    finally:
        engine.dispose()


async def test_app_only_delegation_excludes_project_wide_null_scope() -> None:
    """SQL NULL must not turn a wider project scope into an allowed app scope."""

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for table in (
            "apex.api_consumers",
            "apex.consumer_scopes",
            "apex.consumer_keys",
        ):
            Base.metadata.tables[table].create(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            repository = ConsumersRepository(cast(AsyncSession, _AsyncFacade(session)))
            app_consumer = await repository.create(
                name="app-target",
                consumer_type="headless",
                role="viewer",
                key_hash="1" * 64,
                scopes=[ScopeRef(project_id="project-1", app_id="app-a")],
            )
            await repository.create(
                name="project-wide-target",
                consumer_type="headless",
                role="viewer",
                key_hash="2" * 64,
                scopes=[ScopeRef(project_id="project-1")],
            )

            visible = await repository.list_all(
                allowed_scopes=[ScopeRef(project_id="project-1", app_id="app-a")]
            )

            assert [consumer.id for consumer in visible] == [app_consumer.id]
    finally:
        engine.dispose()


async def test_legacy_rotated_key_expiry_requires_an_explicit_rotation() -> None:
    old_expiry = datetime.now(UTC) + timedelta(days=1)
    consumer = ApiConsumer(
        id="consumer-1",
        name="ambiguous",
        consumer_type="headless",
        role="viewer",
        key_hash="a" * 64,
        enabled=True,
        expires_at=old_expiry,
        rotation_count=1,
    )
    consumer.scopes = []
    consumer.keys = [
        ConsumerKey(
            key_hash=consumer.key_hash,
            expires_at=old_expiry,
            expiry_source="legacy_ambiguous",
        )
    ]
    session = Mock()
    session.commit = AsyncMock()

    with pytest.raises(AmbiguousConsumerKeyExpiryError, match="rotate"):
        await ConsumersRepository(session).update_existing(
            consumer,
            expires_at=old_expiry + timedelta(days=1),
            expires_at_set=True,
        )
    session.commit.assert_not_awaited()


async def test_credential_commit_resolution_fails_closed_on_read_error() -> None:
    session = Mock()
    session.rollback = AsyncMock(side_effect=ConnectionError("database unavailable"))
    repository = ConsumersRepository(session)

    assert (
        await repository._resolve_credential_commit(
            "a" * 64,
            expected_consumer_id="consumer-1",
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"),
        ("name", "x" * 256),
        ("created_by", 1),
        ("created_by", "bad\x00actor"),
    ],
)
async def test_consumer_create_rejects_unsafe_metadata_before_io(field: str, value: Any) -> None:
    session = Mock()
    arguments: dict[str, Any] = {
        "name": "consumer",
        "consumer_type": "headless",
        "role": "viewer",
        "key_hash": "a" * 64,
        "created_by": "admin",
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="consumer"):
        await ConsumersRepository(session).create(**arguments)

    session.add.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consumer_type", "service"),
        ("consumer_type", ConsumerType.HEADLESS),
        ("consumer_type", 1),
        ("role", "owner"),
        ("role", Role.ADMIN),
        ("role", False),
        ("expires_at", datetime.now()),
        ("expires_at", "2026-01-01T00:00:00Z"),
    ],
)
async def test_consumer_create_requires_exact_enum_and_lifecycle_values_before_io(
    field: str,
    value: Any,
) -> None:
    session = Mock()
    arguments: dict[str, Any] = {
        "name": "consumer",
        "consumer_type": "headless",
        "role": "viewer",
        "key_hash": "a" * 64,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="consumer"):
        await ConsumersRepository(session).create(**arguments)

    session.add.assert_not_called()


@pytest.mark.parametrize(
    "scopes",
    [
        object(),
        [ScopeRef(project_id="project-1"), ScopeRef(project_id="project-1")],
        [
            ScopeRef(project_id="project-1"),
            ScopeRef(project_id="project-1", app_id="app-1"),
        ],
        [ScopeRef.model_construct(project_id=None, app_id=None)],
        [ScopeRef.model_construct(project_id=" project-1", app_id=None)],
        [
            ScopeRef.model_construct(
                project_id="sk-0123456789abcdefghijkl",
                app_id=None,
            )
        ],
        [ScopeRef.model_construct(project_id="project-1", app_id=1)],
        [ScopeRef(project_id=f"project-{index}") for index in range(MAX_CHILD_ITEMS + 1)],
    ],
)
async def test_consumer_create_rejects_malformed_or_redundant_scopes_before_io(
    scopes: Any,
) -> None:
    session = Mock()

    with pytest.raises(ValueError, match="consumer"):
        await ConsumersRepository(session).create(
            name="consumer",
            consumer_type="headless",
            role="viewer",
            key_hash="a" * 64,
            scopes=scopes,
        )

    session.add.assert_not_called()


async def test_consumer_scope_validation_never_executes_hostile_sequence_hooks() -> None:
    class HostileScopes(list[Any]):
        called = False

        def __iter__(self) -> Any:
            self.called = True
            raise AssertionError("custom scope iteration must not execute")

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("custom scope length must not execute")

    scopes = HostileScopes([ScopeRef(project_id="project-1")])
    session = Mock()

    with pytest.raises(ValueError, match="consumer scopes"):
        await ConsumersRepository(session).create(
            name="consumer",
            consumer_type="headless",
            role="viewer",
            key_hash="a" * 64,
            scopes=scopes,
        )

    assert scopes.called is False
    session.add.assert_not_called()


async def test_consumer_updates_reject_unsafe_metadata_before_mutation() -> None:
    session = Mock()
    session.commit = AsyncMock()
    consumer = ApiConsumer(
        id="consumer-1",
        name="safe",
        consumer_type="headless",
        role="viewer",
        key_hash="a" * 64,
        enabled=True,
    )
    consumer.keys = []
    consumer.scopes = []
    repository = ConsumersRepository(session)
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(ValueError, match="credential material"):
        await repository.update_existing(consumer, name=credential)
    with pytest.raises(ValueError, match="credential material"):
        await repository.update_existing(consumer, updated_by=credential)

    assert consumer.name == "safe"
    assert consumer.updated_by is None
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "owner"},
        {"role": Role.ADMIN},
        {"enabled": 1},
        {"expires_at_set": 1},
        {"revoked_at_set": None},
        {"expires_at": datetime.now()},
        {"revoked_at": "2026-01-01T00:00:00Z"},
        {"scopes": [ScopeRef.model_construct(project_id=None, app_id=None)]},
    ],
)
async def test_consumer_update_rejects_invalid_direct_values_before_lookup(
    changes: dict[str, Any],
) -> None:
    session = Mock()
    repository = ConsumersRepository(session)

    with pytest.raises(ValueError, match="consumer"):
        await repository.update("consumer-1", **changes)

    session.scalars.assert_not_called()


async def test_consumer_update_existing_validates_every_value_before_mutation() -> None:
    consumer = ApiConsumer(
        id="consumer-1",
        name="safe",
        consumer_type="headless",
        role="viewer",
        key_hash="a" * 64,
        enabled=True,
    )
    consumer.keys = []
    consumer.scopes = []
    session = Mock()
    session.commit = AsyncMock()

    with pytest.raises(ValueError, match="consumer enabled"):
        await ConsumersRepository(session).update_existing(
            consumer,
            name="would-mutate",
            enabled=1,  # type: ignore[arg-type]
        )

    assert consumer.name == "safe"
    assert consumer.enabled is True
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("field", ["grace_expires_at", "expires_at"])
async def test_consumer_rotation_rejects_non_utc_lifecycle_values_before_lookup(
    field: str,
) -> None:
    session = Mock()

    with pytest.raises(ValueError, match="UTC-aware"):
        await ConsumersRepository(session).replace_key_hash(
            "consumer-1",
            "b" * 64,
            **{field: cast(Any, datetime.now())},
        )

    session.scalars.assert_not_called()


async def test_consumer_rotation_rejects_expiry_before_grace_deadline_before_lookup() -> None:
    session = Mock()
    grace_deadline = datetime.now(UTC) + timedelta(days=7)

    with pytest.raises(ValueError, match="later than.*grace"):
        await ConsumersRepository(session).replace_key_hash(
            "consumer-1",
            "b" * 64,
            grace_expires_at=grace_deadline,
            expires_at=grace_deadline - timedelta(days=1),
        )

    session.scalar.assert_not_called()


@pytest.mark.parametrize(
    "key_hash",
    ["raw-secret", "A" * 64, "a" * 63, "g" * 64, None],
)
async def test_consumer_key_writes_require_exact_lowercase_sha256(
    key_hash: Any,
) -> None:
    session = Mock()
    repository = ConsumersRepository(session)

    with pytest.raises(ValueError, match="lowercase hexadecimal digest"):
        await repository.create(
            name="consumer",
            consumer_type="headless",
            role="viewer",
            key_hash=key_hash,
        )
    with pytest.raises(ValueError, match="lowercase hexadecimal digest"):
        await repository.replace_key_hash("consumer-1", key_hash)

    session.add.assert_not_called()


async def test_consumer_key_digest_is_never_sent_to_generic_credential_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected: list[str] = []
    original = consumers_module.reject_credential_text

    def record_metadata(value: str | None, *, label: str) -> str | None:
        if value is not None:
            inspected.append(value)
        return original(value, label=label)

    monkeypatch.setattr(consumers_module, "reject_credential_text", record_metadata)
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock()
    digest = "a" * 64

    await ConsumersRepository(session).create(
        name="consumer",
        consumer_type="headless",
        role="viewer",
        key_hash=digest,
        created_by="admin",
    )

    assert digest not in inspected
    assert inspected == ["consumer", "admin"]


async def test_consumer_rotation_rejects_malformed_legacy_hash_before_copy() -> None:
    consumer = ApiConsumer(
        id="consumer-1",
        name="legacy",
        consumer_type="headless",
        role="viewer",
        key_hash="raw-legacy-secret",
        enabled=True,
    )
    consumer.keys = []
    consumer.scopes = []
    session = Mock()
    session.commit = AsyncMock()
    repository = ConsumersRepository(session)
    repository.get_for_update = AsyncMock(return_value=consumer)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="lowercase hexadecimal digest"):
        await repository.replace_key_hash("consumer-1", "b" * 64)

    assert consumer.keys == []
    assert consumer.key_hash == "raw-legacy-secret"
    session.commit.assert_not_awaited()


async def test_consumer_deletion_quarantines_legacy_secret_bearing_tombstone_text() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    credential_id = "password=legacy-id-secret"
    consumer = ApiConsumer(
        id=credential_id,
        name=credential,
        consumer_type="headless",
        role="viewer",
        key_hash="a" * 64,
        enabled=True,
    )
    consumer.keys = []
    consumer.scopes = [
        ConsumerScope(project_id=credential, app_id=credential),
        ConsumerScope(project_id="safe-project", app_id=None),
    ]
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock()

    assert await ConsumersRepository(session).delete_existing(
        consumer,
        deleted_by="admin",
    )

    record = session.add.call_args.args[0]
    assert isinstance(record, ConsumerDeletionRecord)
    assert record.consumer_id == "[REDACTED]"
    assert record.name == "[REDACTED]"
    assert record.deleted_by == "admin"
    assert record.scopes == {
        "scopes": [
            {"project_id": "[REDACTED]", "app_id": "[REDACTED]"},
            {"project_id": "safe-project", "app_id": None},
        ]
    }
    durable_values = {
        "consumer_id": record.consumer_id,
        "deleted_by": record.deleted_by,
        "name": record.name,
        "consumer_type": record.consumer_type,
        "role": record.role,
        "scopes": record.scopes,
    }
    assert credential not in repr(durable_values)
    assert credential_id not in repr(durable_values)


def test_credential_hash_is_consumed_once_without_mutating_current_key() -> None:
    consumer = ApiConsumer(
        id="consumer-1",
        name="resolved",
        consumer_type="headless",
        role="viewer",
        key_hash="current",
        enabled=True,
    )
    setattr(
        consumer,
        consumers_module._CREDENTIAL_RESPONSE_KEY_HASH_ATTRIBUTE,
        "issued",
    )
    assert consume_credential_response_key_hash(consumer) == "issued"
    assert consume_credential_response_key_hash(consumer) == "current"


def test_consumer_scope_and_duplicate_helpers() -> None:
    assert consumers_module._delegable_scope_predicate([]) is None
    assert (
        consumers_module._delegable_scope_predicate(
            [
                ScopeRef(project_id="project-1"),
                ScopeRef(project_id="project-1", app_id="covered"),
                ScopeRef(project_id="project-2", app_id="app-2"),
            ]
        )
        is not None
    )

    class DriverConstraintError(Exception):
        def __init__(self, constraint_name: str) -> None:
            super().__init__("duplicate consumer row")
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    original = DriverConstraintError("uq_api_consumers_name")
    error = consumers_module.IntegrityError("insert", {}, original)
    assert consumers_module._is_duplicate_consumer_name(error)
