"""Unit-level transaction behavior for API-consumer persistence."""

from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from apex.persistence.models import ApiConsumer
from apex.persistence.repositories.consumers import (
    ConsumersRepository,
    DuplicateConsumerNameError,
)


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

    with pytest.raises(DuplicateConsumerNameError):
        await ConsumersRepository(session).create(
            name="duplicate",
            consumer_type="headless",
            role="viewer",
            key_hash="a" * 64,
        )

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
