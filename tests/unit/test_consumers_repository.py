"""Unit-level transaction behavior for API-consumer persistence."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from apex.persistence.models import ApiConsumer, ConsumerKey
from apex.persistence.repositories.consumers import (
    ConsumersRepository,
    DuplicateConsumerNameError,
)
from apex.routers.consumers import _created_model


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError(
        "INSERT",
        {},
        Exception(f'duplicate key violates unique constraint "{constraint}"'),
    )


async def test_create_duplicate_name_rolls_back_and_raises_conflict() -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=_integrity_error("uq_api_consumers_name"))
    session.rollback = AsyncMock()

    with pytest.raises(DuplicateConsumerNameError) as raised:
        await ConsumersRepository(session).create(
            name="duplicate",
            consumer_type="headless",
            role="viewer",
            key_hash="a" * 64,
        )

    assert str(raised.value) == "consumer name already exists"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    session.rollback.assert_awaited_once()
    session.refresh.assert_not_called()


async def test_update_duplicate_name_rolls_back_and_raises_conflict() -> None:
    session = Mock()
    session.commit = AsyncMock(side_effect=_integrity_error("uq_api_consumers_name"))
    session.rollback = AsyncMock()
    consumer = ApiConsumer(
        id="consumer-1",
        name="old-name",
        consumer_type="headless",
        role="viewer",
        key_hash="b" * 64,
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = []

    with pytest.raises(DuplicateConsumerNameError):
        await ConsumersRepository(session).update_existing(consumer, name="duplicate")

    session.rollback.assert_awaited_once()


async def test_unrelated_consumer_integrity_error_is_not_mislabeled() -> None:
    error = _integrity_error("uq_consumer_keys_key_hash")
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as raised:
        await ConsumersRepository(session).create(
            name="unique-name",
            consumer_type="headless",
            role="viewer",
            key_hash="c" * 64,
        )

    assert raised.value is error
    session.rollback.assert_awaited_once()


async def test_create_does_not_depend_on_post_commit_refresh() -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=ConnectionError("refresh transport failed"))

    consumer = await ConsumersRepository(session).create(
        name="durable-create",
        consumer_type="headless",
        role="viewer",
        key_hash="9" * 64,
    )

    assert consumer.key_hash == "9" * 64
    session.refresh.assert_not_awaited()


async def test_create_resolves_lost_commit_acknowledgement_by_unique_key_hash() -> None:
    committed = ApiConsumer(
        id="consumer-committed",
        name="durable-create",
        consumer_type="headless",
        role="viewer",
        key_hash="8" * 64,
        enabled=True,
    )
    committed.scopes = []
    committed.keys = [ConsumerKey(key_hash="8" * 64)]
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=ConnectionError("commit acknowledgement lost"))
    session.rollback = AsyncMock()
    session.scalar = AsyncMock(return_value=committed)

    resolved = await ConsumersRepository(session).create(
        name="durable-create",
        consumer_type="headless",
        role="viewer",
        key_hash="8" * 64,
    )

    assert resolved is not None
    assert resolved is committed
    session.rollback.assert_awaited_once()
    session.scalar.assert_awaited_once()


async def test_update_does_not_misresolve_an_uncommitted_noncredential_change() -> None:
    session = Mock()
    session.commit = AsyncMock(side_effect=ConnectionError("commit failed"))
    session.scalar = AsyncMock()
    consumer = ApiConsumer(
        id="consumer-1",
        name="old-name",
        consumer_type="headless",
        role="viewer",
        key_hash="7" * 64,
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = []

    with pytest.raises(ConnectionError, match="commit failed"):
        await ConsumersRepository(session).update_existing(consumer, name="new-name")

    session.scalar.assert_not_awaited()


async def test_consumer_and_key_expiries_are_independent() -> None:
    consumer_expiry = datetime.now(UTC) + timedelta(days=1)
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    repository = ConsumersRepository(session)

    consumer = await repository.create(
        name="expiring-consumer",
        consumer_type="headless",
        role="viewer",
        key_hash="d" * 64,
        expires_at=consumer_expiry,
    )

    assert consumer.expires_at == consumer_expiry
    assert consumer.keys[0].expires_at is None

    repository.get_for_update = AsyncMock(return_value=consumer)  # type: ignore[method-assign]
    rotated = await repository.replace_key_hash(consumer.id, "e" * 64)
    assert rotated is not None
    assert rotated is consumer
    assert rotated.keys[-1].expires_at is None


async def test_expiry_update_repairs_only_provably_inherited_initial_key() -> None:
    old_expiry = datetime.now(UTC) + timedelta(days=1)
    new_expiry = datetime.now(UTC) + timedelta(days=2)
    session = Mock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    consumer = ApiConsumer(
        id="consumer-1",
        name="legacy",
        consumer_type="headless",
        role="viewer",
        key_hash="f" * 64,
        enabled=True,
        expires_at=old_expiry,
        rotation_count=0,
    )
    consumer.scopes = []
    consumer.keys = [ConsumerKey(key_hash="f" * 64, expires_at=old_expiry)]

    await ConsumersRepository(session).update_existing(
        consumer,
        expires_at=new_expiry,
        expires_at_set=True,
    )

    assert consumer.expires_at == new_expiry
    assert consumer.keys[0].expires_at is None


async def test_rotation_materializes_legacy_hash_before_applying_grace() -> None:
    session = Mock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    consumer = ApiConsumer(
        id="consumer-legacy",
        name="legacy",
        consumer_type="headless",
        role="viewer",
        key_hash="a" * 64,
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = []
    repository = ConsumersRepository(session)
    repository.get_for_update = AsyncMock(return_value=consumer)  # type: ignore[method-assign]
    grace = datetime.now(UTC) + timedelta(minutes=2)

    rotated = await repository.replace_key_hash(
        consumer.id,
        "b" * 64,
        grace_expires_at=grace,
    )

    assert rotated is consumer
    old = next(key for key in consumer.keys if key.key_hash == "a" * 64)
    assert old.revoked_at is None
    assert old.expires_at == grace


async def test_rotation_resolves_lost_commit_acknowledgement_by_new_key_hash() -> None:
    old_hash = "1" * 64
    new_hash = "2" * 64
    consumer = ApiConsumer(
        id="consumer-rotate",
        name="rotate",
        consumer_type="headless",
        role="viewer",
        key_hash=old_hash,
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = [ConsumerKey(key_hash=old_hash)]
    committed = ApiConsumer(
        id=consumer.id,
        name=consumer.name,
        consumer_type=consumer.consumer_type,
        role=consumer.role,
        key_hash=new_hash,
        enabled=True,
        rotation_count=1,
    )
    committed.scopes = []
    committed.keys = [ConsumerKey(key_hash=old_hash), ConsumerKey(key_hash=new_hash)]
    session = Mock()
    session.commit = AsyncMock(side_effect=ConnectionError("commit acknowledgement lost"))
    session.rollback = AsyncMock()
    session.scalar = AsyncMock(return_value=committed)
    repository = ConsumersRepository(session)
    repository.get_for_update = AsyncMock(return_value=consumer)  # type: ignore[method-assign]

    resolved = await repository.replace_key_hash(consumer.id, new_hash)

    assert resolved is not None
    assert resolved is committed
    assert resolved.key_hash == new_hash
    session.rollback.assert_awaited_once()


async def test_rotation_lost_ack_resolves_active_issued_key_after_later_rotation() -> None:
    old_hash = "1" * 64
    first_new_hash = "2" * 64
    later_new_hash = "3" * 64
    consumer = ApiConsumer(
        id="consumer-interleaved-rotate",
        name="interleaved",
        consumer_type="headless",
        role="viewer",
        key_hash=old_hash,
        enabled=True,
    )
    consumer.scopes = []
    consumer.keys = [ConsumerKey(key_hash=old_hash)]

    # Rotation two committed after rotation one's commit ACK was lost. Rotation
    # one's key remains grace-active in consumer_keys, but is no longer the
    # denormalized ApiConsumer.key_hash.
    committed = ApiConsumer(
        id=consumer.id,
        name=consumer.name,
        consumer_type=consumer.consumer_type,
        role=consumer.role,
        key_hash=later_new_hash,
        enabled=True,
        rotation_count=2,
    )
    committed.scopes = []
    committed.keys = [
        ConsumerKey(
            key_hash=first_new_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        ConsumerKey(key_hash=later_new_hash),
    ]
    session = Mock()
    session.commit = AsyncMock(side_effect=ConnectionError("commit acknowledgement lost"))
    session.rollback = AsyncMock()
    session.scalar = AsyncMock(return_value=committed)
    repository = ConsumersRepository(session)
    repository.get_for_update = AsyncMock(return_value=consumer)  # type: ignore[method-assign]

    resolved = await repository.replace_key_hash(consumer.id, first_new_hash)

    assert resolved is not None
    assert resolved is committed
    assert resolved.key_hash == later_new_hash
    response = _created_model(resolved, "first-rotation-plaintext")
    assert response.api_key == "first-rotation-plaintext"
    assert response.key_fingerprint == first_new_hash[:8]
    assert resolved.key_hash == later_new_hash
    statement = session.scalar.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN apex.consumer_keys" in sql
    assert "apex.consumer_keys.key_hash" in sql
    assert "apex.consumer_keys.revoked_at IS NULL" in sql
    assert "apex.consumer_keys.expires_at IS NULL" in sql
    assert "apex.api_consumers.id" in sql
