"""Repository conflict translation must not retain raw driver diagnostics."""

from collections.abc import Callable
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from apex.persistence.repositories.catalog import (
    CatalogRepository,
    DuplicateNameError,
    _is_duplicate_catalog_name,
)
from apex.persistence.repositories.connections import (
    ConnectionsRepository,
    DuplicateConnectionNameError,
    _is_duplicate_connection_name,
)
from apex.persistence.repositories.consumers import _is_duplicate_consumer_name
from apex.persistence.repositories.prompts import _is_duplicate_prompt_key
from apex.persistence.repositories.saved_queries import _is_duplicate_saved_query_name


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError(
        "INSERT",
        {},
        Exception(
            "CANARY-DB-DETAIL caller-value: duplicate key violates unique constraint "
            f'"{constraint}"'
        ),
    )


@pytest.mark.parametrize(
    ("classifier", "constraint"),
    [
        (_is_duplicate_connection_name, "uq_connections_name"),
        (_is_duplicate_catalog_name, "uq_applications_project_id"),
        (_is_duplicate_prompt_key, "uq_prompts_namespace"),
        (_is_duplicate_saved_query_name, "uq_saved_queries_global_name"),
        (_is_duplicate_consumer_name, "uq_api_consumers_name"),
    ],
)
def test_duplicate_classifiers_use_only_bounded_exact_driver_arguments(
    classifier: Callable[[IntegrityError], bool],
    constraint: str,
) -> None:
    class HostileDriverError(Exception):
        stringified = False

        def __str__(self) -> str:
            self.stringified = True
            raise AssertionError("driver exception must not be stringified")

    original = HostileDriverError(
        f"duplicate key violates unique constraint {constraint}" + ("x" * 1_000_000)
    )
    error = IntegrityError("INSERT", {}, Exception("placeholder"))
    error.orig = original

    assert classifier(error) is True
    assert original.stringified is False


async def test_connection_duplicate_is_fixed_and_drops_driver_context() -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=_integrity_error("uq_connections_name"))
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()

    with pytest.raises(DuplicateConnectionNameError) as raised:
        await ConnectionsRepository(session).create(
            kind="execution_engine",
            provider="simulated",
            name="CANARY-CALLER-NAME",
        )

    assert str(raised.value) == "connection name already exists"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "CANARY" not in str(raised.value)
    session.rollback.assert_awaited_once()
    session.refresh.assert_not_awaited()


async def test_unrelated_connection_integrity_error_is_not_mislabeled() -> None:
    error = _integrity_error("fk_connections_unrelated")
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as raised:
        await ConnectionsRepository(session).create(
            kind="execution_engine",
            provider="simulated",
            name="unique",
        )

    assert raised.value is error
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize(
    "constraint",
    ["uq_applications_project_id", "uq_environments_application_id"],
)
async def test_catalog_duplicate_is_fixed_and_drops_driver_context(constraint: str) -> None:
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=_integrity_error(constraint))
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()

    with pytest.raises(DuplicateNameError) as raised:
        await CatalogRepository(session).create_application(
            project_id="CANARY-CALLER-PROJECT",
            name="CANARY-CALLER-NAME",
        )

    assert str(raised.value) == "catalog name already exists"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "CANARY" not in str(raised.value)
    session.rollback.assert_awaited_once()
    session.refresh.assert_not_awaited()


async def test_unrelated_catalog_integrity_error_is_not_mislabeled() -> None:
    error = _integrity_error("fk_applications_unrelated")
    session = Mock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as raised:
        await CatalogRepository(session).create_application(project_id="project", name="unique")

    assert raised.value is error
    session.rollback.assert_awaited_once()
