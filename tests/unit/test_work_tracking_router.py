"""/work-tracking routes: fake adapter via resolver override, scoping, saved queries."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
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
from apex.routers.work_tracking import ExecuteQueryRequest, TranslateQueryRequest, router
from apex.services import work_item_mutations as mutation_module
from apex.services.work_item_mutations import (
    WorkItemMutationOutcomeAmbiguousError,
    WorkItemMutationService,
    get_work_item_mutation_service,
)
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (TranslateQueryRequest, {"text": "open bugs", "connection_id": "conn\x00id"}),
        (TranslateQueryRequest, {"text": "open\x00bugs"}),
        (
            ExecuteQueryRequest,
            {
                "query": {"provider": "jira", "query": "project = DEMO"},
                "connection_id": "conn\x00id",
            },
        ),
        (
            ExecuteQueryRequest,
            {"query": {"provider": "jira", "query": "project\x00 = DEMO"}},
        ),
    ],
)
def test_work_query_body_models_reject_nul_provider_input(
    model: type[Any], payload: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        model.model_validate(payload)


def test_execute_query_body_rejects_offset_outside_provider_page_before_resolution() -> None:
    with pytest.raises(ValueError, match="less_than_equal"):
        ExecuteQueryRequest.model_validate(
            {
                "query": {"provider": "jira", "query": "project = DEMO"},
                "offset": 1_001,
            }
        )


class FakeWorkTrackingAdapter:
    """WorkTrackingPort fake with a recordable canned backlog."""

    def __init__(
        self,
        provider: str = "fake",
        project_id: str | None = None,
        *,
        internal_project_id: str | None = None,
    ) -> None:
        self.provider = provider
        # project_id models the external Jira key / ADO TeamProject. The APEX
        # owner is a separate resolver-supplied marker.
        self.project_id = project_id
        self.apex_project_id = (
            internal_project_id if internal_project_id is not None else project_id
        )
        self.items: dict[str, WorkItem] = {
            "PHX-1": WorkItem(key="PHX-1", title="First", kind="bug", status="open"),
            "PHX-2": WorkItem(key="PHX-2", title="Second", kind="story", status="closed"),
        }
        self.translate_calls: list[tuple[str, QueryContext]] = []
        self.execute_calls: list[tuple[TranslatedQuery, Page]] = []
        self.list_calls: list[tuple[WorkItemFilters, Page]] = []
        self.create_calls: list[WorkItemDraft] = []
        self.enrich_calls: list[tuple[str, Enrichment]] = []
        self.created_by_marker: dict[str, WorkItem] = {}
        self.comment_markers: set[tuple[str, str]] = set()

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        self.translate_calls.append((natural_language, context))
        return TranslatedQuery(
            provider=self.provider, query=f'text ~ "{natural_language}"', confidence=0.45
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
        self.create_calls.append(draft)
        return WorkItem(key="PHX-900", title=draft.title, kind=draft.kind, status="open")

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        self.enrich_calls.append((key, enrichment))
        return await self.get_item(key)

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        return self.created_by_marker.get(marker)

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        item = WorkItem(key="PHX-900", title=draft.title, kind=draft.kind, status="open")
        self.created_by_marker[marker] = item
        return item

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None:
        self.enrich_calls.append((key, Enrichment(fields=fields)))

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        return (key, marker) in self.comment_markers

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None:
        self.enrich_calls.append((key, Enrichment(comment=comment)))
        self.comment_markers.add((key, marker))


class BoomAdapter:
    """Simulates upstream-tracker failures already mapped by a real adapter."""

    def __init__(self, exc: Exception, provider: str = "jira") -> None:
        self._exc = exc
        self.provider = provider
        self.project_id = "p1"
        self.apex_project_id = "p1"

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


class MetadataResolver:
    """Separates metadata selection from adapter construction for replay tests."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.build_calls = 0
        self.fail_build = False

    async def resolve_metadata(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            config=SimpleNamespace(id="dev-work-tracking-fake", provider="fake"),
            persisted=False,
            connection_version=None,
        )

    async def build_from_metadata(self, _metadata: Any) -> Any:
        self.build_calls += 1
        if self.fail_build:
            raise RuntimeError("secret store unavailable")
        return SimpleNamespace(
            adapter=self.adapter,
            connection_id="dev-work-tracking-fake",
            persisted=False,
            connection_version=None,
        )


class FakeSavedQueriesRepository:
    """In-memory stand-in matching SavedQueriesRepository's surface."""

    def __init__(self) -> None:
        self.rows: dict[str, SavedQuery] = {}
        self.list_calls = 0
        self.locked_gets: list[str] = []

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

    async def get_for_update(self, saved_query_id: str) -> SavedQuery | None:
        self.locked_gets.append(saved_query_id)
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
        self.list_calls += 1
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
    assert adapter.translate_calls[0][1].hints == {"project": "p1"}


def test_translate_uses_external_project_hint_after_internal_binding() -> None:
    resolver = FakeResolver(
        FakeWorkTrackingAdapter(
            provider="jira",
            project_id="PHX",
            internal_project_id="internal-project-1",
        )
    )
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="internal-project-1")]), resolver=resolver
    )

    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": "open bugs"})

    assert response.status_code == 200
    _, context = resolver.adapter.translate_calls[0]
    assert context.project_id == "internal-project-1"
    assert context.hints == {"project": "PHX"}


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


def test_translate_multi_project_requires_project() -> None:
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")])
    )
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": "open bugs"})
    assert response.status_code == 403
    assert resolver.calls == []


def test_translate_multi_project_uses_selected_project() -> None:
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")])
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate",
            params={"project": "CAT"},
            json={"text": "open bugs"},
        )
    assert response.status_code == 200
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "CAT")]
    assert resolver.adapter.translate_calls[0][1].project_id == "CAT"


def test_translate_rejects_out_of_scope_project() -> None:
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate",
            params={"project": "CAT"},
            json={"text": "open bugs"},
        )
    assert response.status_code == 403
    assert resolver.calls == []


def test_translate_blank_text_is_422() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": ""})
    assert response.status_code == 422


def test_translate_rejects_oversized_text_before_adapter_resolution() -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": "x" * 20_001})

    assert response.status_code == 422
    assert resolver.adapter.translate_calls == []


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


def test_execute_query_rejects_provider_window_before_adapter_resolution() -> None:
    app, resolver, _ = make_app(identity(role=Role.ADMIN, scopes=[]))

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {"provider": "fake", "query": "project = PHX"},
                "limit": 200,
                "offset": 801,
            },
        )

    assert response.status_code == 422
    assert resolver.calls == []
    assert resolver.adapter.execute_calls == []


def test_execute_query_injects_jira_project_scope() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": "status = Open ORDER BY updated DESC"}},
        )
    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == "project in (PHX) AND (status = Open) ORDER BY updated DESC"


def test_execute_query_constrains_to_external_project_after_internal_binding() -> None:
    resolver = FakeResolver(
        FakeWorkTrackingAdapter(
            provider="jira",
            project_id="PHX",
            internal_project_id="internal-project-1",
        )
    )
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="internal-project-1")]), resolver=resolver
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": "status = Open"}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == "project in (PHX) AND (status = Open)"
    assert "internal-project-1" not in query.query


def test_execute_query_quotes_connection_project_as_jql_data() -> None:
    external_project = 'PHX") OR project = OTHER OR project in ("PHX'
    resolver = FakeResolver(
        FakeWorkTrackingAdapter(
            provider="jira",
            project_id=external_project,
            internal_project_id="internal-project-1",
        )
    )
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="internal-project-1")]), resolver=resolver
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": "status = Open"}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        'project in ("PHX\\") OR project = OTHER OR project in (\\"PHX") AND (status = Open)'
    )


def test_execute_query_rejects_conflicting_jira_project_scope() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": "project = OTH AND status = Open"}},
        )
    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []


@pytest.mark.parametrize(
    "query",
    [
        "status = Open) OR key = OTH-1 OR (status = Open",
        "status = Open -- project = OTH",
        "status = Open; project = OTH",
        'status = "unterminated',
    ],
)
def test_execute_query_rejects_jira_scope_envelope_escape(query: str) -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": query}},
        )

    assert response.status_code == 422
    assert resolver.adapter.execute_calls == []


def test_execute_query_injects_ado_multi_project_scope() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")]),
        resolver=resolver,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            params={"project": "CAT"},
            json={"query": {"provider": "ado", "query": "SELECT [System.Id] FROM WorkItems"}},
        )
    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] IN ('CAT')"
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "CAT")]


def test_execute_query_rejects_wiql_scope_envelope_escape() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItems WHERE ([System.State] = 'Open')) "
                        "OR ([System.TeamProject] = 'OTHER'"
                    ),
                }
            },
        )

    assert response.status_code == 422
    assert resolver.adapter.execute_calls == []


def test_execute_query_multi_project_requires_project() -> None:
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")])
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "ado", "query": "SELECT [System.Id] FROM WorkItems"}},
        )
    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []
    assert resolver.calls == []


def test_execute_query_rejects_project_outside_scope_before_adapter_resolution() -> None:
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            params={"project": "CAT"},
            json={"query": {"provider": "ado", "query": "SELECT [System.Id] FROM WorkItems"}},
        )
    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []
    assert resolver.calls == []


def test_execute_query_unknown_provider_requires_unscoped_admin() -> None:
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "unknown", "query": "project = PHX"}},
        )
    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []


def test_execute_query_rejects_provider_spoof_before_adapter_call() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "stub", "query": "status = Open"}},
        )
    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []


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
    assert response.json()["title"] == "work-tracking connection not found"
    assert "nope" not in response.text


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
    assert missing.json()["title"] == "work item not found"
    assert "PHX-404" not in missing.text


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


def test_list_work_items_rejects_provider_window_before_list_call() -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items", params={"offset": 801, "limit": 200})

    assert response.status_code == 422
    assert resolver.adapter.list_calls == []


def test_list_work_items_multi_project_requires_project() -> None:
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")])
    )
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items")
    assert response.status_code == 403
    assert resolver.calls == []


def test_list_work_items_multi_project_uses_selected_project() -> None:
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="PHX"), ScopeRef(project_id="CAT")])
    )
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items", params={"project": "PHX"})
    assert response.status_code == 200
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "PHX")]


def test_direct_item_allows_internal_project_to_different_external_jira_key() -> None:
    resolver = FakeResolver(
        FakeWorkTrackingAdapter(
            provider="jira",
            project_id="PHX",
            internal_project_id="internal-p1",
        )
    )
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="internal-p1")]), resolver=resolver
    )

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 200
    assert resolver.calls == [(PortKind.WORK_TRACKING, None, "internal-p1")]
    assert resolver.adapter.project_id == "PHX"
    assert resolver.adapter.apex_project_id == "internal-p1"


def test_direct_item_route_rejects_adapter_bound_to_another_project() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")
    assert response.status_code == 403


def test_create_work_item_requires_operator() -> None:
    app, _, _ = make_app(identity(role=Role.VIEWER))
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            json={"title": "New story"},
            headers={"Idempotency-Key": "create-viewer"},
        )
    assert response.status_code == 403


def test_create_work_item_created() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            json={"title": "New story", "kind": "story"},
            headers={"Idempotency-Key": "create-new-story"},
        )
    assert response.status_code == 201
    assert response.json()["key"] == "PHX-900"


def test_create_work_item_requires_idempotency_key() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/items", json={"title": "No key"})

    assert response.status_code == 422
    assert resolver.adapter.created_by_marker == {}


def test_create_work_item_replays_and_rejects_key_payload_conflict() -> None:
    app, resolver, _ = make_app(identity())
    headers = {"Idempotency-Key": "router-replay-conflict"}
    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/items",
            json={"title": "Stable payload"},
            headers=headers,
        )
        replay = client.post(
            "/v1/work-tracking/items",
            json={"title": "Stable payload"},
            headers=headers,
        )
        conflict = client.post(
            "/v1/work-tracking/items",
            json={"title": "Different payload"},
            headers=headers,
        )

    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    assert conflict.status_code == 409
    assert len(resolver.adapter.created_by_marker) == 1


def test_create_work_item_surfaces_ambiguous_dispatch_for_operator_reconciliation() -> None:
    class AmbiguousMutationService:
        async def replay_create(self, **_kwargs: Any) -> None:
            return None

        async def create(self, **_kwargs: Any) -> WorkItem:
            row = SimpleNamespace(
                id="mutation-123",
                provider_marker="apex-idem-mutation-123",
            )
            raise WorkItemMutationOutcomeAmbiguousError(cast(Any, row), operation="create")

    app, _, _ = make_app(identity())
    app.dependency_overrides[get_work_item_mutation_service] = AmbiguousMutationService

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            json={"title": "Ambiguous"},
            headers={"Idempotency-Key": "ambiguous-dispatch"},
        )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["title"] == "work-item mutation outcome is ambiguous"
    assert "mutation-123" not in response.text
    assert "apex-idem-mutation-123" not in response.text


def test_completed_create_replays_before_adapter_construction() -> None:
    resolver = MetadataResolver(FakeWorkTrackingAdapter())
    app, _, _ = make_app(identity(), resolver=resolver)  # type: ignore[arg-type]
    repository = mutation_module._EphemeralMutationRepository()
    mutations = WorkItemMutationService(repository, ephemeral_repository=repository)
    app.dependency_overrides[get_work_item_mutation_service] = lambda: mutations
    headers = {"Idempotency-Key": "terminal-before-secrets"}

    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/items",
            json={"title": "Replay without provider"},
            headers=headers,
        )
        resolver.fail_build = True
        replay = client.post(
            "/v1/work-tracking/items",
            json={"title": "Replay without provider"},
            headers=headers,
        )

    assert created.status_code == replay.status_code == 201
    assert replay.json() == created.json()
    assert resolver.build_calls == 1


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/work-tracking/items", {"title": "Forbidden"}),
        ("/v1/work-tracking/items/PHX-1/enrich", {"comment": "Forbidden"}),
    ],
)
def test_app_only_operator_cannot_mutate_project_wide_work_items_before_resolution(
    path: str,
    payload: dict[str, Any],
) -> None:
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="p1", app_id="app-1")]))

    with TestClient(app) as client:
        response = client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": "app-only-forbidden"},
        )

    assert response.status_code == 403
    assert resolver.calls == []
    assert resolver.adapter.created_by_marker == {}
    assert resolver.adapter.enrich_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "t" * 501},
        {"title": "ok", "description": "d" * 20_001},
        {"title": "ok", "fields": {f"field-{index}": index for index in range(65)}},
    ],
)
def test_create_work_item_rejects_oversized_payload_before_adapter(
    payload: dict[str, Any],
) -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/items", json=payload)

    assert response.status_code == 422
    assert resolver.adapter.create_calls == []


def test_enrich_work_item_roles_and_payload() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        ok = client.post(
            "/v1/work-tracking/items/PHX-1/enrich",
            json={"comment": "triaged", "fields": {"System.Tags": "perf"}},
            headers={"Idempotency-Key": "enrich-phx-1"},
        )
    assert ok.status_code == 200
    assert len(resolver.adapter.enrich_calls) == 2
    fields_key, fields_enrichment = resolver.adapter.enrich_calls[0]
    comment_key, comment_enrichment = resolver.adapter.enrich_calls[1]
    assert fields_key == comment_key == "PHX-1"
    assert fields_enrichment.fields == {"System.Tags": "perf"}
    assert comment_enrichment.comment == "triaged"

    viewer_app, _, _ = make_app(identity(role=Role.VIEWER))
    with TestClient(viewer_app) as client:
        denied = client.post(
            "/v1/work-tracking/items/PHX-1/enrich",
            json={"comment": "x"},
            headers={"Idempotency-Key": "enrich-viewer"},
        )
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


def test_list_saved_queries_rejects_huge_offset_before_repository() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/saved-queries", params={"offset": 10_001})

    assert response.status_code == 422
    assert repo.list_calls == 0


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


def test_global_saved_query_mutations_require_unscoped_admin() -> None:
    repo = seeded_repo()
    scoped_app, _, _ = make_app(identity(), repo=repo)
    with TestClient(scoped_app) as client:
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={"name": "global", "provider": "jira", "query": "q"},
        )
        updated = client.patch(
            "/v1/work-tracking/saved-queries/s3", json={"description": "changed"}
        )
        deleted = client.delete("/v1/work-tracking/saved-queries/s3")
    assert created.status_code == 403
    assert updated.status_code == 403
    assert deleted.status_code == 403
    assert "s3" in repo.rows

    admin_app, _, _ = make_app(identity(role=Role.ADMIN, scopes=[]), repo=repo)
    with TestClient(admin_app) as client:
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={"name": "global", "provider": "jira", "query": "q"},
        )
        updated = client.patch(
            "/v1/work-tracking/saved-queries/s3", json={"description": "changed"}
        )
        deleted = client.delete("/v1/work-tracking/saved-queries/s3")
    assert created.status_code == 201
    assert updated.status_code == 200
    assert deleted.status_code == 204


def test_app_only_scope_cannot_mutate_project_wide_saved_queries() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")]), repo=repo)
    with TestClient(app) as client:
        assert client.get("/v1/work-tracking/saved-queries/s1").status_code == 200
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "app cannot own this",
                "provider": "jira",
                "query": "q",
                "project_id": "p1",
            },
        )
        updated = client.patch(
            "/v1/work-tracking/saved-queries/s1", json={"description": "changed"}
        )
        deleted = client.delete("/v1/work-tracking/saved-queries/s1")
    assert created.status_code == 403
    assert updated.status_code == 403
    assert deleted.status_code == 403
    assert "s1" in repo.rows


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
        global_update = client.patch(
            "/v1/work-tracking/saved-queries/s3", json={"name": "alpha p1"}
        )
    assert updated.status_code == 200
    assert updated.json()["query"] == "status = Done"
    assert updated.json()["description"] == "now done"
    assert repo.rows["s1"].query == "status = Done"
    assert repo.locked_gets == ["s1", "s3"]
    assert global_update.status_code == 403
    assert repo.rows["s3"].project_id is None


def test_update_saved_query_rejects_ignored_ownership_fields() -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)

    with TestClient(app) as client:
        response = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"project_id": "p2"},
        )

    assert response.status_code == 422
    assert repo.rows["s1"].project_id == "p1"
    assert repo.locked_gets == []


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
    assert repo.locked_gets == ["s1", "s2"]
    assert "s1" not in repo.rows

    viewer_app, _, _ = make_app(identity(role=Role.VIEWER), repo=repo)
    with TestClient(viewer_app) as client:
        assert client.delete("/v1/work-tracking/saved-queries/s3").status_code == 403
