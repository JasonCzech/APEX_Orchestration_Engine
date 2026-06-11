"""DB-backed ConnectionResolver: precedence, scope checks, caching, fallbacks."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.services.connections import ConnectionResolver, StoredConnection


def stored(
    connection_id: str,
    *,
    kind: PortKind = PortKind.WORK_TRACKING,
    provider: str = "stub",
    project_id: str | None = None,
    enabled: bool = True,
    updated_at: datetime | None = None,
) -> StoredConnection:
    return StoredConnection(
        config=ConnectionConfig(id=connection_id, kind=kind, provider=provider, name=connection_id),
        project_id=project_id,
        enabled=enabled,
        updated_at=updated_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


class FakeStore:
    """In-memory ConnectionStore mirroring DbConnectionStore selection rules."""

    def __init__(self, rows: list[StoredConnection] | None = None) -> None:
        self.rows: dict[str, StoredConnection] = {r.config.id: r for r in rows or []}
        self.error: Exception | None = None

    async def get(self, connection_id: str) -> StoredConnection | None:
        if self.error is not None:
            raise self.error
        return self.rows.get(connection_id)

    async def find_default(self, kind: PortKind, project_id: str | None) -> StoredConnection | None:
        if self.error is not None:
            raise self.error
        candidates = [r for r in self.rows.values() if r.config.kind is kind and r.enabled]
        if project_id is not None:
            scoped = [r for r in candidates if r.project_id == project_id]
            if scoped:
                return scoped[0]
        global_rows = [r for r in candidates if r.project_id is None]
        return global_rows[0] if global_rows else None


def _conn_id(adapter: object) -> str:
    """The stub adapters keep their ConnectionConfig on ._conn."""
    config = getattr(adapter, "_conn", None)
    assert config is not None
    return config.id


# ── precedence: connection_id > project > global > static ───────────────────


async def test_project_row_beats_global_row() -> None:
    store = FakeStore([stored("global-wt"), stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    assert _conn_id(await resolver.resolve(PortKind.WORK_TRACKING, project_id="demo")) == "demo-wt"
    assert _conn_id(await resolver.resolve(PortKind.WORK_TRACKING)) == "global-wt"


async def test_explicit_connection_id_beats_project_default() -> None:
    store = FakeStore([stored("global-wt"), stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    adapter = await resolver.resolve(
        PortKind.WORK_TRACKING, connection_id="global-wt", project_id="demo"
    )
    assert _conn_id(adapter) == "global-wt"  # global rows are usable from any project


async def test_no_db_row_falls_back_to_static_stub() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    adapter = await resolver.resolve(PortKind.WORK_TRACKING)
    assert _conn_id(adapter) == "dev-work-tracking-stub"


async def test_db_error_falls_back_to_static_stub() -> None:
    store = FakeStore([stored("global-wt")])
    store.error = SQLAlchemyError("connection refused")
    resolver = ConnectionResolver(store=store)
    adapter = await resolver.resolve(PortKind.WORK_TRACKING)
    assert _conn_id(adapter) == "dev-work-tracking-stub"


# ── scope / state checks on explicit connection_id ───────────────────────────


async def test_explicit_id_scoped_to_other_project_raises() -> None:
    store = FakeStore([stored("other-wt", project_id="other")])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="scoped to project 'other'"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="other-wt", project_id="demo")


async def test_explicit_id_disabled_raises() -> None:
    store = FakeStore([stored("off-wt", enabled=False)])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="disabled"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="off-wt")


async def test_explicit_id_kind_mismatch_raises() -> None:
    store = FakeStore([stored("logs", kind=PortKind.LOG_SEARCH)])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="kind"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="logs")


async def test_unknown_id_still_raises_keyerror() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    with pytest.raises(KeyError, match="no-such-conn"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="no-such-conn")


async def test_static_dev_id_resolves_even_with_store() -> None:
    resolver = ConnectionResolver(store=FakeStore())
    adapter = await resolver.resolve(PortKind.WORK_TRACKING, connection_id="dev-work-tracking-stub")
    assert _conn_id(adapter) == "dev-work-tracking-stub"


# ── caching keyed by (connection_id, updated_at) ─────────────────────────────


async def test_cache_hit_for_unchanged_row() -> None:
    store = FakeStore([stored("global-wt")])
    resolver = ConnectionResolver(store=store)
    first = await resolver.resolve(PortKind.WORK_TRACKING)
    second = await resolver.resolve(PortKind.WORK_TRACKING)
    assert first is second


async def test_cache_invalidated_when_updated_at_changes() -> None:
    store = FakeStore([stored("global-wt", updated_at=datetime(2026, 1, 1, tzinfo=UTC))])
    resolver = ConnectionResolver(store=store)
    before = await resolver.resolve(PortKind.WORK_TRACKING)

    # admin PATCH bumps updated_at -> next resolve rebuilds the adapter
    store.rows["global-wt"] = stored("global-wt", updated_at=datetime(2026, 1, 2, tzinfo=UTC))
    after = await resolver.resolve(PortKind.WORK_TRACKING)
    assert after is not before

    again = await resolver.resolve(PortKind.WORK_TRACKING)
    assert again is after  # and the new instance is cached


async def test_static_resolver_unchanged_without_store() -> None:
    resolver = ConnectionResolver()
    first = await resolver.resolve(PortKind.WORK_TRACKING)
    second = await resolver.resolve(PortKind.WORK_TRACKING, "dev-work-tracking-stub")
    assert first is second
