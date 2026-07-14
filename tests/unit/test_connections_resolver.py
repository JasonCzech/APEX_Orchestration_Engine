"""DB-backed ConnectionResolver: precedence, scope checks, caching, fallbacks."""

import asyncio
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.services import connections as connections_service
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


async def test_db_error_fails_closed_in_locked_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([stored("global-wt")])
    store.error = SQLAlchemyError("connection refused")
    monkeypatch.setattr(
        connections_service, "get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    resolver = ConnectionResolver(store=store)

    with pytest.raises(RuntimeError, match="connection store unavailable"):
        await resolver.resolve(PortKind.WORK_TRACKING)


async def test_empty_store_does_not_fall_back_in_locked_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connections_service, "get_settings", lambda: SimpleNamespace(is_locked_down=True)
    )
    resolver = ConnectionResolver(store=FakeStore())

    with pytest.raises(RuntimeError, match="static development fallbacks are disabled"):
        await resolver.resolve(PortKind.WORK_TRACKING)

    # An explicitly constructed non-DB resolver remains available to tests and
    # local embedded use even when settings are patched to a locked environment.
    assert _conn_id(await ConnectionResolver().resolve(PortKind.WORK_TRACKING)) == (
        "dev-work-tracking-stub"
    )


async def test_resolve_with_connection_id_merges_options_and_checks_provider() -> None:
    row = stored("engine-a", kind=PortKind.EXECUTION_ENGINE, provider="sim")
    row = StoredConnection(
        config=row.config.model_copy(update={"options": {"base": "kept"}}),
        project_id=row.project_id,
        enabled=row.enabled,
        updated_at=row.updated_at,
    )
    resolver = ConnectionResolver(store=FakeStore([row]))

    base = await resolver.resolve(PortKind.EXECUTION_ENGINE)
    adapter, resolved_id = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 50.0},
    )
    second_overlay, _ = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        expected_provider="sim",
        options_overlay={"fail_at_pct": 75.0},
    )
    assert resolved_id == "engine-a"
    assert base._conn.options == {"base": "kept"}
    assert adapter._conn.options == {"base": "kept", "fail_at_pct": 50.0}
    assert second_overlay._conn.options == {"base": "kept", "fail_at_pct": 75.0}
    assert len({id(base), id(adapter), id(second_overlay)}) == 3

    with pytest.raises(ValueError, match="not requested provider"):
        await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE, expected_provider="loadrunner"
        )


@pytest.mark.parametrize("key", ["base_url", "endpoint", "_apex_trusted_private_host"])
async def test_options_overlay_cannot_change_target_or_trust_policy(key: str) -> None:
    row = stored("engine-a", kind=PortKind.EXECUTION_ENGINE, provider="sim")
    resolver = ConnectionResolver(store=FakeStore([row]))

    with pytest.raises(ValueError, match="cannot change network target or trust policy"):
        await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE,
            options_overlay={key: "https://attacker.example"},
        )


async def test_work_tracking_uses_internal_binding_not_external_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(
        store=FakeStore([stored("jira-p1", provider="jira", project_id="internal-project-1")])
    )

    async def jira(*args: object, **kwargs: object) -> object:
        # External Jira key intentionally differs from the owning APEX project.
        return SimpleNamespace(provider="jira", project_id="PHX")

    monkeypatch.setattr(resolver, "_build_cached", jira)
    adapter = await resolver.resolve(PortKind.WORK_TRACKING, project_id="internal-project-1")

    assert adapter.project_id == "PHX"
    assert adapter.apex_project_id == "internal-project-1"


async def test_global_real_work_tracking_connection_rejected_for_scoped_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(store=FakeStore([stored("global-jira", provider="jira")]))
    built = False

    async def jira(*args: object, **kwargs: object) -> object:
        nonlocal built
        built = True
        return SimpleNamespace(provider="jira", project_id="PHX")

    monkeypatch.setattr(resolver, "_build_cached", jira)
    with pytest.raises(ValueError, match="not internally bound"):
        await resolver.resolve(PortKind.WORK_TRACKING, project_id="internal-project-1")

    assert built is False


async def test_global_stub_work_tracking_connection_allowed_for_scoped_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ConnectionResolver(store=FakeStore([stored("global-wt")]))

    async def stub(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(provider="stub")

    monkeypatch.setattr(resolver, "_build_cached", stub)
    assert await resolver.resolve(PortKind.WORK_TRACKING, project_id="project-a")


# ── scope / state checks on explicit connection_id ───────────────────────────


async def test_explicit_id_scoped_to_other_project_raises() -> None:
    store = FakeStore([stored("other-wt", project_id="other")])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="scoped to project 'other'"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="other-wt", project_id="demo")


async def test_explicit_id_scoped_project_requires_project_context() -> None:
    store = FakeStore([stored("demo-wt", project_id="demo")])
    resolver = ConnectionResolver(store=store)
    with pytest.raises(ValueError, match="not None"):
        await resolver.resolve(PortKind.WORK_TRACKING, connection_id="demo-wt")


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


async def test_runtime_rejects_private_s3_endpoint_before_adapter_build() -> None:
    config = ConnectionConfig(
        id="private-store",
        kind=PortKind.ARTIFACT_STORE,
        provider="s3",
        name="private-store",
        options={"endpoint": "http://169.254.169.254/latest/meta-data"},
    )
    resolver = ConnectionResolver(connections=[config])

    with pytest.raises(ValueError, match="private adapter hosts are disabled"):
        await resolver.resolve(PortKind.ARTIFACT_STORE)


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


@pytest.fixture
def closeable_provider() -> Iterator[tuple[str, list[str]]]:
    provider = "test-closeable"
    closed: list[str] = []

    class CloseableAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.conn = conn

        async def aclose(self) -> None:
            closed.append(self.conn.id)

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(CloseableAdapter)
    try:
        yield provider, closed
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)


async def test_cache_invalidation_closes_replaced_adapter(
    closeable_provider: tuple[str, list[str]],
) -> None:
    provider, closed = closeable_provider
    store = FakeStore(
        [stored("global-wt", provider=provider, updated_at=datetime(2026, 1, 1, tzinfo=UTC))]
    )
    resolver = ConnectionResolver(store=store)

    first = await resolver.resolve(PortKind.WORK_TRACKING)
    store.rows["global-wt"] = stored(
        "global-wt", provider=provider, updated_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    second = await resolver.resolve(PortKind.WORK_TRACKING)

    assert second is not first
    assert closed == ["global-wt"]


async def test_resolver_close_closes_cached_adapters(
    closeable_provider: tuple[str, list[str]],
) -> None:
    provider, closed = closeable_provider
    store = FakeStore([stored("global-wt", provider=provider)])
    resolver = ConnectionResolver(store=store)

    await resolver.resolve(PortKind.WORK_TRACKING)
    await resolver.close()

    assert closed == ["global-wt"]


def test_shared_resolver_isolates_and_closes_adapters_per_event_loop() -> None:
    provider = "test-multiloop"
    rendezvous = threading.Barrier(2)
    resolver = ConnectionResolver(
        connections=[
            ConnectionConfig(
                id="multi-loop",
                kind=PortKind.WORK_TRACKING,
                provider=provider,
                name="multi-loop",
            )
        ]
    )

    class LoopOwnedAdapter:
        def __init__(self, conn: ConnectionConfig, secret: object | None = None) -> None:
            self.created_on = id(asyncio.get_running_loop())
            self.closed_on: int | None = None

        async def aclose(self) -> None:
            self.closed_on = id(asyncio.get_running_loop())

    AdapterRegistry.register(PortKind.WORK_TRACKING, provider)(LoopOwnedAdapter)

    async def use_from_one_loop() -> LoopOwnedAdapter:
        rendezvous.wait(timeout=5)
        first = await resolver.resolve(PortKind.WORK_TRACKING)
        second = await resolver.resolve(PortKind.WORK_TRACKING)
        assert first is second
        await resolver.close()
        return first

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(asyncio.run, use_from_one_loop()) for _ in range(2)]
            adapters = [future.result(timeout=10) for future in futures]
    finally:
        AdapterRegistry._factories.pop((PortKind.WORK_TRACKING, provider), None)

    assert adapters[0] is not adapters[1]
    assert len({adapter.created_on for adapter in adapters}) == 2
    assert all(adapter.closed_on == adapter.created_on for adapter in adapters)
