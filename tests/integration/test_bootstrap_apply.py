"""apply_document against a real Postgres (opt-in via APEX_TEST_DATABASE_URI).

Mirrors the project's integration style: skipped unless a Postgres URI is set
(the CI `integration` job provides one). Creates the apex schema + tables with
metadata.create_all (idempotent next to Alembic-migrated tables), applies a
document twice to prove idempotency, and asserts admin-key hashing + the two
input-error paths. Test rows use a dedicated project id and are cleaned up.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.service import hash_api_key
from apex.bootstrap import BootstrapDocument, apply_document
from apex.bootstrap.runner import BootstrapError
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Base,
    Connection,
    Environment,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"),
    reason="needs postgres (set APEX_TEST_DATABASE_URI)",
)

TEST_PROJECT = "bootstrap-it"
ADMIN_NAME = "it-admin"
CONNECTION_NAME = "it-artifacts"

DOC = {
    "applications": [{"project_id": TEST_PROJECT, "name": "Checkout"}],
    "environments": [
        {
            "project_id": TEST_PROJECT,
            "application": "Checkout",
            "name": "staging-it",
            "kind": "k8s",
            "hosts": [{"hostname": "host-01", "role": "app"}],
        }
    ],
    "connections": [
        {
            "name": CONNECTION_NAME,
            "kind": "artifact_store",
            "provider": "s3",
            "options": {"endpoint": "minio:9000"},
            "secret_ref": "env:APEX_MINIO_SECRET_KEY",
        }
    ],
    "admin": {"name": ADMIN_NAME, "key_env": "APEX_BOOTSTRAP_ADMIN_KEY"},
}


def _async_uri() -> str:
    uri = os.environ["APEX_TEST_DATABASE_URI"]
    if "+asyncpg" in uri:
        return uri
    return uri.replace("postgresql+psycopg", "postgresql").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(_async_uri())
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS apex"))
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with maker() as session:
            await session.execute(delete(ApiConsumer).where(ApiConsumer.name == ADMIN_NAME))
            await session.execute(delete(Connection).where(Connection.name == CONNECTION_NAME))
            await session.execute(delete(Application).where(Application.project_id == TEST_PROJECT))
            await session.commit()
        await engine.dispose()


async def test_apply_is_idempotent_and_hashes_admin_key(
    sessionmaker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APEX_BOOTSTRAP_ADMIN_KEY", "initial-admin-key")
    doc = BootstrapDocument.model_validate(DOC)

    async with sessionmaker() as session:
        first = await apply_document(doc, session, env=os.environ)
        await session.commit()
    assert first.applications_created == [f"{TEST_PROJECT}/Checkout"]
    assert first.connections_created == [CONNECTION_NAME]
    assert first.admin_created == ADMIN_NAME

    # Second run: everything already converged -> nothing created, admin unchanged.
    async with sessionmaker() as session:
        second = await apply_document(doc, session, env=os.environ)
        await session.commit()
    assert second.applications_created == []
    assert second.environments_created == []
    assert second.connections_created == []
    assert second.admin_created is None
    assert second.admin_existing == ADMIN_NAME

    async with sessionmaker() as session:
        consumer = await session.scalar(select(ApiConsumer).where(ApiConsumer.name == ADMIN_NAME))
        assert consumer is not None
        assert consumer.key_hash == hash_api_key("initial-admin-key")
        assert "initial-admin-key" not in consumer.key_hash  # stored hashed, not plaintext
        env_row = await session.scalar(select(Environment).where(Environment.name == "staging-it"))
        assert env_row is not None and len(env_row.hosts) == 1


async def test_environment_without_application_raises(sessionmaker: async_sessionmaker) -> None:
    doc = BootstrapDocument.model_validate(
        {"environments": [{"project_id": TEST_PROJECT, "application": "Ghost", "name": "e"}]}
    )
    async with sessionmaker() as session:
        with pytest.raises(BootstrapError, match="does not exist"):
            await apply_document(doc, session, env=os.environ)


async def test_admin_without_key_env_raises(
    sessionmaker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APEX_BOOTSTRAP_ADMIN_KEY", raising=False)
    doc = BootstrapDocument.model_validate({"admin": {"name": ADMIN_NAME}})
    async with sessionmaker() as session:
        with pytest.raises(BootstrapError, match="is unset/empty"):
            await apply_document(doc, session, env=os.environ)
