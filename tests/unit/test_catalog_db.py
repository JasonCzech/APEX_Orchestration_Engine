"""Thin Postgres integration test for CatalogRepository (opt-in via env)."""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


async def test_application_environment_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.persistence.repositories.catalog import CatalogRepository

    engine = create_async_engine(os.environ["APEX_TEST_DATABASE_URI"])
    maker = async_sessionmaker(engine, expire_on_commit=False)
    project = f"it-{uuid.uuid4().hex[:8]}"
    try:
        async with maker() as session:
            repo = CatalogRepository(session)
            app = await repo.create_application(project_id=project, name="it-app")
            assert app.created_at is not None

            env = await repo.create_environment(
                application_id=app.id,
                name="it-env",
                kind="vm",
                hosts=[{"hostname": "h1.local", "role": "app"}, {"hostname": "h2.local"}],
            )
            assert [h.hostname for h in env.hosts] == ["h1.local", "h2.local"]

            fetched = await repo.get_environment(env.id)
            assert fetched is not None
            assert fetched.application.project_id == project

            env = await repo.update_environment(
                fetched, {"name": "it-env-2"}, hosts=[{"hostname": "h3.local"}]
            )
            assert env.name == "it-env-2"
            assert [h.hostname for h in env.hosts] == ["h3.local"]

            listed = await repo.list_applications(visible_projects=[project])
            assert [a.id for a in listed] == [app.id]

            await repo.delete_application(app)  # cascades the environment
            assert await repo.get_environment(env.id) is None
    finally:
        await engine.dispose()
