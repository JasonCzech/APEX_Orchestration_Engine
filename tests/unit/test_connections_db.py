"""Thin Postgres integration test for ConnectionsRepository (opt-in via env)."""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_connection_roundtrip_with_host_mappings() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.persistence.repositories.connections import ConnectionsRepository

    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    maker = async_sessionmaker(engine, expire_on_commit=False)
    name = f"it-conn-{uuid.uuid4().hex[:8]}"
    try:
        async with maker() as session:
            repo = ConnectionsRepository(session)
            conn = await repo.create(
                kind="work_tracking",
                provider="stub",
                name=name,
                project_id=None,
                secret_ref="env:IT_TOKEN",
            )
            assert conn.enabled is True
            first_updated_at = conn.updated_at

            conn = await repo.update(conn, {"options": {"x": 1}})
            assert conn.options == {"x": 1}
            assert conn.updated_at >= first_updated_at

            conn = await repo.replace_host_mappings(
                conn, [{"pattern": "*.local", "target": "10.0.0.1"}]
            )
            assert [m.target for m in conn.host_mappings] == ["10.0.0.1"]
            conn = await repo.replace_host_mappings(conn, [])
            assert conn.host_mappings == []

            conn = await repo.set_enabled(conn, False)
            assert conn.enabled is False

            listed = await repo.list_connections(kind="work_tracking")
            assert name in [c.name for c in listed]

            await repo.delete(conn)
            assert await repo.get(conn.id) is None
    finally:
        await engine.dispose()
