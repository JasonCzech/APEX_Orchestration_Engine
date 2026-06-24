"""/work-tracking routes: fake adapter via resolver override, scoping, saved queries."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.registry import PortKind
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    TranslatedQuery,
    WorkItem,
    WorkItemDraft,
    WorkItemFilters,
    WorkItemPage,
)
from apex.persistence.models import SavedQuery
from apex.routers.work_tracking import router
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeWorkTrackingAdapter:
    """WorkTrackingPort fake with a recordable canned backlog."""

    def __init__(self) -> None:
        self.items: dict[str, WorkItem] = {
            "PHX-1": WorkItem(key="PHX-1", title="First", kind="bug", status="open"),
            "PHX-2": WorkItem(key="PHX-2", title="Second", kind="story", status="closed"),
        }
        self.translate_calls: list[tuple[str, QueryContext]] = []
        self.execute_calls: list[tuple[TranslatedQuery, Page]] = []
        self.list_calls: list[tuple[WorkItemFilters, Page]] = []
        self.enrich_calls: list[tuple[str, Enrichment]] = []

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        self.translate_calls.append((natural_language, context))
        return TranslatedQuery(
            provider="fake", query=f'text ~ "{natural_language}"', confidence=0.45
        )

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage:
        self.execute_calls.append((query, page))
        rows = list(self.items.values())
        window = rows[page.offset : page.offset + page.limit]
        return WorkItemPage(items=window, total=len(rows), page=page)

    async def get_item(self, key: str) -> WorkItem:
        if key not in self.items:
            raise KeyError(f"work item {key!r} not found in fake tracker")
        return self.items[key]

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
        self.list_calls.append((filters, page))
        rows = [i for i in self.items.values() if not filters.status or i.status == filters.status]
        return WorkItemPage(items=rows, total=len(rows), page=page)

    async def create_item(self, draft: WorkItemDraft) -> WorkItem:
        return WorkItem(key="PHX-900", title=draft.title, kind=draft.kind, status="open")

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        self.enrich_calls.append((key, enrichment))
        return await self.get_item(key)


class BoomAdapter:
    """Simulates upstream-tracker failures already mapped by a real adapter."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def _raise(self, *args: Any, **kwargs: Any) -> Any:
        raise self._exc

    translate_query = _raise
    execute_query = _raise
    get_item = _raise
    list_items = _raise
    create_item = _raise
    enrich_item = _raise


class FakeResolver:
    """Records resolve() args; returns the configured adapter or raises."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.calls: list[tuple[PortKind, str | None, str | None]] = []
        self.raises: Exception | None = None

    async def resolve(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        self.calls.append((kind, connection_id, project_id))
        if self.raises is not None:
            raise self.raises
        return self.adapter


class FakeSavedQueriesRepository:
    """In-memory stand-in matching SavedQueriesRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, SavedQuery] = {}

    def _conflict(self, row: SavedQuery) -> bool:
        return any(
            other.id != row.id and other.name == row.name and other.project_id == row.project_id
            for other in self.rows.values()
        )

    async def add(self, row: SavedQuery) -> SavedQuery:
        if self._conflict(row):
            raise ValueError(
                f"a saved query named {row.name!r} already exists for project {row.project_id!r}"
            )
        row.id = row.id or uuid.uuid4().hex[:32]
        row.created_at = row.created_at or datetime.now(UTC)
        row.updated_at = row.updated_at or row.created_at
        self.rows[row.id] = row
        return row

    async def get(self, saved_query_id: str) -> SavedQuery | None:
        return self.rows.get(saved_query_id)

    async def list(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        allowed_project_ids: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SavedQuery]:
        rows = sorted(self.rows.values(), key=lambda r: r.name)
        if allowed_project_ids is not None:
            rows = [r for r in rows if r.project_id is None or r.project_id in allowed_project_ids]
        if project is not None:
            rows = [r for r in rows if r.project_id == project]
        if provider is not None:
            rows = [r for r in rows if r.provider == provider]
        return rows[offset : offset + limit]

    async def update(self, row: SavedQuery, changes: dict[str, Any]) -> SavedQuery:
        for key, value in changes.items():
            setattr(row, key, value)
        if self._conflict(row):
            raise ValueError(
                f"a saved query named {row.name!r} already exists for project {row.project_id!r}"
            )
        row.updated_at = datetime.now(UTC)
        return row

    async def delete(self, row: SavedQuery) -> None:
        self.rows.pop(row.id, None)


# ── App factory ──────────────────────────────────────────────────────────────


def identity(role: Role = Role.OPERATOR, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="op",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes if scopes is not None else [ScopeRef(project_id="p1")],
    )


def make_app(
    who: ConsumerIdentity,
    resolver: FakeResolver | None = None,
    repo: FakeSavedQueriesRepository | None = None,
) -> tuple[FastAPI, FakeResolver, FakeSavedQueriesRepository]:
    resolver = resolver or FakeResolver(FakeWorkTrackingAdapter())
    repo = repo or FakeSavedQueriesRepository()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: who
    app.dependency_overrides[get_work_tracking_resolver] = lambda: resolver
    app.dependency_overrides[get_saved_queries_repository] = lambda: repo
    return app, resolver, repo


def saved_query_row(
    row_id: str, name: str, project_id: str | None, provider: str = "jira"
) -> SavedQuery:
    now = datetime.now(UTC)
    return SavedQuery(
        id=row_id,
        name=name,
        project_id=project_id,
        provider=provider,
        query="project = PHX",
        created_at=now,
        updated_at=now,
    )


# ── Query passthrough ────────────────────────────────────────────────────────


def test_translate_resolves_with_scoped_project() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": "open bugs"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "fake"
    assert body["query"] == 'text ~ "open bugs"'
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "p1")]
    adapter = resolver.adapter
    assert adapter.translate_calls[0][1].project_id == "p1"  # QueryContext carries the scope


def test_translate_body_connection_id_wins_over_query_param() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate",
            params={"connection_id": "from-query"},
            json={"text": "open bugs", "connection_id": "from-body"},
        )
    assert response.status_code == 200
    assert resolver.calls[0][1] == "from-body"


def test_translate_blank_text_is_422() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": ""})
    assert response.status_code == 422


def test_execute_query_pages_through_adapter() -> None:
    app, resolver, _ = make_app(identity(role=Role.ADMIN, scopes=[]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "fake", "query": "project = PHX"}, "limit": 1, "offset": 1},
        )
    assert response.status_code == 200
    body = response.json()
    assert [item["key"] for item in body["items"]] == ["PHX-2"]
    assert body["total"] == 2
    assert body["page"] == {"offset": 1, "limit": 1}
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, None)]  # unscoped admin


def test_unknown_connection_id_is_404_problem() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter())
    resolver.raises = KeyError("unknown connection_id 'nope'; known: []")
    app, _, _ = make_app(identity(), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate", json={"text": "x", "connection_id": "nope"}
        )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "unknown connection_id" in response.json()["title"]


def test_misconfigured_connection_is_409() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter())
    resolver.raises = ValueError("connection 'c9' is disabled")
    app, _, _ = make_app(identity(), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate", json={"text": "x", "connection_id": "c9"}
        )
    assert response.status_code == 409


# ── Item passthrough ─────────────────────────────────────────────────────────


def test_get_work_item_found_and_missing() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        found = client.get("/v1/work-tracking/items/PHX-1")
        missing = client.get("/v1/work-tracking/items/PHX-404")
    assert found.status_code == 200
    assert found.json()["title"] == "First"
    assert missing.status_code == 404
    assert "PHX-404" in missing.json()["title"]


def test_list_work_items_passes_filters_and_connection_id() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.get(
            "/v1/work-tracking/items",
            params={
                "status": "open",
                "kind": "bug",
                "q": "checkout",
                "limit": 5,
                "offset": 2,
                "connection_id": "jira-acme",
            },
        )
    assert response.status_code == 200
    assert resolver.calls == [(PortKind.WORK_TRACKING, "jira-acme", "p1")]
    filters, page = resolver.adapter.list_calls[0]
    assert filters == WorkItemFilters(status="open", kind="bug", text="checkout")
    assert (page.offset, page.limit) == (2, 5)


def test_create_work_item_requires_operator() -> None:
    app, _, _ = make_app(identity(role=Role.VIEWER))
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/items", json={"title": "New story"})
    assert response.status_code == 403


def test_create_work_item_created() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items", json={"title": "New story", "kind": "story"}
        )
    assert response.status_code == 201
    assert response.json()["key"] == "PHX-900"


def test_enrich_work_item_roles_and_payload() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        ok = client.post(
            "/v1/work-tracking/items/PHX-1/enrich",
            json={"comment": "triaged", "fields": {"System.Tags": "perf"}},
        )
    assert ok.status_code == 200
    key, enrichment = resolver.adapter.enrich_calls[0]
    assert key == "PHX-1"
    assert enrichment.comment == "triaged"
    assert enrichment.fields == {"System.Tags": "perf"}

    viewer_app, _, _ = make_app(identity(role=Role.VIEWER))
    with TestClient(viewer_app) as client:
        denied = client.post("/v1/work-tracking/items/PHX-1/enrich", json={"comment": "x"})
    assert denied.status_code == 403


def test_upstream_runtime_error_is_502_problem_detail() -> None:
    resolver = FakeResolver(BoomAdapter(RuntimeError("jira GET /x failed with HTTP 503: outage")))
    app, _, _ = make_app(identity(), resolver=resolver)
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "work tracker upstream failure"
    assert "HTTP 503" not in response.text


def test_adapter_value_error_is_422() -> None:
    resolver = FakeResolver(BoomAdapter(ValueError("jira rejected the request: bad JQL")))
    app, _, _ = make_app(identity(), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": "project zz"}},
        )
    assert response.status_code == 422
    assert response.json()["title"] == "work tracker rejected the request"
    assert "bad JQL" not in response.text


# ── Saved queries CRUD + scoping ─────────────────────────────────────────────


def seeded_repo() -> FakeSavedQueriesRepository:
    repo = FakeSavedQueriesRepository()
    repo.rows["s1"] = saved_query_row("s1", "alpha p1", "p1")
    repo.rows["s2"] = saved_query_row("s2", "beta p2", "p2")
    repo.rows["s3"] = saved_query_row("s3", "gamma global", None, provider="ado")
    return repo


def test_list_saved_queries_scoped_sees_own_and_global() -> None:
    app, _, _ = make_app(identity(), repo=seeded_repo())
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/saved-queries")
    ids = [row["id"] for row in response.json()["items"]]
    assert sorted(ids) == ["s1", "s3"]


def test_list_saved_queries_admin_sees_all_and_filters_by_provider() -> None:
    app, _, _ = make_app(identity(role=Role.ADMIN, scopes=[]), repo=seeded_repo())
    with TestClient(app) as client:
        everything = client.get("/v1/work-tracking/saved-queries")
        ado_only = client.get("/v1/work-tracking/saved-queries", params={"provider": "ado"})
    assert len(everything.json()["items"]) == 3
    assert [row["id"] for row in ado_only.json()["items"]] == ["s3"]


def test_create_saved_query_scoping_and_conflict() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)
    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "p1 opens",
                "provider": "jira",
                "query": "status = Open",
                "project_id": "p1",
            },
        )
        out_of_scope = client.post(
            "/v1/work-tracking/saved-queries",
            json={"name": "p2 opens", "provider": "jira", "query": "q", "project_id": "p2"},
        )
        duplicate = client.post(
            "/v1/work-tracking/saved-queries",
            json={"name": "alpha p1", "provider": "jira", "query": "q", "project_id": "p1"},
        )
    assert created.status_code == 201
    body = created.json()
    assert body["created_by"] == "op"
    assert body["id"] in repo.rows
    assert out_of_scope.status_code == 403
    assert duplicate.status_code == 409


def test_create_saved_query_viewer_is_403() -> None:
    app, _, _ = make_app(identity(role=Role.VIEWER))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/saved-queries",
            json={"name": "n", "provider": "jira", "query": "q"},
        )
    assert response.status_code == 403


def test_get_saved_query_scoped_404() -> None:
    app, _, _ = make_app(identity(), repo=seeded_repo())
    with TestClient(app) as client:
        assert client.get("/v1/work-tracking/saved-queries/s1").status_code == 200
        assert client.get("/v1/work-tracking/saved-queries/s2").status_code == 404  # out of scope
        assert client.get("/v1/work-tracking/saved-queries/missing").status_code == 404


def test_update_saved_query_patches_fields_and_conflicts() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)
    with TestClient(app) as client:
        updated = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"query": "status = Done", "description": "now done"},
        )
        collision = client.patch(
            "/v1/work-tracking/saved-queries/s3", json={"name": "alpha p1", "project_id": "p1"}
        )
    assert updated.status_code == 200
    assert updated.json()["query"] == "status = Done"
    assert updated.json()["description"] == "now done"
    assert repo.rows["s1"].query == "status = Done"
    # s3 is global; renaming alone cannot collide with ("p1", "alpha p1") since
    # project_id is not updatable through the PATCH body.
    assert collision.status_code == 200
    assert repo.rows["s3"].project_id is None


def test_update_saved_query_name_collision_is_409() -> None:
    repo = seeded_repo()
    repo.rows["s4"] = saved_query_row("s4", "delta p1", "p1")
    app, _, _ = make_app(identity(), repo=repo)
    with TestClient(app) as client:
        response = client.patch("/v1/work-tracking/saved-queries/s4", json={"name": "alpha p1"})
    assert response.status_code == 409


def test_delete_saved_query_roles_and_scope() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)
    with TestClient(app) as client:
        assert client.delete("/v1/work-tracking/saved-queries/s1").status_code == 204
        assert client.delete("/v1/work-tracking/saved-queries/s2").status_code == 404
    assert "s1" not in repo.rows

    viewer_app, _, _ = make_app(identity(role=Role.VIEWER), repo=repo)
    with TestClient(viewer_app) as client:
        assert client.delete("/v1/work-tracking/saved-queries/s3").status_code == 403
