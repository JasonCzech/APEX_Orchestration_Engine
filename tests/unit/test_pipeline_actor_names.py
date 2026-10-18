"""Run actor names require an authorized snapshot and only expose recorded principals."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import ApiConsumer, Base
from apex.services.pipeline_actors import pipeline_actor_names


class AsyncFacade:
    def __init__(self, session: Session):
        self.session = session

    async def execute(self, statement: Any):
        return self.session.execute(statement)


def consumer(scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="current",
        name="Current operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=scopes or [ScopeRef(project_id="p1", app_id="a1")],
    )


def snapshot(actors: list[str], *, project: str = "p1", app: str = "a1") -> dict[str, Any]:
    return {
        "project_id": project,
        "app_id": app,
        "values": {
            "phase_results": {"execution": {"approvals": [{"actor": actor} for actor in actors]}}
        },
    }


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.api_consumers"].create(connection)
    with Session(engine) as session:
        session.add_all(
            [
                ApiConsumer(
                    id="author",
                    name="Original author",
                    key_hash="a" * 64,
                    consumer_type="dashboard",
                    role="operator",
                ),
                ApiConsumer(
                    id="outsider",
                    name="Private outsider",
                    key_hash="b" * 64,
                    consumer_type="dashboard",
                    role="admin",
                ),
                ApiConsumer(
                    id="deleted",
                    name="Deleted author",
                    key_hash="c" * 64,
                    consumer_type="dashboard",
                    role="operator",
                    deleted_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()
    queries: list[str] = []
    event.listen(
        engine, "before_cursor_execute", lambda _c, _u, statement, *_args: queries.append(statement)
    )
    try:
        with Session(engine) as session:
            yield cast(AsyncSession, AsyncFacade(session)), queries
    finally:
        engine.dispose()


async def test_names_only_include_recorded_live_consumers_and_current_identity(database):
    session, _ = database
    result = await pipeline_actor_names(
        snapshot(["author", "deleted", "missing", "current"]), session, consumer()
    )
    assert result.names == {"author": "Original author", "current": "Current operator"}
    assert not result.truncated


@pytest.mark.parametrize("project,app", [("p2", "a1"), ("p1", "a2")])
async def test_cross_scope_fails_before_querying_directory(database, project, app):
    session, queries = database
    with pytest.raises(HTTPException) as error:
        await pipeline_actor_names(
            snapshot(["outsider"], project=project, app=app), session, consumer()
        )
    assert error.value.status_code == 404
    assert not queries


async def test_lookup_is_bounded_and_signals_omission(database):
    session, queries = database
    result = await pipeline_actor_names(
        snapshot(["author", *[f"missing-{i}" for i in range(300)]]), session, consumer()
    )
    assert result.truncated
    assert result.names == {}
    assert len(queries) == 1
    assert queries[0].count("?") == 256


async def test_empty_or_malformed_approval_history_does_not_query_directory(database):
    session, queries = database
    value = snapshot([])
    value["values"]["phase_results"]["reporting"] = {"approvals": {"actor": "outsider"}}
    result = await pipeline_actor_names(value, session, consumer())
    assert result.names == {}
    assert not queries


async def test_scoped_actor_endpoint_reads_the_run_before_the_directory(database):
    import httpx
    from fastapi import FastAPI
    from langgraph_sdk.errors import NotFoundError

    from apex.app.dependencies import get_current_identity
    from apex.persistence.db import get_session
    from apex.routers.pipelines import get_pipeline_read_service, router

    session, queries = database
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_identity] = consumer
    app.dependency_overrides[get_session] = lambda: session

    class ScopedRead:
        async def get_pipeline(self, thread_id: str):
            if thread_id != "permitted":
                raise NotFoundError(
                    "Not found",
                    response=httpx.Response(
                        404, request=httpx.Request("GET", "http://test/thread")
                    ),
                    body=None,
                )
            return snapshot(["author"])

    app.dependency_overrides[get_pipeline_read_service] = ScopedRead
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/pipelines/unknown/actors")
        assert response.status_code == 404
        assert not queries
        response = await client.get("/pipelines/permitted/actors")
        assert response.status_code == 200
        assert response.json() == {"names": {"author": "Original author"}, "truncated": False}
