"""Combined-run authorization precedes pagination, count and stream admission."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import Table, delete, literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import JSONResponse

from apex.app import test_scope_security as middleware
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.test_scope import TestScopeV1
from apex.persistence.models import Base, RunParticipant, RunScopeBinding
from apex.services.test_scope import (
    can_access_thread,
    load_persisted_test_scope,
    participant_visibility,
    persist_test_scope,
)


def topology():
    return TestScopeV1.model_validate(
        {
            "participants": [
                {
                    "id": "front",
                    "project_id": "p1",
                    "application_id": "a1",
                    "environment_id": "e1",
                    "layer": "frontend",
                },
                {
                    "id": "back",
                    "project_id": "p2",
                    "application_id": "a2",
                    "environment_id": "e2",
                    "layer": "backend",
                },
            ],
            "primary_participant_id": "front",
            "traffic_entry_participant_id": "front",
        }
    )


def identity(*, all_participants=False):
    return ConsumerIdentity(
        consumer_id="scope-review",
        name="Reviewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="p1", app_id="a1")]
        + ([ScopeRef(project_id="p2", app_id="a2")] if all_participants else []),
    )


@pytest.fixture
async def database(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    cast(Table, model.__table__) for model in (RunScopeBinding, RunParticipant)
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apex.persistence.db.get_sessionmaker", lambda: factory)
    monkeypatch.setattr(
        middleware,
        "get_default_resolver",
        lambda: SimpleNamespace(resolve=AsyncMock(return_value=identity())),
    )
    monkeypatch.setattr(middleware, "SEARCH_PAGE_SIZE", 2)
    monkeypatch.setattr(middleware, "MAX_SEARCH_SCAN", 10)
    yield factory
    await engine.dispose()


class NativeRoutes:
    def __init__(self, rows):
        self.rows = rows
        self.target_calls = []

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        path = scope["path"].rstrip("/")
        if path == "/threads/count":
            return await JSONResponse(999, headers={"X-Pagination-Total": "999"})(
                scope, receive, send
            )
        if path == "/threads/search":
            payload = await request.json()
            limit, offset = payload.get("limit", 10), payload.get("offset", 0)
            if type(limit) is not int or limit < 1:
                return await JSONResponse({"detail": "invalid pagination"}, status_code=422)(
                    scope, receive, send
                )
            rows = self.rows
            if "ids" in payload:
                rows = [row for row in rows if row["thread_id"] in payload["ids"]]
            page = rows[offset : offset + limit]
            if payload.get("select"):
                page = [
                    {key: value for key, value in row.items() if key in payload["select"]}
                    for row in page
                ]
            return await JSONResponse(
                page, headers={"X-Pagination-Total": "999", "X-Pagination-Next": "999"}
            )(scope, receive, send)
        self.target_calls.append(path)
        return await JSONResponse({"target": "admitted"})(scope, receive, send)


async def request(app, path, payload=None, method="POST"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware.TestScopeAccessMiddleware(app)),
        base_url="http://review",
    ) as client:
        return await client.request(method, path, json=payload)


@pytest.mark.parametrize("suffix", ["", "/"])
async def test_search_paginates_visible_runs_and_replaces_upstream_count_headers(database, suffix):
    scope = topology()
    await persist_test_scope("hidden-1", scope)
    await persist_test_scope("hidden-2", scope)
    native = NativeRoutes(
        [
            {"thread_id": "hidden-1", "metadata": {"test_scope": scope.model_dump(mode="json")}},
            {"thread_id": "visible-1", "metadata": {}},
            {"thread_id": "hidden-2", "metadata": {"test_scope": scope.model_dump(mode="json")}},
            {"thread_id": "visible-2", "metadata": {}},
            {"thread_id": "visible-3", "metadata": {}},
        ]
    )
    response = await request(
        native, "/threads/search" + suffix, {"limit": 1, "offset": 1, "select": ["thread_id"]}
    )
    assert response.status_code == 200
    assert response.json() == [{"thread_id": "visible-2"}]
    assert response.headers["x-pagination-total"] == "3"
    assert response.headers["x-pagination-next"] == "2"
    assert "metadata" not in response.text and "hidden" not in response.text
    counted = await request(native, "/threads/count" + suffix, {})
    assert counted.json() == 3
    assert "x-pagination-total" not in counted.headers


async def test_count_never_returns_partial_results_or_upstream_total(database, monkeypatch):
    monkeypatch.setattr(middleware, "MAX_SEARCH_SCAN", 2)
    native = NativeRoutes([{"thread_id": str(i), "metadata": {}} for i in range(4)])
    response = await request(native, "/threads/count", {})
    assert response.status_code == 503
    assert "999" not in response.text and "x-pagination-total" not in response.headers


async def test_native_pagination_validation_is_preserved(database):
    response = await request(NativeRoutes([]), "/threads/search", {"limit": 0})
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/threads/missing/runs/stream", "/v1/pipelines/missing/resume"])
async def test_metadata_marked_new_run_without_binding_never_admits_stream_or_resume(
    database, path
):
    native = NativeRoutes(
        [{"thread_id": "missing", "metadata": {"test_scope": topology().model_dump(mode="json")}}]
    )
    response = await request(native, path, {})
    assert response.status_code == 404
    assert native.target_calls == []


@pytest.mark.parametrize(
    "path", ["/v1/pipelines/analysis/connections", "/v1/pipelines/execution-readiness"]
)
async def test_setup_routes_are_not_mistaken_for_thread_ids(database, path):
    native = NativeRoutes([])
    response = await request(native, path, {}, method="GET")
    assert response.status_code == 200
    assert native.target_calls == [path]


async def test_incomplete_binding_rejects_direct_reads_and_sql_analytics(database):
    await persist_test_scope("combined", topology())
    async with database() as session:
        await session.execute(
            delete(RunParticipant).where(
                RunParticipant.thread_id == "combined", RunParticipant.participant_id == "back"
            )
        )
        await session.commit()
    assert not await can_access_thread(identity(), "combined")
    with pytest.raises(ValueError, match="incomplete"):
        await load_persisted_test_scope("combined")
    async with database() as session:
        assert (
            await session.scalar(
                select(literal("combined")).where(
                    participant_visibility(literal("combined"), identity().scopes)
                )
            )
            is None
        )


async def test_header_digest_and_orphan_rows_fail_closed(database):
    await persist_test_scope("combined", topology())
    async with database() as session:
        binding = await session.get(RunScopeBinding, "combined")
        assert binding is not None
        binding.scope_digest = "0" * 64
        await session.commit()
    assert not await can_access_thread(identity(all_participants=True), "combined")
    with pytest.raises(ValueError, match="corrupt"):
        await load_persisted_test_scope("combined")
    async with database() as session:
        await session.execute(
            delete(RunScopeBinding).where(RunScopeBinding.thread_id == "combined")
        )
        await session.commit()
    assert not await can_access_thread(identity(all_participants=True), "combined")
    with pytest.raises(ValueError, match="missing"):
        await load_persisted_test_scope("combined")
    assert await load_persisted_test_scope("legacy") is None
    assert await can_access_thread(identity(), "legacy", expected_scope=None)
    assert not await can_access_thread(
        identity(), "legacy", expected_scope=topology().model_dump(mode="json")
    )


async def test_native_uuid_identity_resolves_the_same_binding(database):
    from uuid import uuid4

    thread_id = uuid4()
    await persist_test_scope(str(thread_id), topology())
    assert await can_access_thread(identity(all_participants=True), thread_id)
    assert await load_persisted_test_scope(thread_id) == TestScopeV1.model_validate(
        {
            **topology().model_dump(mode="json"),
            "participants": sorted(
                topology().model_dump(mode="json")["participants"],
                key=lambda participant: participant["id"],
            ),
        }
    )


def test_public_rate_limit_and_audit_wrap_internal_scope_scans():
    from apex.app.http import app
    from apex.app.security import AuthAuditMiddleware, RateLimitMiddleware

    order: list[object] = [entry.cls for entry in app.user_middleware]
    assert order.index(RateLimitMiddleware) < order.index(middleware.TestScopeAccessMiddleware)
    assert order.index(AuthAuditMiddleware) < order.index(middleware.TestScopeAccessMiddleware)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/pipelines/run/abort"),
        ("POST", "/v1/pipelines/run/gates/gate/resume"),
        ("POST", "/v1/pipelines/run/rerun"),
        ("PATCH", "/v1/pipelines/run/phases/reporting/prompt-review"),
        ("POST", "/threads/run/runs/stream"),
        ("DELETE", "/threads/run"),
    ],
)
async def test_viewer_mutations_fail_role_admission_before_projection_io(monkeypatch, method, path):
    viewer = identity().model_copy(update={"role": Role.VIEWER})
    monkeypatch.setattr(
        middleware,
        "get_default_resolver",
        lambda: SimpleNamespace(resolve=AsyncMock(return_value=viewer)),
    )
    projection = AsyncMock(side_effect=AssertionError("Role check must precede projection IO"))
    monkeypatch.setattr(middleware, "can_access_thread", projection)
    native = NativeRoutes([])
    response = await request(native, path, {}, method=method)
    assert response.status_code == 403
    projection.assert_not_awaited()
    assert not native.target_calls


@pytest.mark.parametrize("suffix", ["history", "state/checkpoint"])
async def test_native_post_reads_remain_available_to_viewers(database, monkeypatch, suffix):
    viewer = identity().model_copy(update={"role": Role.VIEWER})
    monkeypatch.setattr(
        middleware,
        "get_default_resolver",
        lambda: SimpleNamespace(resolve=AsyncMock(return_value=viewer)),
    )
    native = NativeRoutes([{"thread_id": "legacy", "metadata": {}}])
    response = await request(native, f"/threads/legacy/{suffix}", {})
    assert response.status_code == 200
    assert native.target_calls == [f"/threads/legacy/{suffix}"]
