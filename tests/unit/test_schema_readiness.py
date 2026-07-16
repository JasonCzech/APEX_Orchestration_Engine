from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from apex.persistence import schema_readiness
from apex.persistence.migration_lineage import packaged_revision_lineage


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _Connection:
    def __init__(
        self,
        versions: list[str] | None = None,
        *,
        lineage: list[tuple[str, str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._versions = versions or []
        self._lineage = lineage if lineage is not None else _packaged_lineage()
        self._error = error

    async def execute(self, statement: Any) -> _Result:
        if self._error is not None:
            raise self._error
        if str(statement) == "SELECT version_num FROM apex.alembic_version":
            return _Result(self._versions)
        assert str(statement) == (
            "SELECT revision_num, parent_revision_num FROM apex.alembic_revision_lineage"
        )
        return _Result(self._lineage)


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Connection]:
        yield self._connection


def _packaged_lineage(*extra: tuple[str, str]) -> list[tuple[str, str]]:
    return sorted(
        packaged_revision_lineage(schema_readiness.packaged_schema_scripts()).union(extra)
    )


async def test_schema_readiness_accepts_exact_packaged_head() -> None:
    await schema_readiness.validate_schema_head(_Engine(_Connection(["0028"])))


async def test_schema_readiness_accepts_registered_descendant_for_safe_code_rollback() -> None:
    await schema_readiness.validate_schema_head(
        _Engine(
            _Connection(
                ["future-0029"],
                lineage=_packaged_lineage(("future-0029", "0028")),
            )
        )
    )


async def test_schema_readiness_accepts_registered_future_branches() -> None:
    await schema_readiness.validate_schema_head(
        _Engine(
            _Connection(
                ["future-a", "future-b"],
                lineage=_packaged_lineage(
                    ("future-a", "0028"),
                    ("future-b", "0028"),
                ),
            )
        )
    )


@pytest.mark.parametrize(
    ("versions", "extra_lineage"),
    [
        ([], []),
        (["0024"], []),
        (["0028", "unexpected-branch"], []),
        (["future-divergent"], [("future-divergent", "0024")]),
        (["future-incomplete"], [("future-incomplete", "missing-parent")]),
        (["0028", "future-0029"], [("future-0029", "0028")]),
        (
            ["future-0029", "future-0030"],
            [("future-0029", "0028"), ("future-0030", "future-0029")],
        ),
        (
            ["future-cycle-a"],
            [("future-cycle-a", "future-cycle-b"), ("future-cycle-b", "future-cycle-a")],
        ),
    ],
)
async def test_schema_readiness_rejects_missing_stale_or_divergent_heads(
    versions: list[str],
    extra_lineage: list[tuple[str, str]],
) -> None:
    with pytest.raises(schema_readiness.SchemaNotReadyError, match="packaged head is 0028"):
        await schema_readiness.validate_schema_head(
            _Engine(_Connection(versions, lineage=_packaged_lineage(*extra_lineage)))
        )


async def test_schema_readiness_rejects_mutated_known_lineage() -> None:
    lineage = [edge for edge in _packaged_lineage() if edge[0] != "0028"] + [("0028", "0010")]

    with pytest.raises(schema_readiness.SchemaNotReadyError, match="lineage could not be proven"):
        await schema_readiness.validate_schema_head(_Engine(_Connection(["0028"], lineage=lineage)))


async def test_schema_readiness_does_not_reflect_database_revision_labels() -> None:
    canary = "database-revision-secret-canary\nforged-log-line"

    with pytest.raises(schema_readiness.SchemaNotReadyError) as caught:
        await schema_readiness.validate_schema_head(_Engine(_Connection([canary])))

    assert canary not in str(caught.value)
    assert "forged-log-line" not in str(caught.value)


async def test_schema_readiness_sanitizes_database_failures() -> None:
    failure = OperationalError("SELECT secret", {"password": "do-not-leak"}, Exception("boom"))

    with pytest.raises(schema_readiness.SchemaNotReadyError) as caught:
        await schema_readiness.validate_schema_head(_Engine(_Connection(error=failure)))

    assert "do-not-leak" not in str(caught.value)
    assert "alembic upgrade head" in str(caught.value)


async def test_schema_readiness_sanitizes_engine_configuration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine() -> None:
        raise ValueError("postgresql://admin:do-not-leak@example.invalid/apex")

    monkeypatch.setattr(schema_readiness, "get_engine", fail_engine)

    with pytest.raises(schema_readiness.SchemaNotReadyError) as caught:
        await schema_readiness.validate_schema_head()

    assert "do-not-leak" not in str(caught.value)
    assert "schema is unavailable" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_packaged_schema_head_tracks_latest_revision() -> None:
    schema_readiness.packaged_schema_scripts.cache_clear()
    schema_readiness.packaged_schema_heads.cache_clear()

    assert schema_readiness.packaged_schema_heads() == frozenset({"0028"})
