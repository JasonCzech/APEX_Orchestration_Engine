"""/work-tracking routes: fake adapter via resolver override, scoping, saved queries."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
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
from apex.persistence.repositories.saved_queries import SavedQueryNameConflictError
from apex.routers.work_tracking import (
    ExecuteQueryRequest,
    SavedQueryOut,
    TranslateQueryRequest,
    router,
)
from apex.services import work_item_mutations as mutation_module
from apex.services import work_items as provider_work_items
from apex.services.work_item_mutations import (
    WorkItemMutationOutcomeAmbiguousError,
    WorkItemMutationService,
    get_work_item_mutation_service,
)
from apex.services.work_tracking import (
    get_saved_queries_repository,
    get_work_tracking_resolver,
)

DEFAULT_CONNECTION_ID = "dev-work-tracking-fake"

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
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1

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

    async def resolve_with_connection_id(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Any, str]:
        adapter = await self.resolve(
            kind,
            connection_id=connection_id,
            project_id=project_id,
        )
        return adapter, connection_id or DEFAULT_CONNECTION_ID


class MetadataResolver:
    """Separates metadata selection from adapter construction for replay tests."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.metadata_calls: list[tuple[PortKind, str | None, str | None]] = []
        self.build_calls = 0
        self.fail_build = False
        self.metadata_connection_id = DEFAULT_CONNECTION_ID
        self.built_connection_id = DEFAULT_CONNECTION_ID
        self.built_persisted = False
        self.built_connection_version: datetime | None = None

    async def resolve_metadata(
        self,
        kind: PortKind,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        self.metadata_calls.append((kind, connection_id, project_id))
        return SimpleNamespace(
            config=SimpleNamespace(id=self.metadata_connection_id, provider="fake"),
            persisted=False,
            connection_version=None,
        )

    async def build_from_metadata(self, _metadata: Any) -> Any:
        self.build_calls += 1
        if self.fail_build:
            raise RuntimeError("secret store unavailable")
        return SimpleNamespace(
            adapter=self.adapter,
            connection_id=self.built_connection_id,
            persisted=self.built_persisted,
            connection_version=self.built_connection_version,
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
            raise SavedQueryNameConflictError(
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
            raise SavedQueryNameConflictError(
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
    row_id: str,
    name: str,
    project_id: str | None,
    provider: str = "jira",
    connection_id: str | None = None,
) -> SavedQuery:
    now = datetime.now(UTC)
    return SavedQuery(
        id=row_id,
        name=name,
        project_id=project_id,
        provider=provider,
        query="project = PHX",
        connection_id=connection_id,
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
    assert body["connection_id"] == DEFAULT_CONNECTION_ID
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


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/v1/work-tracking/query/translate",
            {"text": "open bugs", "connection_id": "from-body"},
        ),
        (
            "/v1/work-tracking/query/execute",
            {
                "query": {"provider": "fake", "query": "project = PHX"},
                "connection_id": "from-body",
            },
        ),
    ],
)
def test_query_rejects_conflicting_body_and_query_connection_ids(
    path: str,
    payload: dict[str, Any],
) -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            path,
            params={"connection_id": "from-query"},
            json=payload,
        )
    assert response.status_code == 409
    assert response.json()["title"] == "conflicting work-tracking connection ids"
    assert resolver.calls == []


def test_binding_returns_exact_authorized_connection_identity() -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        response = client.get(
            "/v1/work-tracking/binding",
            params={"connection_id": "tracker-p1"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "connection_id": "tracker-p1",
        "provider": "fake",
    }
    assert resolver.calls == [(PortKind.WORK_TRACKING, "tracker-p1", "p1")]
    assert resolver.adapter.close_calls == 1


def test_translate_response_can_execute_directly_with_its_nested_binding() -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        translated = client.post(
            "/v1/work-tracking/query/translate",
            json={"text": "open bugs"},
        )
        executed = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": translated.json()},
        )

    assert translated.status_code == 200
    assert executed.status_code == 200
    assert resolver.calls == [
        (PortKind.WORK_TRACKING, None, "p1"),
        (PortKind.WORK_TRACKING, DEFAULT_CONNECTION_ID, "p1"),
    ]


def test_binding_fails_closed_when_legacy_resolver_cannot_report_selected_id() -> None:
    adapter = FakeWorkTrackingAdapter()

    class ResolveOnlyResolver:
        async def resolve(
            self,
            _kind: PortKind,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> Any:
            raise AssertionError(
                f"unbound legacy resolution must not run: {connection_id=} {project_id=}"
            )

    app, _, _ = make_app(identity(), resolver=cast(Any, ResolveOnlyResolver()))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/binding")

    assert response.status_code == 409
    assert response.json()["title"] == "work-tracking connection conflict"
    assert adapter.close_calls == 0


def test_binding_rejects_credential_shaped_provider_without_reflection() -> None:
    provider = "Bearer opaque-provider-canary"
    app, resolver, _ = make_app(
        identity(),
        resolver=FakeResolver(FakeWorkTrackingAdapter(provider=provider)),
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/work-tracking/binding",
            params={"connection_id": DEFAULT_CONNECTION_ID},
        )

    assert response.status_code == 409
    assert provider not in response.text
    assert resolver.adapter.close_calls == 1


def test_read_resolver_must_return_the_exact_requested_connection_id() -> None:
    adapter = FakeWorkTrackingAdapter()

    class MismatchedReadResolver:
        async def resolve_with_metadata(
            self,
            _kind: PortKind,
            *,
            connection_id: str | None = None,
            project_id: str | None = None,
        ) -> Any:
            assert connection_id == "requested-connection"
            assert project_id == "p1"
            return SimpleNamespace(
                adapter=adapter,
                connection_id="different-connection",
                persisted=False,
                connection_version=None,
            )

    app, _, _ = make_app(identity(), resolver=cast(Any, MismatchedReadResolver()))

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/translate",
            json={"text": "open bugs", "connection_id": "requested-connection"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "invalid work-tracking connection"
    assert adapter.translate_calls == []
    assert adapter.close_calls == 1


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


def test_translate_rejects_credential_bearing_provider_query_without_reflection() -> None:
    secret = "translated-query-secret-canary"

    class CredentialQueryAdapter(FakeWorkTrackingAdapter):
        async def translate_query(
            self,
            natural_language: str,
            *,
            context: QueryContext,
        ) -> TranslatedQuery:
            del natural_language, context
            return TranslatedQuery(provider="fake", query=f"password={secret}")

    app, _, _ = make_app(
        identity(),
        resolver=FakeResolver(CredentialQueryAdapter()),
    )
    with TestClient(app) as client:
        response = client.post("/v1/work-tracking/query/translate", json={"text": "open bugs"})

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"
    assert secret not in response.text


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
    assert body["connection_id"] == DEFAULT_CONNECTION_ID
    assert body["provider"] == "fake"
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
    "query_text",
    [
        'summary ~ "project = OTHER"',
        r'summary ~ "quoted \"project = OTHER\""',
    ],
)
def test_execute_query_ignores_project_text_in_jql_literals(query_text: str) -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": query_text}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == f"project in (PHX) AND ({query_text})"


def test_execute_query_decodes_escaped_jql_project_literal() -> None:
    external_project = 'PHX"BLUE'
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
    raw = r'project = "PHX\"BLUE" AND status = Open'

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "jira", "query": raw}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == 'project in ("PHX\\"BLUE") AND (' + raw + ")"


def test_execute_query_rejects_nested_conflicting_jira_project_scope() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira", project_id="PHX"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="PHX")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "jira",
                    "query": "status = Open OR (project IN (PHX, OTHER))",
                }
            },
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


def test_execute_query_accepts_wiql_literal_ending_in_backslash() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)
    raw = "SELECT [System.Id] FROM WorkItems WHERE [System.Title] = 'folder\\'"

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "ado", "query": raw}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT') AND ([System.Title] = 'folder\\')"
    )


def test_execute_query_preserves_wiql_asof_after_injected_where() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": ("SELECT [System.Id] FROM WorkItems ASOF '02-11-2025 00:00:00Z'"),
                }
            },
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT') ASOF '02-11-2025 00:00:00Z'"
    )


def test_execute_query_preserves_wiql_order_and_asof_suffix() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Open' "
                        "ORDER BY [System.Id] ASOF '02-11-2025'"
                    ),
                }
            },
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT') AND ([System.State] = 'Open') "
        "ORDER BY [System.Id] ASOF '02-11-2025'"
    )


def test_execute_query_preserves_wiql_mode_and_scopes_both_link_ends() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItemLinks WHERE "
                        "([Source].[System.WorkItemType] = 'Bug') AND "
                        "([Target].[System.WorkItemType] = 'Task') "
                        "ORDER BY [System.Id] MODE (MustContain)"
                    ),
                }
            },
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItemLinks WHERE "
        "([Source].[System.TeamProject] IN ('CAT') AND "
        "[Target].[System.TeamProject] IN ('CAT')) AND "
        "(([Source].[System.WorkItemType] = 'Bug') AND "
        "([Target].[System.WorkItemType] = 'Task')) "
        "ORDER BY [System.Id] MODE (MustContain)"
    )


def test_execute_query_does_not_treat_wiql_field_names_as_suffixes() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": ("SELECT [Custom.ASOF] FROM WorkItems WHERE [Custom.Mode] = 'open'"),
                }
            },
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [Custom.ASOF] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT') AND ([Custom.Mode] = 'open')"
    )


def test_execute_query_does_not_treat_wiql_macros_as_suffixes() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItems "
                        "WHERE [Custom.Mode] = @mode ASOF '02-11-2025'"
                    ),
                }
            },
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT') AND ([Custom.Mode] = @mode) "
        "ASOF '02-11-2025'"
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "[System.Title] CONTAINS '[System.TeamProject] = ''OTHER'''",
        "[System.Title] CONTAINS 'folder\\[System.TeamProject] = ''OTHER'''",
    ],
)
def test_execute_query_ignores_project_text_in_wiql_literals(predicate: str) -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)
    raw = f"SELECT [System.Id] FROM WorkItems WHERE {predicate}"

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "ado", "query": raw}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] IN ('CAT') AND ({predicate})"
    )


def test_execute_query_decodes_doubled_wiql_project_literal() -> None:
    external_project = "CAT'S"
    resolver = FakeResolver(
        FakeWorkTrackingAdapter(
            provider="ado",
            project_id=external_project,
            internal_project_id="internal-project-1",
        )
    )
    app, resolver, _ = make_app(
        identity(scopes=[ScopeRef(project_id="internal-project-1")]), resolver=resolver
    )
    raw = "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'CAT''S'"

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={"query": {"provider": "ado", "query": raw}},
        )

    assert response.status_code == 200
    query, _ = resolver.adapter.execute_calls[0]
    assert query.query == (
        "SELECT [System.Id] FROM WorkItems WHERE "
        "[System.TeamProject] IN ('CAT''S') AND "
        "([System.TeamProject] = 'CAT''S')"
    )


def test_execute_query_rejects_nested_conflicting_wiql_project_scope() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItems WHERE "
                        "([System.State] = 'Open' OR "
                        "([System.TeamProject] IN ('CAT', 'OTHER')))"
                    ),
                }
            },
        )

    assert response.status_code == 403
    assert resolver.adapter.execute_calls == []


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


def test_execute_query_rejects_wiql_backslash_quote_scope_escape() -> None:
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="ado", project_id="CAT"))
    app, resolver, _ = make_app(identity(scopes=[ScopeRef(project_id="CAT")]), resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/query/execute",
            json={
                "query": {
                    "provider": "ado",
                    "query": (
                        "SELECT [System.Id] FROM WorkItems WHERE "
                        "[System.State] = 'Open\\') OR "
                        "[System.TeamProject] = 'OTHER' OR ('x' = 'x'"
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


def test_unexpected_resolver_error_is_stable_503_without_reflection() -> None:
    secret = "resolver-runtime-secret-canary"
    resolver = FakeResolver(FakeWorkTrackingAdapter())
    resolver.raises = HTTPException(status_code=418, detail=secret)
    app, _, _ = make_app(identity(), resolver=resolver)

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 503
    assert response.json()["title"] == "work-tracking connection unavailable"
    assert secret not in response.text


def test_adapter_provider_scalar_subclass_is_rejected_without_hooks() -> None:
    secret = "provider-scalar-hook-secret-canary"

    class HostileProvider(str):
        def strip(self, *_args: Any, **_kwargs: Any) -> str:
            raise AssertionError(secret)

        def casefold(self) -> str:
            raise AssertionError(secret)

    adapter = FakeWorkTrackingAdapter(provider=cast(str, HostileProvider("jira")))
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 409
    assert response.json()["title"] == "invalid work-tracking connection"
    assert secret not in response.text


def test_external_project_scalar_subclass_is_rejected_without_hooks() -> None:
    secret = "external-project-hook-secret-canary"

    class HostileProject(str):
        def strip(self, *_args: Any, **_kwargs: Any) -> str:
            raise AssertionError(secret)

    adapter = FakeWorkTrackingAdapter(
        provider="jira",
        project_id=cast(str, HostileProject("p1")),
        internal_project_id="p1",
    )
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 409
    assert response.json()["title"] == (
        "resolved work-tracking adapter has no external project configured"
    )
    assert secret not in response.text


# ── Item passthrough ─────────────────────────────────────────────────────────


def test_get_work_item_found_and_missing() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        found = client.get("/v1/work-tracking/items/PHX-1")
        missing = client.get("/v1/work-tracking/items/PHX-404")
    assert found.status_code == 200
    assert found.json()["title"] == "First"
    assert found.json()["connection_id"] == DEFAULT_CONNECTION_ID
    assert found.json()["provider"] == "fake"
    assert missing.status_code == 404
    assert missing.json()["title"] == "work item not found"
    assert "PHX-404" not in missing.text


def test_get_work_item_rejects_a_different_provider_key() -> None:
    adapter = FakeWorkTrackingAdapter()
    adapter.items["PHX-1"] = adapter.items["PHX-1"].model_copy(
        update={"key": "PHX-2"}
    )
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"


def test_get_work_item_redacts_descriptive_provider_credentials() -> None:
    secret = "work-item-response-secret-canary"
    adapter = FakeWorkTrackingAdapter()
    adapter.items["PHX-1"] = adapter.items["PHX-1"].model_copy(
        update={
            "title": f"password={secret}",
            "description": f"Authorization: Bearer {secret}",
        }
    )
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 200
    assert secret not in response.text
    assert "[REDACTED]" in response.json()["title"]
    assert "[REDACTED]" in response.json()["description"]


@pytest.mark.parametrize(
    "update",
    [
        {"key": "password=work-item-response-secret-canary"},
        {"url": "https://tracker.example/item?token=work-item-response-secret-canary"},
    ],
)
def test_get_work_item_rejects_unsafe_provider_identity_or_url(
    update: dict[str, str],
) -> None:
    adapter = FakeWorkTrackingAdapter()
    adapter.items["PHX-1"] = adapter.items["PHX-1"].model_copy(update=update)
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 502
    assert "work-item-response-secret-canary" not in response.text


@pytest.mark.parametrize(
    ("helper_name", "call"),
    [
        (
            "_validated_provider_work_item",
            lambda: provider_work_items.validated_provider_work_item({}),
        ),
        (
            "_validated_provider_work_item_page",
            lambda: provider_work_items.validated_provider_work_item_page(
                object(), requested_page=Page(offset=0, limit=1)
            ),
        ),
        (
            "_validated_provider_query",
            lambda: provider_work_items.validated_provider_query(
                object(), expected_provider="jira"
            ),
        ),
    ],
)
def test_provider_projection_errors_detach_secret_bearing_validation_context(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    call: Any,
) -> None:
    secret = "provider-projection-raw-secret-canary"

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(provider_work_items, helper_name, explode)

    with pytest.raises(RuntimeError) as exc_info:
        call()

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


def test_invalid_provider_url_port_detaches_parser_context() -> None:
    secret = "provider-url-port-secret-canary"

    with pytest.raises(ValueError, match="unsafe work-item URL") as exc_info:
        provider_work_items._validate_work_item_url(f"https://tracker.internal:{secret}/item")

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


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
    assert response.json()["connection_id"] == "jira-acme"
    assert response.json()["provider"] == "fake"
    assert resolver.calls == [(PortKind.WORK_TRACKING, "jira-acme", "p1")]
    filters, page = resolver.adapter.list_calls[0]
    assert filters == WorkItemFilters(status="open", kind="bug", text="checkout")
    assert (page.offset, page.limit) == (2, 5)


def test_list_rejects_provider_page_larger_than_requested_limit() -> None:
    class OversizedPageAdapter(FakeWorkTrackingAdapter):
        async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
            del filters
            return WorkItemPage(items=list(self.items.values()), total=2, page=page)

    adapter = OversizedPageAdapter()
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items", params={"limit": 1})

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"


def test_list_preflights_constructed_oversized_page_before_item_projection() -> None:
    class ConstructedOversizedPageAdapter(FakeWorkTrackingAdapter):
        async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
            del filters
            item = next(iter(self.items.values()))
            return WorkItemPage.model_construct(
                items=[item] * 10_000,
                total=10_000,
                page=page,
            )

    app, _, _ = make_app(
        identity(),
        resolver=FakeResolver(ConstructedOversizedPageAdapter()),
    )
    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items", params={"limit": 1})

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"


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


@pytest.mark.parametrize(
    ("method", "path", "json_body", "headers"),
    [
        ("GET", "/v1/work-tracking/items", None, None),
        ("GET", "/v1/work-tracking/items/PHX-1", None, None),
        (
            "POST",
            "/v1/work-tracking/items",
            {"title": "must stay bounded"},
            {"Idempotency-Key": "missing-external-project-create"},
        ),
        (
            "POST",
            "/v1/work-tracking/items/PHX-1/enrich",
            {"fields": {"summary": "must stay bounded"}},
            {"Idempotency-Key": "missing-external-project-enrich"},
        ),
    ],
)
def test_scoped_real_provider_routes_require_external_project_binding(
    method: str,
    path: str,
    json_body: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> None:
    adapter = FakeWorkTrackingAdapter(
        provider="jira",
        project_id=None,
        internal_project_id="p1",
    )
    resolver = FakeResolver(adapter)
    app, resolver, _ = make_app(identity(), resolver=resolver)

    with TestClient(app) as client:
        response = client.request(
            method,
            path,
            params=({"connection_id": DEFAULT_CONNECTION_ID} if method == "POST" else None),
            json=json_body,
            headers=headers,
        )

    assert response.status_code == 409
    assert response.json()["title"] == (
        "resolved work-tracking adapter has no external project configured"
    )
    assert adapter.list_calls == []
    assert adapter.create_calls == []
    assert adapter.enrich_calls == []


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
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "New story"},
            headers={"Idempotency-Key": "create-viewer"},
        )
    assert response.status_code == 403


def test_create_work_item_created() -> None:
    app, _, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "New story", "kind": "story"},
            headers={"Idempotency-Key": "create-new-story"},
        )
    assert response.status_code == 201
    assert response.json()["key"] == "PHX-900"
    assert response.json()["connection_id"] == DEFAULT_CONNECTION_ID
    assert response.json()["provider"] == "fake"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/work-tracking/items", {"title": "No connection"}),
        (
            "/v1/work-tracking/items/PHX-1/enrich",
            {"comment": "No connection"},
        ),
    ],
)
def test_work_item_mutations_require_exact_connection_id(
    path: str,
    payload: dict[str, Any],
) -> None:
    app, resolver, _ = make_app(identity())

    with TestClient(app) as client:
        response = client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": "missing-exact-connection"},
        )

    assert response.status_code == 422
    assert any(
        error["type"] == "missing" and error["loc"][0] == "query"
        for error in response.json()["errors"]
    )
    assert resolver.calls == []
    assert resolver.adapter.created_by_marker == {}
    assert resolver.adapter.enrich_calls == []


def test_create_work_item_requires_idempotency_key() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "No key"},
        )

    assert response.status_code == 422
    assert resolver.adapter.created_by_marker == {}


def test_create_work_item_replays_and_rejects_key_payload_conflict() -> None:
    app, resolver, _ = make_app(identity())
    headers = {"Idempotency-Key": "router-replay-conflict"}
    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Stable payload"},
            headers=headers,
        )
        replay = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Stable payload"},
            headers=headers,
        )
        conflict = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
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
            params={"connection_id": DEFAULT_CONNECTION_ID},
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
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Replay without provider"},
            headers=headers,
        )
        resolver.fail_build = True
        replay = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Replay without provider"},
            headers=headers,
        )

    assert created.status_code == replay.status_code == 201
    assert replay.json() == created.json()
    assert resolver.build_calls == 1


def test_mutation_adapter_must_match_preselected_connection_identity() -> None:
    resolver = MetadataResolver(FakeWorkTrackingAdapter())
    resolver.built_connection_id = "different-connection"
    app, _, _ = make_app(identity(), resolver=resolver)  # type: ignore[arg-type]
    repository = mutation_module._EphemeralMutationRepository()
    mutations = WorkItemMutationService(repository, ephemeral_repository=repository)
    app.dependency_overrides[get_work_item_mutation_service] = lambda: mutations

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Must not dispatch"},
            headers={"Idempotency-Key": "connection-identity-mismatch"},
        )

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"
    assert resolver.adapter.created_by_marker == {}
    assert resolver.adapter.close_calls == 1


def test_mutation_metadata_must_match_the_exact_requested_connection_id() -> None:
    resolver = MetadataResolver(FakeWorkTrackingAdapter())
    resolver.metadata_connection_id = "different-connection"
    app, _, _ = make_app(identity(), resolver=resolver)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Must not build"},
            headers={"Idempotency-Key": "metadata-identity-mismatch"},
        )

    assert response.status_code == 409
    assert response.json()["title"] == "invalid work-tracking connection"
    assert resolver.metadata_calls == [(PortKind.WORK_TRACKING, DEFAULT_CONNECTION_ID, "p1")]
    assert resolver.build_calls == 0
    assert resolver.adapter.created_by_marker == {}


def test_mutation_resolver_surface_failure_is_stable_503() -> None:
    secret = "mutation-resolver-surface-secret-canary"

    class HostileResolver:
        @property
        def resolve_metadata(self) -> Any:
            raise HTTPException(status_code=418, detail=secret)

    app, _, _ = make_app(identity(), resolver=cast(Any, HostileResolver()))

    with TestClient(app) as client:
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"title": "Must not resolve"},
            headers={"Idempotency-Key": "resolver-surface-failure"},
        )

    assert response.status_code == 503
    assert response.json()["title"] == "work-tracking connection unavailable"
    assert secret not in response.text


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
            params={"connection_id": DEFAULT_CONNECTION_ID},
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
        response = client.post(
            "/v1/work-tracking/items",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json=payload,
            headers={"Idempotency-Key": "invalid-create-payload"},
        )

    assert response.status_code == 422
    assert resolver.adapter.create_calls == []


def test_enrich_work_item_roles_and_payload() -> None:
    app, resolver, _ = make_app(identity())
    with TestClient(app) as client:
        ok = client.post(
            "/v1/work-tracking/items/PHX-1/enrich",
            params={"connection_id": DEFAULT_CONNECTION_ID},
            json={"comment": "triaged", "fields": {"System.Tags": "perf"}},
            headers={"Idempotency-Key": "enrich-phx-1"},
        )
    assert ok.status_code == 200
    assert ok.json()["connection_id"] == DEFAULT_CONNECTION_ID
    assert ok.json()["provider"] == "fake"
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
            params={"connection_id": DEFAULT_CONNECTION_ID},
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


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(status_code=418, detail="adapter-http-secret-canary"),
        LookupError("adapter-generic-secret-canary"),
    ],
)
def test_arbitrary_adapter_errors_are_stable_502_without_reflection(error: Exception) -> None:
    resolver = FakeResolver(BoomAdapter(error))
    app, _, _ = make_app(identity(), resolver=resolver)

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 502
    assert response.json()["title"] == "work tracker upstream failure"
    assert "secret-canary" not in response.text


def test_adapter_cleanup_failure_does_not_replace_success() -> None:
    secret = "adapter-close-secret-canary"

    class CloseFailureAdapter(FakeWorkTrackingAdapter):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError(secret)

    adapter = CloseFailureAdapter()
    app, _, _ = make_app(identity(), resolver=FakeResolver(adapter))

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/items/PHX-1")

    assert response.status_code == 200
    assert response.json()["key"] == "PHX-1"
    assert secret not in response.text
    assert adapter.close_calls == 1


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


def scoped_jira_resolver() -> FakeResolver:
    return FakeResolver(
        FakeWorkTrackingAdapter(
            provider="jira",
            project_id="PHX",
            internal_project_id="p1",
        )
    )


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
    resolver = scoped_jira_resolver()
    app, _, _ = make_app(identity(), resolver=resolver, repo=repo)
    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "p1 opens",
                "provider": "jira",
                "query": "status = Open",
                "project_id": "p1",
                "connection_id": "jira-p1",
            },
        )
        out_of_scope = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "p2 opens",
                "provider": "jira",
                "query": "q",
                "project_id": "p2",
                "connection_id": "jira-p2",
            },
        )
        duplicate = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "alpha p1",
                "provider": "jira",
                "query": "q",
                "project_id": "p1",
                "connection_id": "jira-p1",
            },
        )
    assert created.status_code == 201
    body = created.json()
    assert body["created_by"] == "op"
    assert body["connection_id"] == "jira-p1"
    assert body["id"] in repo.rows
    assert repo.rows[body["id"]].connection_id == "jira-p1"
    assert out_of_scope.status_code == 403
    assert duplicate.status_code == 409


def test_project_saved_query_requires_matching_exact_connection() -> None:
    repo = seeded_repo()
    resolver = scoped_jira_resolver()
    app, _, _ = make_app(identity(), resolver=resolver, repo=repo)

    with TestClient(app) as client:
        missing = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "missing binding",
                "provider": "jira",
                "query": "status = Open",
                "project_id": "p1",
            },
        )
        mismatch = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "wrong provider",
                "provider": "ado",
                "query": "SELECT [System.Id] FROM WorkItems",
                "project_id": "p1",
                "connection_id": "jira-p1",
            },
        )

    assert missing.status_code == 422
    assert missing.json()["title"] == ("project saved queries require a work-tracking connection")
    assert mismatch.status_code == 409
    assert mismatch.json()["title"] == (
        "saved query provider does not match the work-tracking connection"
    )
    assert set(repo.rows) == {"s1", "s2", "s3"}


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


def test_global_saved_query_is_an_unbound_template() -> None:
    repo = seeded_repo()
    resolver = FakeResolver(FakeWorkTrackingAdapter(provider="jira"))
    app, _, _ = make_app(
        identity(role=Role.ADMIN, scopes=[]),
        resolver=resolver,
        repo=repo,
    )

    with TestClient(app) as client:
        created = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "global template",
                "provider": "jira",
                "query": "status = Open",
            },
        )
        pinned = client.post(
            "/v1/work-tracking/saved-queries",
            json={
                "name": "invalid pinned global",
                "provider": "jira",
                "query": "status = Open",
                "connection_id": "jira-global",
            },
        )

    assert created.status_code == 201
    assert created.json()["project_id"] is None
    assert created.json()["connection_id"] is None
    assert repo.rows[created.json()["id"]].connection_id is None
    assert pinned.status_code == 422
    assert pinned.json()["title"] == (
        "global saved queries must not pin a work-tracking connection"
    )
    assert resolver.calls == []


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


def test_legacy_project_saved_query_can_only_rebind_explicitly() -> None:
    repo = seeded_repo()
    assert repo.rows["s1"].connection_id is None
    resolver = scoped_jira_resolver()
    app, _, _ = make_app(identity(), resolver=resolver, repo=repo)

    with TestClient(app) as client:
        harmless = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"description": "legacy row retained until repair"},
        )
        provider_without_binding = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"provider": "jira"},
        )
        repaired = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"connection_id": "jira-p1"},
        )

    assert harmless.status_code == 200
    assert harmless.json()["connection_id"] is None
    assert provider_without_binding.status_code == 422
    assert provider_without_binding.json()["title"] == (
        "project saved queries require a work-tracking connection"
    )
    assert repaired.status_code == 200
    assert repaired.json()["connection_id"] == "jira-p1"
    assert repo.rows["s1"].connection_id == "jira-p1"
    assert resolver.calls == [(PortKind.WORK_TRACKING, "jira-p1", "p1")]


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


@pytest.mark.parametrize("field", ["name", "provider", "query"])
def test_update_saved_query_rejects_explicit_null_nonnullable_fields(field: str) -> None:
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)

    with TestClient(app) as client:
        response = client.patch("/v1/work-tracking/saved-queries/s1", json={field: None})

    assert response.status_code == 422
    assert repo.locked_gets == []
    assert getattr(repo.rows["s1"], field) is not None


def test_update_saved_query_allows_explicit_null_description() -> None:
    repo = seeded_repo()
    repo.rows["s1"].description = "old"
    app, _, _ = make_app(identity(), repo=repo)

    with TestClient(app) as client:
        response = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"description": None},
        )

    assert response.status_code == 200
    assert repo.rows["s1"].description is None


@pytest.mark.parametrize("field", ["name", "provider", "query", "description"])
def test_saved_query_writes_reject_credential_text_without_reflection(field: str) -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    repo = seeded_repo()
    app, _, _ = make_app(identity(), repo=repo)
    create_payload = {
        "name": "safe name",
        "provider": "jira",
        "query": "status = Open",
        "description": "safe description",
        "project_id": "p1",
    }
    create_payload[field] = credential

    with TestClient(app) as client:
        created = client.post("/v1/work-tracking/saved-queries", json=create_payload)
        updated = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={field: credential},
        )

    assert created.status_code == 422
    assert updated.status_code == 422
    assert credential.encode() not in created.content
    assert credential.encode() not in updated.content
    assert len(repo.rows) == 3


def test_saved_query_legacy_credential_text_is_redacted_from_output() -> None:
    credential = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    repo = seeded_repo()
    row = repo.rows["s1"]
    row.id = credential
    row.name = credential
    row.provider = credential
    row.query = credential
    row.connection_id = credential
    row.description = credential
    row.created_by = credential
    app, _, _ = make_app(identity(), repo=repo)

    with TestClient(app) as client:
        response = client.get("/v1/work-tracking/saved-queries/s1")

    assert response.status_code == 200
    assert credential.encode() not in response.content
    body = response.json()
    for field in (
        "id",
        "name",
        "provider",
        "query",
        "connection_id",
        "description",
        "created_by",
    ):
        assert body[field] == "[REDACTED]"

    row.project_id = credential
    assert SavedQueryOut.model_validate(row).project_id == "[REDACTED]"


def test_saved_query_malformed_legacy_text_is_bounded_and_quarantined() -> None:
    row = saved_query_row("legacy", "safe", "p1")
    row.name = ""
    row.provider = "p" * 65
    row.query = cast(Any, {"unexpected": "shape"})
    row.connection_id = "c" * 33
    row.description = "unsafe\x00description"
    row.project_id = "p" * 256
    row.created_by = "actor" * 52

    output = SavedQueryOut.model_validate(row)

    for field in (
        "name",
        "provider",
        "query",
        "connection_id",
        "description",
        "project_id",
        "created_by",
    ):
        assert getattr(output, field) == "[REDACTED]"


def test_update_saved_query_generic_repository_validation_is_422_not_conflict() -> None:
    repo = seeded_repo()
    canary = "not-a-duplicate-secret-canary"

    async def invalid_update(_row: SavedQuery, _changes: dict[str, Any]) -> SavedQuery:
        raise ValueError(canary)

    repo.update = cast(Any, invalid_update)
    app, _, _ = make_app(identity(), repo=repo)
    with TestClient(app) as client:
        response = client.patch(
            "/v1/work-tracking/saved-queries/s1",
            json={"description": "safe"},
        )

    assert response.status_code == 422
    assert response.json()["title"] == "invalid saved query update"
    assert canary.encode() not in response.content


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
