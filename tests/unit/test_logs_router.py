"""/logs/search routes: window defaulting, scoping, connection selection, errors."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apex.adapters.elk.log_search import ElkLogEntry
from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.integrations import LogEntry, LogQuery, LogSearchResult, Page, TimeWindow
from apex.routers.logs import LogSearchRequest, get_log_search_resolver, router
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


def test_log_search_body_rejects_nul_connection_id() -> None:
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        LogSearchRequest.model_validate({"connection_id": "conn\x00id"})


@pytest.mark.parametrize(
    "payload",
    [
        {"query": {"text": "secret\x00suffix"}},
        {"query": {"filters": {"service": "api\x00shadow"}}},
        {"window": {"from": "2026-01-01T00:00:00Z\x00shadow"}},
    ],
)
def test_log_search_body_rejects_nul_provider_inputs(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LogSearchRequest.model_validate(payload)


class FakeLogSearchAdapter:
    def __init__(self, result: LogSearchResult = RESULT, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[LogQuery, TimeWindow, Page]] = []
        self.close_calls = 0

    async def search(self, query: LogQuery, *, window: TimeWindow, page: Page) -> LogSearchResult:
        self.calls.append((query, window, page))
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.close_calls += 1


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
    actual_scopes = (
        [ScopeRef(project_id="p1")] if scopes is None and role is not Role.ADMIN else scopes or []
    )
    return ConsumerIdentity(
        consumer_id="c1",
        name="viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=actual_scopes,
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
    assert query.query == "timeout" and query.filters == {"project_id": "p1"}
    assert (page.offset, page.limit) == (0, 50)
    assert adapter.close_calls == 1


def test_search_redacts_provider_credentials_from_messages_and_extras() -> None:
    message_canary = "message-secret-canary-4f9d"
    field_canary = "field-secret-canary-8a2c"
    bearer_canary = "bearer-secret-canary-3b7e"
    result = LogSearchResult(
        entries=[
            ElkLogEntry(
                at="2026-06-10T11:59:59Z",
                level="ERROR",
                service="payment-svc",
                message=f"password={message_canary}",
                fields={
                    "authorization": field_canary,
                    "request": {"header": f"Bearer {bearer_canary}"},
                },
            )
        ],
        total=1,
    )
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 200
    assert message_canary not in response.text
    assert field_canary not in response.text
    assert bearer_canary not in response.text
    entry = response.json()["entries"][0]
    assert "[REDACTED]" in entry["message"]
    assert entry["fields"]["[redacted-credential-key]"] == "[REDACTED]"
    assert entry["fields"]["request"]["header"] == "Bearer [REDACTED]"


def test_search_rejects_adapter_result_larger_than_requested_page() -> None:
    result = LogSearchResult(
        entries=[
            LogEntry(at="2026-06-10T11:59:59Z", message="one"),
            LogEntry(at="2026-06-10T11:59:58Z", message="two"),
        ],
        total=2,
    )
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {"limit": 1},
    )

    assert response.status_code == 502


def test_search_rejects_hostile_provider_entry_list_without_traversal() -> None:
    class HostileEntries(list[LogEntry]):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("provider list length must not be called")

        def __iter__(self) -> Iterator[LogEntry]:
            self.called = True
            raise AssertionError("provider list iteration must not be called")

        def __getitem__(self, index: Any) -> Any:
            self.called = True
            raise AssertionError("provider list indexing must not be called")

    entries = HostileEntries([LogEntry(at="2026-06-10T11:59:59Z", message="safe")])
    result = LogSearchResult.model_construct(entries=entries, total=1)

    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert entries.called is False


def test_search_rejects_arbitrary_entry_without_reading_spoofed_class() -> None:
    class HostileEntry:
        class_called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_called = True
                raise AssertionError("provider __class__ descriptor must not be called")
            return object.__getattribute__(self, name)

    entry = HostileEntry()
    result = LogSearchResult.model_construct(entries=[entry], total=1)

    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 502
    assert entry.class_called is False


def test_search_reconstructs_log_subclass_without_invoking_field_descriptors() -> None:
    class DescriptorLogEntry(LogEntry):
        called: ClassVar[bool] = False

        def __getattribute__(self, name: str) -> Any:
            if name in {"at", "level", "service", "message", "fields"}:
                type(self).called = True
                raise AssertionError("provider field descriptor must not be invoked")
            return super().__getattribute__(name)

    entry = DescriptorLogEntry.model_construct(
        at="2026-06-10T11:59:59Z",
        level="INFO",
        service="checkout",
        message="safe",
    )
    DescriptorLogEntry.called = False
    result = LogSearchResult.model_construct(entries=[entry], total=1)

    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 200
    assert response.json()["entries"][0]["message"] == "safe"
    assert DescriptorLogEntry.called is False


def test_search_rejects_hostile_log_fields_mapping_without_traversal() -> None:
    class HostileFields(dict[str, Any]):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("provider mapping length must not be called")

        def items(self) -> Any:
            self.called = True
            raise AssertionError("provider mapping items must not be called")

        def __iter__(self) -> Iterator[str]:
            self.called = True
            raise AssertionError("provider mapping iteration must not be called")

    class ExtendedLogEntry(LogEntry):
        fields: dict[str, Any]

    fields = HostileFields(safe="value")
    entry = ExtendedLogEntry.model_construct(
        at="2026-06-10T11:59:59Z",
        level="INFO",
        service="checkout",
        message="safe",
        fields=fields,
    )
    result = LogSearchResult.model_construct(entries=[entry], total=1)

    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert fields.called is False


def test_search_rejects_forged_result_key_before_rehashing_it() -> None:
    class HostileKey(str):
        hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            if self.hash_calls > 1:
                raise AssertionError("forged provider key must not be rehashed")
            return str.__hash__(self)

    result = LogSearchResult(entries=[], total=0)
    state = cast(dict[Any, Any], result.__dict__)
    state.pop("total")
    key = HostileKey("total")
    state[key] = 0

    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter(result)), identity()),
        {},
    )

    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert key.hash_calls == 1


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
    assert query.filters == {"thread_id": "thread-99", "service": "payment-svc", "project_id": "p1"}
    assert query.query == ""  # no text -> blank query_string is omitted by adapters


def test_search_rejects_unsupported_filter_fields_before_adapter_resolution() -> None:
    adapter = FakeLogSearchAdapter()
    resolver = FakeResolver(adapter)
    canary = "caller-controlled-filter-canary"
    response = post_search(
        make_app(resolver, identity()),
        {"query": {"filters": {canary: "secret", "thread_id": "thread-99"}}},
    )
    assert response.status_code == 422
    assert response.json()["errors"] == [
        {
            "type": "value_error",
            "loc": ["body", "<field>", "<field>"],
            "msg": "Invalid request value",
        }
    ]
    assert canary.encode() not in response.content
    assert resolver.calls == []
    assert adapter.calls == []


def test_search_rejects_blank_and_oversized_filter_values() -> None:
    app = make_app(FakeResolver(FakeLogSearchAdapter()), identity())
    blank = post_search(app, {"query": {"filters": {"service": "  "}}})
    assert blank.status_code == 422

    oversized = post_search(app, {"query": {"filters": {"thread_id": "x" * 513}}})
    assert oversized.status_code == 422


def test_search_rejects_oversized_free_text_query() -> None:
    app = make_app(FakeResolver(FakeLogSearchAdapter()), identity())
    response = post_search(app, {"query": {"text": "x" * 2049}})
    assert response.status_code == 422


# ── window validation ─────────────────────────────────────────────────────────


def test_search_rejects_non_iso_window_with_422() -> None:
    adapter = FakeLogSearchAdapter()
    canary = "Bearer definitely-not-for-reflection"
    response = post_search(
        make_app(FakeResolver(adapter), identity()), {"window": {"from": canary}}
    )
    assert response.status_code == 422
    assert response.json()["title"] == "invalid log search window"
    assert canary not in response.text
    assert adapter.calls == []


def test_search_rejects_inverted_window_with_422() -> None:
    start = "2026-06-02T00:00:00+00:00"
    end = "2026-06-01T00:00:00+00:00"
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter()), identity()),
        {"window": {"from": start, "to": end}},
    )
    assert response.status_code == 422
    assert response.json()["title"] == "invalid log search window"
    assert start not in response.text
    assert end not in response.text


def test_search_bounds_partial_windows_before_provider_io() -> None:
    adapter = FakeLogSearchAdapter()
    app = make_app(FakeResolver(adapter), identity())

    from_only = post_search(app, {"window": {"from": "2000-01-01T00:00:00+00:00"}})
    assert from_only.status_code == 422
    assert adapter.calls == []

    to_only = post_search(app, {"window": {"to": "2026-06-02T00:00:00+00:00"}})
    assert to_only.status_code == 200
    (_, window, _) = adapter.calls[0]
    assert datetime.fromisoformat(window.end or "") - datetime.fromisoformat(
        window.start or ""
    ) == timedelta(hours=1)


def test_search_rejects_partial_window_datetime_underflow_before_resolver_io() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    response = post_search(
        make_app(resolver, identity()),
        {"window": {"to": "0001-01-01T00:00:00+00:00"}},
    )

    assert response.status_code == 422
    assert response.json()["title"] == "invalid log search window"
    assert resolver.calls == []
    assert resolver.adapter.calls == []


def test_search_rejects_wide_window_before_adapter_resolution() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    response = post_search(
        make_app(resolver, identity()),
        {
            "window": {
                "from": "2026-01-01T00:00:00+00:00",
                "to": "2026-03-01T00:00:00+00:00",
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["title"] == "invalid log search window"
    assert resolver.calls == []


def test_effective_window_mixes_naive_and_aware_bounds() -> None:
    window = effective_window("2026-06-01T00:00:00", "2026-06-02T00:00:00+00:00")
    assert window.start == "2026-06-01T00:00:00"


def test_effective_window_detaches_invalid_timestamp_exception() -> None:
    with pytest.raises(ValueError, match="ISO-8601") as raised:
        effective_window("caller-controlled-timestamp", None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_search_limit_above_500_fails_validation() -> None:
    response = post_search(
        make_app(FakeResolver(FakeLogSearchAdapter()), identity()), {"limit": 501}
    )
    assert response.status_code == 422


def test_search_rejects_deep_result_window_before_adapter_resolution() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    app = make_app(resolver, identity())

    too_deep = post_search(app, {"offset": 10_000, "limit": 1})
    crossing = post_search(app, {"offset": 9_900, "limit": 101})

    assert too_deep.status_code == 422
    assert crossing.status_code == 422
    assert resolver.calls == []


# ── connection selection + project scoping ────────────────────────────────────


def test_connection_id_from_body_reaches_resolver() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    assert (
        post_search(make_app(resolver, identity()), {"connection_id": "conn-elk-prod"}).status_code
        == 200
    )
    assert resolver.calls == [("conn-elk-prod", "p1")]


def test_connection_id_query_param_overrides_body() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    response = post_search(
        make_app(resolver, identity()),
        {"connection_id": "conn-body"},
        params={"connection_id": "conn-param"},
    )
    assert response.status_code == 200
    assert resolver.calls == [("conn-param", "p1")]


@pytest.mark.parametrize("connection_id", ["x" * 33, "conn\x00logs"])
def test_connection_id_query_rejects_invalid_values_before_resolver_io(
    connection_id: str,
) -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())

    response = post_search(
        make_app(resolver, identity()),
        {},
        params={"connection_id": connection_id},
    )

    assert response.status_code == 422
    assert resolver.calls == []
    assert resolver.adapter.calls == []


def test_single_project_scope_is_passed_to_resolver() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1")])
    assert post_search(make_app(resolver, who), {}).status_code == 200
    assert resolver.calls == [(None, "p1")]


def test_multi_project_scope_requires_project_filter() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    response = post_search(make_app(resolver, who), {})
    assert response.status_code == 403
    assert resolver.calls == []


def test_multi_project_scope_accepts_allowed_project_filter() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1"), ScopeRef(project_id="p2")])
    response = post_search(make_app(resolver, who), {"query": {"filters": {"project_id": "p2"}}})
    assert response.status_code == 200
    assert resolver.calls == [(None, "p2")]


def test_project_filter_outside_scope_is_403_before_adapter_resolution() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1")])
    response = post_search(make_app(resolver, who), {"query": {"filters": {"project_id": "p9"}}})
    assert response.status_code == 403
    assert resolver.calls == []


def test_single_app_scope_injects_app_filter() -> None:
    adapter = FakeLogSearchAdapter()
    resolver = FakeResolver(adapter)
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")])

    response = post_search(make_app(resolver, who), {})

    assert response.status_code == 200
    assert resolver.calls == [(None, "p1")]
    assert adapter.calls[0][0].filters == {"project_id": "p1", "app_id": "app-a"}


def test_app_scope_rejects_sibling_app_before_adapter_resolution() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")])

    response = post_search(
        make_app(resolver, who),
        {"query": {"filters": {"project_id": "p1", "app_id": "app-b"}}},
    )

    assert response.status_code == 403
    assert response.json()["title"] == "app is outside this consumer's scopes"
    assert resolver.calls == []


def test_app_scope_denial_does_not_reflect_filter_identifiers() -> None:
    canary = "log-filter-secret-canary"
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(scopes=[ScopeRef(project_id="p1", app_id="app-a")])

    response = post_search(
        make_app(resolver, who),
        {"query": {"filters": {"project_id": "p1", "app_id": canary}}},
    )

    assert response.status_code == 403
    assert response.json()["title"] == "app is outside this consumer's scopes"
    assert canary not in response.text
    assert resolver.calls == []


def test_multi_app_scope_requires_and_accepts_explicit_app_filter() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter())
    who = identity(
        scopes=[
            ScopeRef(project_id="p1", app_id="app-a"),
            ScopeRef(project_id="p1", app_id="app-b"),
        ]
    )
    app = make_app(resolver, who)

    missing = post_search(app, {"query": {"filters": {"project_id": "p1"}}})
    selected = post_search(
        app,
        {"query": {"filters": {"project_id": "p1", "app_id": "app-b"}}},
    )

    assert missing.status_code == 403
    assert selected.status_code == 200
    assert resolver.calls == [(None, "p1")]


# ── error translation ─────────────────────────────────────────────────────────


def test_unknown_connection_keyerror_is_404_problem() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter(), error=KeyError("unknown connection_id 'nope'"))
    response = post_search(make_app(resolver, identity()), {"connection_id": "nope"})
    assert response.status_code == 404
    assert response.json()["title"] == "log-search connection not found"
    assert "nope" not in response.text


def test_disabled_connection_valueerror_is_422_problem() -> None:
    resolver = FakeResolver(FakeLogSearchAdapter(), error=ValueError("connection 'x' is disabled"))
    assert post_search(make_app(resolver, identity()), {}).status_code == 422


def test_arbitrary_resolver_error_is_stable_503_without_reflection() -> None:
    secret = "log-resolver-secret-canary"
    resolver = FakeResolver(
        FakeLogSearchAdapter(),
        error=HTTPException(status_code=418, detail=secret),
    )

    response = post_search(make_app(resolver, identity()), {})

    assert response.status_code == 503
    assert response.json()["title"] == "log-search connection unavailable"
    assert secret not in response.text


def test_provider_rejected_query_is_422_without_upstream_reason() -> None:
    adapter = FakeLogSearchAdapter(
        error=ValueError("elasticsearch rejected the query: Failed to parse query [service:(]")
    )
    response = post_search(
        make_app(FakeResolver(adapter), identity()), {"query": {"text": "service:("}}
    )
    assert response.status_code == 422
    assert response.json()["title"] == "log provider rejected the query"
    assert "Failed to parse query" not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")


def test_upstream_runtime_error_is_502_problem() -> None:
    adapter = FakeLogSearchAdapter(error=RuntimeError("elasticsearch search failed (status 503)"))
    response = post_search(make_app(FakeResolver(adapter), identity()), {})
    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert "elasticsearch" not in response.text


def test_arbitrary_provider_http_error_is_stable_502_without_reflection() -> None:
    secret = "log-provider-http-secret-canary"
    adapter = FakeLogSearchAdapter(error=HTTPException(status_code=418, detail=secret))

    response = post_search(make_app(FakeResolver(adapter), identity()), {})

    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert secret not in response.text


def test_log_adapter_cleanup_failure_does_not_replace_success() -> None:
    secret = "log-adapter-close-secret-canary"

    class CloseFailureAdapter(FakeLogSearchAdapter):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise HTTPException(status_code=418, detail=secret)

    adapter = CloseFailureAdapter()
    response = post_search(make_app(FakeResolver(adapter), identity()), {})

    assert response.status_code == 200
    assert response.json()["total"] == 42
    assert secret not in response.text
    assert adapter.close_calls == 1


def test_malformed_successful_provider_payload_is_sanitized_502_problem() -> None:
    adapter = FakeLogSearchAdapter(
        error=RuntimeError("elasticsearch search response has malformed hits.hits list")
    )
    response = post_search(make_app(FakeResolver(adapter), identity()), {})

    assert response.status_code == 502
    assert response.json()["title"] == "log search upstream failure"
    assert "hits.hits" not in response.text


def test_raw_httpx_error_is_502_problem() -> None:
    adapter = FakeLogSearchAdapter(error=httpx.ConnectTimeout("connect timeout"))
    assert post_search(make_app(FakeResolver(adapter), identity()), {}).status_code == 502
