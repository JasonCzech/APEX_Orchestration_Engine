"""ConnectionResolver: default DEV connections, generation caching, error paths."""

import pytest

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.adapters.stubs import StubWorkTrackingAdapter
from apex.ports import ExecutionEnginePort, SecretsPort, WorkTrackingPort
from apex.services.connections import (
    DEV_CONNECTIONS,
    ConnectionResolver,
    get_connection_resolver,
)


def test_dev_connections_cover_every_port_kind() -> None:
    assert set(DEV_CONNECTIONS) == set(PortKind)
    for kind, conn in DEV_CONNECTIONS.items():
        assert conn.kind is kind


async def test_resolve_default_builds_expected_adapters() -> None:
    resolver = ConnectionResolver()
    work = await resolver.resolve(PortKind.WORK_TRACKING)
    assert isinstance(work._managed.adapter, StubWorkTrackingAdapter)
    assert isinstance(work, StubWorkTrackingAdapter)
    assert isinstance(work, WorkTrackingPort)

    engine = await resolver.resolve(PortKind.EXECUTION_ENGINE)
    assert isinstance(engine._managed.adapter, SimExecutionEngine)
    assert isinstance(engine, SimExecutionEngine)
    assert isinstance(engine, ExecutionEnginePort)

    secrets = await resolver.resolve(PortKind.SECRETS)
    assert isinstance(secrets, SecretsPort)


async def test_resolve_caches_instances_per_connection_id() -> None:
    resolver = ConnectionResolver()
    first = await resolver.resolve(PortKind.WORK_TRACKING)
    second = await resolver.resolve(PortKind.WORK_TRACKING)
    by_id = await resolver.resolve(PortKind.WORK_TRACKING, "dev-work-tracking-stub")
    # Each caller receives its own lifetime lease, while the underlying adapter
    # generation remains cached until configuration rotation or resolver close.
    assert first is not second
    assert second is not by_id
    assert first._managed is second._managed is by_id._managed


async def test_resolve_unknown_connection_id_raises() -> None:
    resolver = ConnectionResolver()
    with pytest.raises(KeyError, match="no-such-conn"):
        await resolver.resolve(PortKind.WORK_TRACKING, "no-such-conn")


async def test_resolve_kind_mismatch_raises() -> None:
    resolver = ConnectionResolver()
    with pytest.raises(ValueError, match="dev-log-search-stub"):
        await resolver.resolve(PortKind.WORK_TRACKING, "dev-log-search-stub")


async def test_resolver_builds_secretful_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection with secret_ref gets it resolved through the secrets connection."""
    monkeypatch.setenv("APEX_INTEGRATION_TEST_WT_TOKEN", "tok-123")
    custom = ConnectionConfig(
        id="wt-with-secret",
        kind=PortKind.WORK_TRACKING,
        provider="stub",
        name="Stub with secret",
        secret_ref="env:APEX_INTEGRATION_TEST_WT_TOKEN",
    )
    resolver = ConnectionResolver([custom, DEV_CONNECTIONS[PortKind.SECRETS]])
    adapter = await resolver.resolve(PortKind.WORK_TRACKING, "wt-with-secret")
    assert isinstance(adapter._managed.adapter, StubWorkTrackingAdapter)
    assert isinstance(adapter, StubWorkTrackingAdapter)


def test_get_connection_resolver_is_singleton() -> None:
    assert get_connection_resolver() is get_connection_resolver()
