"""Elasticsearch log-search adapter against respx-mocked ES 8 wire fixtures."""

import json
from typing import Any

import httpx
import pytest
import respx

from apex.adapters.elk.log_search import (
    MAX_PAGE_SIZE,
    ElasticsearchLogSearchAdapter,
    ElkLogEntry,
    build_search_body,
    map_hit,
)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import LogQuery, Page, SecretValue, TimeWindow

BASE_URL = "https://elk.internal:9200"
# Shape of an ES-issued encoded API key: base64("id:api_key") — no colon.
API_KEY = "VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="

WINDOW = TimeWindow(start="2026-06-10T11:00:00+00:00", end="2026-06-10T12:00:00+00:00")
PAGE = Page(offset=0, limit=50)


def make_conn(**options: Any) -> ConnectionConfig:
    return ConnectionConfig(
        id="conn-elk",
        kind=PortKind.LOG_SEARCH,
        provider="elasticsearch",
        name="Prod ELK",
        options={"base_url": BASE_URL, **options},
    )


def make_adapter(secret: str | None = API_KEY, **options: Any) -> ElasticsearchLogSearchAdapter:
    return ElasticsearchLogSearchAdapter(
        make_conn(**options), SecretValue(value=secret) if secret is not None else None
    )


def search_route(index: str = "logs-*") -> respx.Route:
    return respx.post(f"{BASE_URL}/{index}/_search")


def sent_body(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


# Recorded-style ES 8 response: three _source shapes exercising every fallback
# chain branch (ECS object, flat/legacy dotted keys, Docker/k8s string log).
ES_RESPONSE: dict[str, Any] = {
    "took": 12,
    "timed_out": False,
    "_shards": {"total": 2, "successful": 2, "skipped": 0, "failed": 0},
    "hits": {
        "total": {"value": 1234, "relation": "gte"},
        "max_score": None,
        "hits": [
            {  # ECS-shaped document
                "_index": "logs-2026.06.10",
                "_id": "kT5KX5cBxq",
                "_score": None,
                "_source": {
                    "@timestamp": "2026-06-10T11:59:59.123Z",
                    "message": "upstream timeout after 800ms calling card-gateway",
                    "log": {"level": "error", "logger": "gateway.client"},
                    "service": {"name": "payment-svc", "version": "2.3.1"},
                    "trace": {"id": "abc123"},
                },
                "sort": [1781179199123],
            },
            {  # flat/legacy document: dotted keys, msg + severity
                "_index": "logs-2026.06.10",
                "_id": "lU5KX5cBxq",
                "_score": None,
                "_source": {
                    "timestamp": "2026-06-10T11:59:58Z",
                    "msg": "connection pool exhausted; queueing request",
                    "severity": "warn",
                    "service.name": "checkout-api",
                    "pool_size": 20,
                },
                "sort": [1781179198000],
            },
            {  # Docker/k8s document: "log" is a plain string, app label service
                "_index": "logs-2026.06.10",
                "_id": "mV5KX5cBxq",
                "_score": None,
                "_source": {
                    "@timestamp": "2026-06-10T11:59:57Z",
                    "log": "readiness probe failed: connection refused",
                    "level": "WARN",
                    "kubernetes": {
                        "labels": {"app": "cart-svc"},
                        "pod_name": "cart-svc-7f9d",
                    },
                },
                "sort": [1781179197000],
            },
        ],
    },
}

# Recorded-style ES 8 error for a malformed query_string.
ES_400: dict[str, Any] = {
    "error": {
        "root_cause": [
            {
                "type": "query_shard_exception",
                "reason": "Failed to parse query [service:(]",
                "index_uuid": "yL2TJxbnTCWvxx5xZNvNqQ",
                "index": "logs-2026.06.10",
            }
        ],
        "type": "search_phase_execution_exception",
        "reason": "all shards failed",
        "phase": "query",
        "grouped": True,
        "failed_shards": [
            {
                "shard": 0,
                "index": "logs-2026.06.10",
                "node": "Yh3hHt3TQK2vPjyMr8YS0A",
                "reason": {
                    "type": "query_shard_exception",
                    "reason": "Failed to parse query [service:(]",
                },
            }
        ],
    },
    "status": 400,
}


# ── hit mapping + search ──────────────────────────────────────────────────────


@respx.mock
async def test_search_maps_every_source_shape_and_gte_total() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    result = await make_adapter().search(
        LogQuery(query="timeout", filters={}), window=WINDOW, page=PAGE
    )

    assert route.called
    assert result.total == 1234  # {value, relation:"gte"} shape
    assert len(result.entries) == 3

    ecs, flat, docker = result.entries
    assert (ecs.at, ecs.level, ecs.service) == ("2026-06-10T11:59:59.123Z", "ERROR", "payment-svc")
    assert ecs.message == "upstream timeout after 800ms calling card-gateway"
    # Consumed-verbatim keys are dropped; nested containers are kept.
    assert isinstance(ecs, ElkLogEntry)
    assert "message" not in ecs.fields and "@timestamp" not in ecs.fields
    assert ecs.fields["trace"] == {"id": "abc123"}
    assert ecs.fields["log"] == {"level": "error", "logger": "gateway.client"}
    assert ecs.fields["service"] == {"name": "payment-svc", "version": "2.3.1"}

    assert (flat.at, flat.level, flat.service) == ("2026-06-10T11:59:58Z", "WARN", "checkout-api")
    assert flat.message == "connection pool exhausted; queueing request"
    assert isinstance(flat, ElkLogEntry)
    assert flat.fields == {"pool_size": 20}  # timestamp/msg/severity/service.name consumed

    assert (docker.level, docker.service) == ("WARN", "cart-svc")
    assert docker.message == "readiness probe failed: connection refused"
    assert isinstance(docker, ElkLogEntry)
    assert docker.fields == {
        "kubernetes": {"labels": {"app": "cart-svc"}, "pod_name": "cart-svc-7f9d"}
    }


@respx.mock
async def test_search_sends_bool_query_with_terms_range_paging_and_sort() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    await make_adapter().search(
        LogQuery(
            query='service:payment AND "timeout"',
            filters={"level": "ERROR", "thread_id": "thread-1"},
        ),
        window=WINDOW,
        page=Page(offset=20, limit=100),
    )

    body = sent_body(route)
    assert body["query"]["bool"]["must"] == [
        {"query_string": {"query": 'service:payment AND "timeout"'}}
    ]
    assert body["query"]["bool"]["filter"] == [
        {"term": {"level": "ERROR"}},
        {"term": {"thread_id": "thread-1"}},
        {"range": {"@timestamp": {"gte": WINDOW.start, "lte": WINDOW.end}}},
    ]
    assert body["from"] == 20
    assert body["size"] == 100
    assert body["sort"] == [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}]


@respx.mock
async def test_search_uses_configured_index_and_caps_size() -> None:
    route = search_route(index="app-logs-prod").mock(
        return_value=httpx.Response(200, json=ES_RESPONSE)
    )
    await make_adapter(index="app-logs-prod").search(
        LogQuery(query="boom", filters={}), window=WINDOW, page=Page(offset=0, limit=9999)
    )
    assert route.called
    assert sent_body(route)["size"] == MAX_PAGE_SIZE


@respx.mock
async def test_search_legacy_int_total_shape() -> None:
    payload = {"hits": {"total": 3, "hits": ES_RESPONSE["hits"]["hits"][:1]}}
    search_route().mock(return_value=httpx.Response(200, json=payload))
    result = await make_adapter().search(LogQuery(query="", filters={}), window=WINDOW, page=PAGE)
    assert result.total == 3
    assert len(result.entries) == 1


@respx.mock
async def test_search_blank_text_and_unbounded_window_omit_clauses() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    await make_adapter().search(LogQuery(query="  ", filters={}), window=TimeWindow(), page=PAGE)
    assert sent_body(route)["query"]["bool"] == {}


# ── auth headers ──────────────────────────────────────────────────────────────


@respx.mock
async def test_api_key_secret_sends_apikey_authorization_header() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    await make_adapter(secret=API_KEY).search(
        LogQuery(query="x", filters={}), window=WINDOW, page=PAGE
    )
    assert route.calls.last.request.headers["Authorization"] == f"ApiKey {API_KEY}"


@respx.mock
async def test_colon_secret_sends_basic_authorization_header() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    await make_adapter(secret="elastic:changeme").search(
        LogQuery(query="x", filters={}), window=WINDOW, page=PAGE
    )
    # base64("elastic:changeme")
    expected = "Basic ZWxhc3RpYzpjaGFuZ2VtZQ=="
    assert route.calls.last.request.headers["Authorization"] == expected


@respx.mock
async def test_no_secret_sends_no_authorization_header() -> None:
    route = search_route().mock(return_value=httpx.Response(200, json=ES_RESPONSE))
    await make_adapter(secret=None).search(
        LogQuery(query="x", filters={}), window=WINDOW, page=PAGE
    )
    assert "Authorization" not in route.calls.last.request.headers


# ── error mapping ─────────────────────────────────────────────────────────────


@respx.mock
async def test_400_maps_to_value_error_with_extracted_reason() -> None:
    search_route().mock(return_value=httpx.Response(400, json=ES_400))
    with pytest.raises(ValueError, match=r"Failed to parse query \[service:\(\]"):
        await make_adapter().search(
            LogQuery(query="service:(", filters={}), window=WINDOW, page=PAGE
        )


@respx.mock
async def test_401_maps_to_runtime_error_mentioning_credentials() -> None:
    body = {"error": {"root_cause": [], "reason": "missing authentication credentials"}}
    search_route().mock(return_value=httpx.Response(401, json=body))
    with pytest.raises(RuntimeError, match="secret_ref credentials"):
        await make_adapter().search(LogQuery(query="x", filters={}), window=WINDOW, page=PAGE)


@respx.mock
async def test_5xx_maps_to_runtime_error_with_status() -> None:
    search_route().mock(return_value=httpx.Response(503, text="upstream sad"))
    with pytest.raises(RuntimeError, match="status 503"):
        await make_adapter().search(LogQuery(query="x", filters={}), window=WINDOW, page=PAGE)


@respx.mock
async def test_transport_error_maps_to_runtime_error() -> None:
    search_route().mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(RuntimeError, match="base_url and network reachability"):
        await make_adapter().search(LogQuery(query="x", filters={}), window=WINDOW, page=PAGE)


@respx.mock
async def test_non_json_success_body_is_runtime_error() -> None:
    search_route().mock(return_value=httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(RuntimeError, match="non-JSON"):
        await make_adapter().search(LogQuery(query="x", filters={}), window=WINDOW, page=PAGE)


# ── construction / registration ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "option,expected",
    [("false", False), (False, False), ("true", True), (True, True), (None, True)],
)
def test_verify_tls_option_is_parsed_not_bool_coerced(option: object, expected: bool) -> None:
    # A string "false" must disable TLS verification, not become bool("false") == True.
    kwargs = {} if option is None else {"verify_tls": option}
    adapter = make_adapter(**kwargs)
    assert adapter._verify_tls is expected


def test_missing_base_url_is_actionable_value_error() -> None:
    conn = ConnectionConfig(
        id="conn-bad", kind=PortKind.LOG_SEARCH, provider="elasticsearch", name="bad", options={}
    )
    with pytest.raises(ValueError, match="options.base_url"):
        ElasticsearchLogSearchAdapter(conn, None)


async def test_registry_builds_elasticsearch_provider() -> None:
    adapter = await AdapterRegistry.build(make_conn(), None)
    assert isinstance(adapter, ElasticsearchLogSearchAdapter)
    assert "elasticsearch" in AdapterRegistry.providers_for(PortKind.LOG_SEARCH)


# ── mapping micro-branches (pure) ─────────────────────────────────────────────


def test_map_hit_defaults_when_source_is_sparse() -> None:
    entry = map_hit({"_id": "x", "_source": {}})
    assert (entry.at, entry.level, entry.service, entry.message) == ("", "INFO", "", "")
    assert entry.fields == {}


def test_map_hit_skips_non_string_level_and_ecs_log_object_for_message() -> None:
    entry = map_hit({"_source": {"log": {"level": "debug"}, "severity": 3, "msg": "hi"}})
    assert entry.message == "hi"  # ECS "log" object is not a message
    assert entry.level == "DEBUG"  # nested log.level wins; numeric severity skipped
    assert "log" in entry.fields  # nested container kept


def test_map_hit_flat_dotted_log_level_key_is_consumed() -> None:
    entry = map_hit({"_source": {"log.level": "info", "message": "m"}})
    assert entry.level == "INFO"
    assert "log.level" not in entry.fields


def test_build_search_body_clamps_negative_offset() -> None:
    page = Page(offset=-5, limit=10)
    assert build_search_body(LogQuery(query="", filters={}), TimeWindow(), page)["from"] == 0
