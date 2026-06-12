"""/logs/search routes: window defaulting, scoping, connection selection, errors."""

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.adapters.elk.log_search import ElkLogEntry
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import LogEntry, LogQuery, LogSearchResult, Page, TimeWindow
from apex.routers.logs import get_log_search_resolver, router
from apex.services.log_search import effective_window

RESULT = LogSearchResult(
    entries=[
        ElkLogEntry(
            at="2026-06-10T11:59:59Z",
            level="ERROR",
            service="payment-svc",
            message="upstream timeout",
            fields={"trace": {"id": "abc123"}},
        ),
        LogEntry(at="2026-06-10T11:59:58Z", level="INFO", service="checkout-api", message="ok"),
    ],
    total=42,
)


class FakeLogSearchAdapter:
    def __init__(self, result: LogSearchResult = RESULT, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[LogQuery, TimeWindow, Page]] = []

    async def search(self, query: LogQuery, *, window: TimeWindow, page: Page) -> LogSearchResult:
        self.calls.append((query, window, page))
        if self.error is not None:
            raise self.error
        return self.result


class FakeResolver:
    def __init__(self, adapter: FakeLogSearchAdapter, error: Exception | None = None) -> None:
        self.adapter = adapter
        self.error = error
        self.calls: list[tuple[str | None, str | None]] = []

    async def __call__(self, connection_id: str | None, project_id: str | None) -> object:
        self.calls.append((connection_id, project_id))
        if self.error is not None:
            raise self.error
        return self.adapter


def identity(role: Role = Role.VIEWER, scopes: list[ScopeRef] | None = None) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=scopes or [],
    )


def make_app(resolver: FakeResolver, who: ConsumerIdentity) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: who
    app.dependency_overrides[get_log_search_resolver] = lambda: resolver
    return app


def post_search(app: FastAPI, body: dict | None = None, **kwargs) -> httpx.Response:
    with TestClient(app) as client:
        return client.post("/v1/logs/search", json=body or {}, **kwargs)


# ── happy path + response shape ───────────────────────────────────────────────


def test_search_maps_entries_total_and_extras_for_any_authenticated_role() -> None:
    adapter = FakeLogSearchAdapter()
    response = post_search(
        make_app(FakeResolver(adapter), identity(role=Role.VIEWER)), {"query": {"text": "timeout"}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 42
    assert body["limit"] == 50 and body["offset"] == 0
    assert body["entries"][0] == {
        "at": "2026-06-10T11:59:59Z",
        "level": "ERROR",
        "service": "payment-svc",
        "message": "upstream timeout",
        "fields": {"trace": {"id": "abc123"}},
    }
    assert body["entries"][1]["fields"] == {}  # plain LogEntry has no extras
    (query, _, page) = adapter.calls[0]
    assert query.query == "timeout" and query.filters == {}
    assert (page.offset, page.limit) == (0, 50)


def test_search_defaults_window_to_last_hour_and_echoes_it() -> None:
    adapter = FakeLogSearchAdapter()
    before = datetime.now(UTC)
    response = post_search(make_app(FakeResolver(adapter), identity()), {})
    after = datetime.now(UTC)
    assert response.status_code == 200
    (_, window, _) = adapter.calls[0]
    start = datetime.fromisoformat(window.start or "")
    end = datetime.fromisoformat(window.end or "")
    assert (end - start).total_seconds() == 3600.0
    assert before <= end <= after
    assert response.json()["window"] == {"from": window.start, "to": window.end}


def test_search_passes_explicit_window_through() -> None:
    adapter = FakeLogSearchAdapter()
    body = {"window": {"from": "2026-06-01T00:00:00+00:00", "to": "2026-06-02T00:00:00+00:00"}}
    response = post_search(make_app(FakeResolver(adapter), identity()), body)
    assert response.status_code == 200
    (_, window, _) = adapter.calls[0]
    assert window == TimeWindow(start="2026-06-01T00:00:00+00:00", end="2026-06-02T00:00:00+00:00")
    assert response.json()["window"] == {
        "from": "2026-06-01T00:00:00+00:00",
        "to": "2026-06-02T00:00:00+00:00",
    }


def test_search_forwards_filters_including_thread_id_convention() -> None:
    adapter = FakeLogSearchAdapter()
    body = {"query": {"filters": {"thread_id": "thread-99", "service": "payment-svc"}}}
    assert post_search(make_app(FakeResolver(adapter), identity()), body).status_code == 200
    (query, _, _) = adapter.calls[0]
    assert query.filters == {"thread_id": "thread-99", "service": "payment-svc"}
    assert query.query == ""  # no text -> blank query_string is omitted by adapters


# ── window validation ─────────────────────────────────────────────────────────


def test_search_rejects_non_iso_window_with_422() -> None:
    adapter = FakeLogSearchAdapter()
    response = post_search(
        make_app(FakeResolver(adapter), identity()), {"window": {"from": "yesterday-ish"}}
    )
    assert response.status_code == 422
    assert "ISO-8601" in response.json()["title"]
    assert adapter.calls == []


def test_search_rejects_inverted_window_with_422() -> None:
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter()), identity()),
        {"window": {"from": "2026-06-02T00:00:00+00:00", "to": "2026-06-01T00:00:00+00:00"}},
    )
    assert response.status_code == 422


def test_effective_window_mixes_naive_and_aware_bounds() -> None:
    window = effective_window("2026-06-01T00:00:00", "2026-06-02T00:00:00+00:00")
    assert window.start == "2026-06-01T00:00:00"


def test_search_limit_above_500_fails_validation() -> None:
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter()), identity()), {"limit": 501}
    )
    assert response.status_code == 422


# ── connection selection + project scoping ────────────────────────────────────


def test_connection_id_from_body_reaches_resolver() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    assert (
        post_search(make_app(resolver, identity()), {"connection_id": "conn-elk-prod"}).status_code
        == 200
    )
    assert resolver.calls == [("conn-elk-prod", None)]


def test_connection_id_query_param_overrides_body() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    response = post_search(
        make_app(resolver, identity()),
        {"connection_id": "conn-body"},
        params={"connection_id": "conn-param"},
    )
    assert response.status_code == 200
    assert resolver.calls == [("conn-param", None)]


def test_single_project_scope_is_passed_to_resolver() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1")])
    assert post_search(make_app(resolver, who), {}).status_code == 200
    assert resolver.calls == [(None, "p1")]


def test_multi_project_scope_resolves_globally() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    assert post_search(make_app(resolver, who), {}).status_code == 200
    assert resolver.calls == [(None, None)]


# ── error translation ─────────────────────────────────────────────────────────


def test_unknown_connection_keyerror_is_404_problem() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter(), error=KeyError("unknown connection_id 'nope'"))
    response = post_search(make_app(resolver, identity()), {"connection_id": "nope"})
    assert response.status_code == 404
    assert "unknown connection_id" in response.json()["title"]


def test_disabled_connection_valueerror_is_422_problem() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter(), error=ValueError("connection 'x' is disabled"))
    assert post_search(make_app(resolver, identity()), {}).status_code == 422


def test_provider_rejected_query_is_422_with_reason() -> None:
    adapter = FakeLogSearchAdapter(
        error=ValueError("elasticsearch rejected the query: Failed to parse query [service:(]")
    )
    response = post_search(
        make_app(FakeResolver(adapter), identity()), {"query": {"text": "service:("}}
    )
    assert response.status_code == 422
    assert "Failed to parse query" in response.json()["title"]
    assert response.headers["content-type"].startswith("application/problem+json")


def test_upstream_runtime_error_is_502_problem() -> None:
    adapter = FakeLogSearchAdapter(error=RuntimeError("elasticsearch search failed (status 503)"))
    response = post_search(make_app(FakeResolver(adapter), identity()), {})
    assert response.status_code == 502
    assert "upstream failure" in response.json()["title"]


def test_raw_httpx_error_is_502_problem() -> None:
    adapter = FakeLogSearchAdapter(error=httpx.ConnectTimeout("connect timeout"))
    assert post_search(make_app(FakeResolver(adapter), identity()), {}).status_code == 502
