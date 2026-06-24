"""Elasticsearch 8 / OpenSearch log search (provider "elasticsearch", LOG_SEARCH).

Connection options:
    {"base_url": "https://elk.internal:9200",  # required
     "index": "logs-*",                         # optional, default "logs-*"
     "verify_tls": true}                        # optional, default true

Secret (resolved from the connection's secret_ref by AdapterRegistry.build):
    - contains ":"  -> HTTP basic auth as "user:pass" (Authorization: Basic ...)
    - otherwise     -> API key (Authorization: ApiKey <key>). Elasticsearch
      hands out API keys base64-encoded (the "encoded" field of the create-API-
      key response), which never contains ":", so the colon test is unambiguous.
    - no secret     -> unauthenticated (security-disabled dev clusters).

Search shape: POST /{index}/_search with a bool query — query_string on
LogQuery.query (when non-blank), one term filter per LogQuery.filters entry
(ANDed; assumes keyword-mapped fields, the ECS default), and an @timestamp
range from the TimeWindow; from/size paging (size capped at 500); sorted
@timestamp desc with unmapped_type=date so indices without the field don't
fail the whole search.

Resilient _source field mapping — real clusters disagree on shapes, so each
mapped attribute walks a documented fallback chain (dotted names are tried as
literal flat keys first, then as nested object paths):
    timestamp: "@timestamp" -> "timestamp"
    message:   "message" -> "msg" -> "log" (only when "log" is a plain string,
               the Docker/fluentd shape; the ECS "log" object is skipped)
    level:     "level" -> "log.level" -> "severity"   (uppercased; INFO default)
    service:   "service" (plain string, or the ECS object's "name" via the
               nested path) -> "service.name" -> "kubernetes.labels.app"
Remaining _source keys land in the entry's `fields` extras: keys consumed
verbatim at the top level are dropped; nested containers (e.g. the whole "log"
object when log.level supplied the level) are kept so no data is lost.
"""

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import Field

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    LogEntry,
    LogQuery,
    LogSearchResult,
    Page,
    SecretValue,
    TimeWindow,
)

DEFAULT_INDEX = "logs-*"
MAX_PAGE_SIZE = 500
REQUEST_TIMEOUT_S = 15.0

_TIMESTAMP_KEYS = ("@timestamp", "timestamp")
_MESSAGE_KEYS = ("message", "msg", "log")
_LEVEL_KEYS = ("level", "log.level", "severity")


class ElkLogEntry(LogEntry):
    """LogEntry plus the unconsumed _source extras.

    The shared domain LogEntry carries no extras field (frozen foundation);
    this subclass passes through LogSearchResult.entries untouched (pydantic
    keeps subclass instances), and the /logs router reads `fields` via getattr.
    """

    fields: dict[str, Any] = Field(default_factory=dict)


# ── request building ──────────────────────────────────────────────────────────


def build_search_body(query: LogQuery, window: TimeWindow, page: Page) -> dict[str, Any]:
    """ES 8 _search body: bool(query_string + term filters + @timestamp range)."""
    filters: list[dict[str, Any]] = [
        {"term": {field: value}} for field, value in sorted(query.filters.items())
    ]
    bounds: dict[str, str] = {}
    if window.start:
        bounds["gte"] = window.start
    if window.end:
        bounds["lte"] = window.end
    if bounds:
        filters.append({"range": {"@timestamp": bounds}})
    bool_query: dict[str, Any] = {}
    if query.query.strip():
        bool_query["must"] = [{"query_string": {"query": query.query}}]
    if filters:
        bool_query["filter"] = filters
    return {
        "query": {"bool": bool_query},
        "from": max(page.offset, 0),
        "size": min(max(page.limit, 0), MAX_PAGE_SIZE),
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
    }


# ── response mapping ──────────────────────────────────────────────────────────


def _resolve_key(source: Mapping[str, Any], key: str) -> tuple[Any, bool]:
    """Value for `key`: literal (possibly dotted) flat key first, then dotted
    nested traversal. Returns (value, consumed_flat) — consumed_flat is True
    only when a literal top-level key supplied the value, which is when the key
    is dropped from the entry's extras (nested containers are kept)."""
    if key in source:
        return source[key], True
    if "." in key:
        node: Any = source
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None, False
            node = node[part]
        return node, False
    return None, False


def _first_string(source: Mapping[str, Any], consumed: set[str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value, flat = _resolve_key(source, key)
        if isinstance(value, str) and value:
            if flat:
                consumed.add(key)
            return value
    return ""


def _extract_service(source: Mapping[str, Any], consumed: set[str]) -> str:
    value, flat = _resolve_key(source, "service")
    if isinstance(value, str) and value:
        if flat:
            consumed.add("service")
        return value
    # The ECS object shape {"service": {"name": ...}} is reached through the
    # "service.name" nested traversal below (container stays in extras).
    return _first_string(source, consumed, ("service.name", "kubernetes.labels.app"))


def map_hit(hit: Mapping[str, Any]) -> ElkLogEntry:
    """One ES hit -> ElkLogEntry via the documented fallback chains."""
    raw_source = hit.get("_source")
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    consumed: set[str] = set()
    at = _first_string(source, consumed, _TIMESTAMP_KEYS)
    message = _first_string(source, consumed, _MESSAGE_KEYS)
    level = _first_string(source, consumed, _LEVEL_KEYS)
    service = _extract_service(source, consumed)
    fields = {key: value for key, value in source.items() if key not in consumed}
    return ElkLogEntry(
        at=at,
        level=level.upper() if level else "INFO",
        service=service,
        message=message,
        fields=fields,
    )


def _parse_total(hits: Mapping[str, Any]) -> int:
    """ES 8 total is {"value": n, "relation": "eq"|"gte"}; legacy/int also seen
    (rest_total_hits_as_int, older OpenSearch)."""
    total = hits.get("total", 0)
    if isinstance(total, Mapping):
        try:
            return int(total.get("value", 0))
        except (TypeError, ValueError):
            return 0
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def _error_reason(response: httpx.Response) -> str:
    """Pull the most specific reason out of an ES error envelope."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            root_causes = error.get("root_cause")
            if isinstance(root_causes, list):
                for cause in root_causes:
                    if isinstance(cause, Mapping) and isinstance(cause.get("reason"), str):
                        return cause["reason"]
            reason = error.get("reason")
            if isinstance(reason, str):
                return reason
        if isinstance(error, str):
            return error
    text = response.text.strip()
    return text[:300] if text else f"HTTP {response.status_code} with empty body"


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.LOG_SEARCH, "elasticsearch")
class ElasticsearchLogSearchAdapter:
    """LogSearchPort against Elasticsearch 8 (OpenSearch-compatible _search).

    The httpx.AsyncClient is built lazily on first search — constructing the
    adapter never touches the network, so registry builds stay cheap. `client`
    exists for tests: inject a pre-configured client instead. Note: instances
    are cached process-wide by the ConnectionResolver; the lazy client is bound
    to the event loop that first searches (fine for routers; worker-thread
    graph loops would need per-loop clients, same constraint as
    DbConnectionStore).
    """

    def __init__(
        self,
        conn: ConnectionConfig | None = None,
        secret: SecretValue | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        options: dict[str, Any] = dict(conn.options) if conn is not None else {}
        base_url = str(options.get("base_url") or "").rstrip("/")
        if client is None and not base_url:
            conn_id = conn.id if conn is not None else "<none>"
            raise ValueError(
                f"elasticsearch connection {conn_id!r} is missing options.base_url "
                '(e.g. "https://elk.internal:9200")'
            )
        self._base_url = base_url
        self._index = str(options.get("index") or DEFAULT_INDEX)
        self._verify_tls = bool(options.get("verify_tls", True))
        self._secret = secret
        self._client = client
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # threading.Lock (not asyncio.Lock): mirrors the S3 adapter — instances
        # are cached process-wide and the guard must not be loop-bound.
        self._client_lock = threading.Lock()

    # ── port surface ──────────────────────────────────────────────────────────

    async def search(self, query: LogQuery, *, window: TimeWindow, page: Page) -> LogSearchResult:
        body = build_search_body(query, window, page)
        client = self._get_client()
        try:
            response = await client.post(f"/{self._index}/_search", json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"elasticsearch search request failed ({exc.__class__.__name__}): {exc}; "
                "check the connection's base_url and network reachability"
            ) from exc
        if response.status_code == 400:
            # Bad query_string / malformed body — caller-correctable.
            raise ValueError(f"elasticsearch rejected the query: {_error_reason(response)}")
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"elasticsearch auth failed (status {response.status_code}): "
                f"{_error_reason(response)}; check the connection's secret_ref credentials"
            )
        if response.status_code >= 300:
            raise RuntimeError(
                f"elasticsearch search failed (status {response.status_code}): "
                f"{_error_reason(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("elasticsearch returned a non-JSON search response") from exc
        hits_obj = payload.get("hits") if isinstance(payload, Mapping) else None
        if not isinstance(hits_obj, Mapping):
            raise RuntimeError("elasticsearch search response is missing the 'hits' object")
        raw_hits = hits_obj.get("hits")
        entries: list[LogEntry] = [
            map_hit(hit)
            for hit in (raw_hits if isinstance(raw_hits, list) else [])
            if isinstance(hit, Mapping)
        ]
        return LogSearchResult(entries=entries, total=_parse_total(hits_obj))

    # ── client bootstrap ──────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            with self._client_lock:
                if self._client is None or self._client.is_closed or self._client_loop is not loop:
                    self._client = self._build_client()
                    self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._client_loop = None

    def _build_client(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        auth: httpx.Auth | None = None
        if self._secret is not None and self._secret.value:
            if ":" in self._secret.value:
                username, _, password = self._secret.value.partition(":")
                auth = httpx.BasicAuth(username, password)
            else:
                headers["Authorization"] = f"ApiKey {self._secret.value}"
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            auth=auth,
            verify=self._verify_tls,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        )
