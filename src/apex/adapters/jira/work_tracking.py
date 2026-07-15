"""Jira Cloud work-tracking adapter (provider "jira", PortKind.WORK_TRACKING).

Wire surface: Jira Cloud REST API v3 (https://<site>.atlassian.net/rest/api/3).
Connection options: {"base_url": "https://<site>.atlassian.net",
"user_email": "<account email>", "project_key"?: "PHX"}; the secret is the
Atlassian API token, sent as HTTP basic auth (email:token).

Wire-format decisions (documented deviations / mappings):
- Descriptions are Atlassian Document Format (ADF). `adf_to_text` walks doc
  nodes into plain text (block nodes emit newlines; mentions/emoji use their
  display text) and tolerates legacy plain-string descriptions. Outbound text
  becomes one ADF paragraph per line via `text_to_adf`.
- Status is normalized from `statusCategory.key` (new -> open, indeterminate ->
  in_progress, done -> closed); unknown categories fall back to the lowercased
  status name. Kind is the lowercased issue-type name.
- Search uses POST /rest/api/3/search/jql, which is TOKEN-paged (nextPageToken,
  no total). Page.offset is mapped best-effort: the adapter fetches
  offset+limit issues from the start (following tokens, <=100 per request) and
  slices the window. WorkItemPage.total is exact when the last page was
  reached, otherwise a lower bound (fetched count + 1).
- enrich_item: enrichment.fields -> PUT /rest/api/3/issue/{key} field update
  ("description" strings are converted to ADF; "status" is rejected — Jira
  requires the transitions API, out of scope for M4; "project" is rejected to
  preserve the configured project boundary); enrichment.comment ->
  POST /rest/api/3/issue/{key}/comment with an ADF body. The item is re-fetched
  afterwards so callers always see the updated row.
- translate_query is a deterministic ruleset (apex.services.work_tracking);
  LLM-backed translation is a planned upgrade behind the same port surface.

The httpx.AsyncClient is lazy and per-instance, but rebuilt if the running
event loop changes: resolver-cached adapter instances are reused across the
short-lived loops that graph nodes spin up, and a pooled connection bound to a
closed loop would otherwise fail.
"""

import asyncio
import base64
import re
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import ValidationError

from apex.adapters.http_resilience import parse_json_response, resilient_request
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.options import require_bounded_credential
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import MAX_DESCRIPTION_CHARS
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    SecretValue,
    TranslatedQuery,
    WorkItem,
    WorkItemDraft,
    WorkItemFilters,
    WorkItemPage,
)
from apex.ports.work_tracking import (
    WorkTrackingMutationRejectedError,
    WorkTrackingMutationTargetNotFoundError,
)
from apex.services.work_tracking import (
    CURRENT_SPRINT,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    WorkQuerySpec,
    confidence_for,
    parse_work_query,
    validate_provider_page,
)

PROVIDER = "jira"
_TIMEOUT_S = 15.0
_MAX_RESULTS_PER_REQUEST = 100
_ISSUE_FIELDS = ["summary", "status", "issuetype", "description", "project"]
_RECONCILIATION_ISSUE_FIELDS = [*_ISSUE_FIELDS, "labels"]
_MUTATION_MARKER = re.compile(r"\Aapex-idem-[0-9a-f]{32}\Z")
_ISSUE_KEY = re.compile(r"\A(?P<project>[A-Za-z][A-Za-z0-9_]{0,254})-(?P<number>[0-9]{1,19})\Z")
_COMMENT_PAGE_SIZE = 100
_MAX_COMMENT_SCAN = 10_000
_MAX_ADF_NODES = 50_000
_MAX_ADF_FLATTEN_CHARS = MAX_DESCRIPTION_CHARS * 4

_STATUS_BY_CATEGORY = {"new": "open", "indeterminate": "in_progress", "done": "closed"}
_JQL_STATUS_CLAUSE = {
    STATUS_OPEN: 'statusCategory = "To Do"',
    STATUS_IN_PROGRESS: 'statusCategory = "In Progress"',
    STATUS_CLOSED: 'statusCategory = "Done"',
}
_JQL_ISSUE_TYPE = {"bug": "Bug", "story": "Story", "task": "Task"}

# ADF node types that terminate a line of text when flattened.
_ADF_BLOCK_TYPES = frozenset(
    {"paragraph", "heading", "blockquote", "listItem", "codeBlock", "tableRow", "rule"}
)


# ── ADF helpers ───────────────────────────────────────────────────────────────


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format tree to plain text.

    Tolerates non-ADF inputs: plain strings pass through, None becomes "".
    Provider JSON is walked iteratively so a deeply nested response cannot
    exhaust the Python stack. Only a bounded prefix is retained before the
    domain's description limit is applied.
    """
    parts: list[str] = []
    retained = 0
    for segment in _iter_adf_text(node):
        room = _MAX_ADF_FLATTEN_CHARS - retained
        if room <= 0:
            continue
        prefix = segment[:room]
        if prefix:
            parts.append(prefix)
            retained += len(prefix)
    text = "".join(parts)
    # collapse the trailing newline every block node emits + runs of blanks
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    normalized = "\n".join(lines)
    if len(normalized) > MAX_DESCRIPTION_CHARS:
        return normalized[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return normalized


def _iter_adf_text(node: Any) -> Iterator[str]:
    """Yield ADF text in document order without recursive provider traversal."""

    stack: list[Any] = [node]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > _MAX_ADF_NODES:
            raise ValueError("jira ADF response exceeds the node limit")
        if current is None:
            continue
        if isinstance(current, str):
            yield current
            continue
        if isinstance(current, list):
            stack.extend(reversed(current))
            continue
        if not isinstance(current, dict):
            raise ValueError("jira ADF response contains a non-node value")
        node_type = current.get("type")
        if node_type is not None and not isinstance(node_type, str):
            raise ValueError("jira ADF response contains a non-string node type")
        if node_type == "text":
            raw_text = current.get("text", "")
            if not isinstance(raw_text, str):
                raise ValueError("jira ADF text node contains a non-string value")
            yield raw_text
            continue
        if node_type == "hardBreak":
            yield "\n"
            continue
        if node_type in ("mention", "emoji"):
            attrs = current.get("attrs") or {}
            if isinstance(attrs, dict):
                display = attrs.get("text") or attrs.get("shortName") or ""
                if not isinstance(display, str):
                    raise ValueError("jira ADF display node contains a non-string value")
                yield display
            else:
                raise ValueError("jira ADF response contains malformed attrs")
            continue
        if node_type in _ADF_BLOCK_TYPES:
            stack.append("\n")
        content = current.get("content", [])
        if not isinstance(content, list):
            raise ValueError("jira ADF response contains malformed content")
        stack.append(content)


def _adf_contains_text(node: Any, needle: str) -> bool:
    """Search flattened ADF with bounded memory, including across text nodes."""

    if not needle:
        return True
    tail = ""
    overlap = max(len(needle) - 1, 0)
    for segment in _iter_adf_text(node):
        boundary = tail + segment[:overlap]
        if needle in segment or needle in boundary:
            return True
        tail = (tail + segment[-overlap:])[-overlap:] if overlap else ""
    return False


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text -> minimal ADF doc: one paragraph per line, blank lines kept."""
    paragraphs: list[dict[str, Any]] = []
    for line in text.split("\n"):
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": paragraphs}


# ── JQL helpers ───────────────────────────────────────────────────────────────


def _jql_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _jql_project(value: str) -> str:
    return value if value.replace("_", "").isalnum() else _jql_quote(value)


def render_jql(spec: WorkQuerySpec) -> str:
    """Render a WorkQuerySpec as JQL ending in `ORDER BY updated DESC`."""
    clauses: list[str] = []
    if spec.project:
        clauses.append(f"project = {_jql_project(spec.project)}")
    if spec.statuses:
        status_terms = [_JQL_STATUS_CLAUSE[s] for s in spec.statuses if s in _JQL_STATUS_CLAUSE]
        if len(status_terms) == 1:
            clauses.append(status_terms[0])
        elif status_terms:
            clauses.append("(" + " OR ".join(status_terms) + ")")
    if spec.kinds:
        names = ", ".join(_JQL_ISSUE_TYPE.get(k, k.capitalize()) for k in spec.kinds)
        clauses.append(f"issuetype in ({names})")
    if spec.assigned_to_me:
        clauses.append("assignee = currentUser()")
    if spec.sprint == CURRENT_SPRINT:
        clauses.append("sprint in openSprints()")
    elif spec.sprint:
        clauses.append(f"sprint = {_jql_quote(spec.sprint)}")
    for phrase in spec.phrases:
        clauses.append(f"text ~ {_jql_quote(phrase)}")
    if spec.created_window == "today":
        clauses.append("created >= startOfDay()")
    elif spec.created_window == "this_week":
        clauses.append("created >= startOfWeek()")
    elif spec.created_window == "last_week":
        clauses.append("created >= startOfWeek(-1w) AND created < startOfWeek()")
    elif spec.created_window == "this_month":
        clauses.append("created >= startOfMonth()")
    if spec.fallback_text:
        clauses.append(f"text ~ {_jql_quote(spec.fallback_text)}")
    where = " AND ".join(clauses)
    order = "ORDER BY updated DESC"
    return f"{where} {order}" if where else order


def _list_field(data: dict[str, Any], field: str, *, context: str) -> list[Any]:
    value = data.get(field)
    if not isinstance(value, list):
        raise RuntimeError(f"jira {context} field {field!r} must be a list")
    return value


def _bounded_provider_int(value: object, *, context: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"jira {context} must be an integer")
    if value < minimum or value > maximum:
        raise RuntimeError(f"jira {context} is outside the allowed range")
    return value


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.WORK_TRACKING, PROVIDER)
class JiraWorkTrackingAdapter:
    provider = PROVIDER

    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        raw_base_url = options.get("base_url")
        if not isinstance(raw_base_url, str) or not raw_base_url:
            raise ValueError(
                f"jira connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://<site>.atlassian.net")'
            )
        base_url = raw_base_url.rstrip("/")
        raw_user_email = options.get("user_email")
        if not isinstance(raw_user_email, str) or not raw_user_email:
            raise ValueError(f"jira connection {conn.id!r} is missing options['user_email']")
        user_email = require_bounded_credential(
            raw_user_email,
            label="jira user_email",
            max_bytes=320,
        )
        if ":" in user_email:
            raise ValueError("jira user_email must not contain ':'")
        if secret is None:
            raise ValueError(
                f"jira connection {conn.id!r} requires an API token; set secret_ref "
                'on the connection (e.g. "env:APEX_INTEGRATION_JIRA_API_TOKEN")'
            )
        api_token = require_bounded_credential(
            secret.value,
            label="jira API token",
        )
        self._base_url = base_url
        raw_project_key = options.get("project_key")
        if raw_project_key in (None, ""):
            self._project_key = None
        elif not isinstance(raw_project_key, str):
            raise ValueError("jira project_key must be a string")
        else:
            self._project_key = require_bounded_credential(
                raw_project_key,
                label="jira project_key",
                max_bytes=255,
            )
        self._allow_private_hosts = private_hosts_allowed(options)
        token = base64.b64encode(f"{user_email}:{api_token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    @property
    def project_id(self) -> str | None:
        """Configured project boundary used by scoped direct-item routes."""

        return self._project_key

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Lazy client, rebuilt when the running event loop changes (resolver
        caches adapter instances across graph-node loops)."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = safe_async_http_client(
                base_url=self._base_url,
                headers=self._headers,
                timeout=_TIMEOUT_S,
                allow_private_hosts=self._allow_private_hosts,
            )
            self._http_loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._http_loop = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        try:
            response = await resilient_request(
                self._client(), method, path, json=json, params=params
            )
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            raise RuntimeError(
                bounded_diagnostic(
                    f"jira request {method} {path} failed before a response arrived: {detail}"
                )
            ) from exc
        if response.status_code == 404 and not_found is not None:
            raise WorkTrackingMutationTargetNotFoundError(not_found)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"jira rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's user_email "
                "and API token secret"
            )
        if response.status_code == 400:
            raise WorkTrackingMutationRejectedError(
                f"jira rejected the request: {_error_text(response)}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"jira {method} {path} failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        return response

    # ── port surface ──────────────────────────────────────────────────────────

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        """Deterministic keyword/pattern NL->JQL translation.

        LLM-backed translation is a planned upgrade — same port surface, only
        this method's internals change.
        """
        spec = parse_work_query(
            natural_language, default_project=self._project_key, context=context
        )
        return TranslatedQuery(
            provider=PROVIDER, query=render_jql(spec), confidence=confidence_for(spec)
        )

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage:
        """Token-paged search mapped onto Page.offset best-effort: fetch
        offset+limit issues from the start, slice the window (see module doc)."""
        if query.provider.casefold() != PROVIDER:
            raise ValueError("translated query provider does not match jira")
        validate_provider_page(page)
        target = page.offset + page.limit
        fetched: list[WorkItem] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        seen_keys: set[str] = set()
        is_last = True
        while len(fetched) < target:
            payload: dict[str, Any] = {
                "jql": query.query,
                "maxResults": min(_MAX_RESULTS_PER_REQUEST, target - len(fetched)),
                "fields": _RECONCILIATION_ISSUE_FIELDS,
            }
            if token:
                payload["nextPageToken"] = token
            response = await self._request("POST", "/rest/api/3/search/jql", json=payload)
            data = _json_object(response, "search")
            # Honor our own requested window even if a misbehaving upstream
            # ignores maxResults and returns a larger array.
            raw_issues = _list_field(data, "issues", context="search response")
            requested_count = min(_MAX_RESULTS_PER_REQUEST, target - len(fetched))
            if len(raw_issues) > requested_count:
                raise RuntimeError("jira search response exceeded the requested page-size budget")
            if any(not isinstance(issue, dict) for issue in raw_issues):
                raise RuntimeError("jira search response contains a non-object issue")
            issues = raw_issues
            for issue in issues:
                item = self._work_item_from_issue(issue)
                if item.key in seen_keys:
                    raise RuntimeError("jira search response contains a duplicate issue key")
                seen_keys.add(item.key)
                fetched.append(item)
            raw_token = data.get("nextPageToken")
            if raw_token is not None and (
                not isinstance(raw_token, str)
                or not raw_token
                or len(raw_token) > 2_048
                or "\x00" in raw_token
            ):
                raise RuntimeError("jira search response has an invalid nextPageToken")
            token = raw_token
            if token is not None:
                if token in seen_tokens:
                    raise RuntimeError("jira search response repeated a nextPageToken")
                seen_tokens.add(token)
            raw_is_last = data.get("isLast", token is None)
            if not isinstance(raw_is_last, bool):
                raise RuntimeError("jira search response field 'isLast' must be a boolean")
            if (raw_is_last and token is not None) or (not raw_is_last and token is None):
                raise RuntimeError("jira search response has inconsistent pagination fields")
            is_last = raw_is_last
            if is_last or not issues:
                break
        total = len(fetched) if is_last else len(fetched) + 1  # lower bound when paging on
        return WorkItemPage(items=fetched[page.offset : target], total=total, page=page)

    async def get_item(self, key: str) -> WorkItem:
        self._require_item_in_project(key)
        response = await self._request(
            "GET",
            f"/rest/api/3/issue/{key}",
            params={"fields": ",".join(_ISSUE_FIELDS)},
            not_found=f"work item {key!r} not found in jira",
        )
        return self._work_item_from_issue(_json_object(response, "work item"))

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
        spec = WorkQuerySpec(
            project=self._project_key,
            statuses=(filters.status,) if filters.status else (),
            kinds=(filters.kind,) if filters.kind else (),
            phrases=(filters.text,) if filters.text else (),
        )
        query = TranslatedQuery(provider=PROVIDER, query=render_jql(spec))
        return await self.execute_query(query, page=page)

    async def create_item(self, draft: WorkItemDraft) -> WorkItem:
        return await self._create_item(draft, marker=None)

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        """Reconcile an ambiguous create via the unique APEX issue label."""

        self._validate_mutation_marker(marker)
        if not self._project_key:
            raise ValueError("jira create reconciliation needs options['project_key']")
        response = await self._request(
            "POST",
            "/rest/api/3/search/jql",
            json={
                "jql": (
                    f"project = {_jql_project(self._project_key)} AND labels = {_jql_quote(marker)}"
                ),
                "maxResults": 2,
                "fields": _ISSUE_FIELDS,
            },
        )
        data = _json_object(response, "idempotency marker search")
        raw_issues = _list_field(data, "issues", context="idempotency search response")
        if len(raw_issues) > 2:
            raise RuntimeError("jira idempotency search response has an invalid issues list")
        if any(not isinstance(issue, dict) for issue in raw_issues):
            raise RuntimeError("jira idempotency search response contains a non-object issue")
        issues = raw_issues
        if len(issues) > 1:
            raise RuntimeError("jira idempotency marker is attached to multiple issues")
        return self._work_item_from_issue(issues[0]) if issues else None

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        self.validate_create_item_idempotent(draft, marker=marker)
        return await self._create_item(draft, marker=marker)

    def validate_create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> None:
        """Validate all local create constraints before fencing the provider POST."""

        self._validate_mutation_marker(marker)
        if any(name.casefold() == "project" for name in draft.fields):
            raise WorkTrackingMutationRejectedError(
                "jira project is fixed by the connection; remove project from draft.fields"
            )
        if not self._project_key:
            raise WorkTrackingMutationRejectedError(
                "jira create_item needs options['project_key'] on the connection"
            )
        labels = next(
            (value for name, value in draft.fields.items() if name.casefold() == "labels"),
            None,
        )
        if labels is not None and not (
            isinstance(labels, list) and all(isinstance(label, str) for label in labels)
        ):
            raise WorkTrackingMutationRejectedError(
                "jira labels must be a list of strings for idempotent create"
            )

    async def _create_item(self, draft: WorkItemDraft, *, marker: str | None) -> WorkItem:
        if any(name.casefold() == "project" for name in draft.fields):
            raise ValueError(
                "jira project is fixed by the connection; remove project from draft.fields"
            )
        if not self._project_key:
            raise ValueError("jira create_item needs options['project_key'] on the connection")
        fields: dict[str, Any] = {
            "summary": draft.title,
            "issuetype": {"name": _JQL_ISSUE_TYPE.get(draft.kind, draft.kind.capitalize())},
            "description": text_to_adf(draft.description),
        }
        fields.update(draft.fields)
        if marker is not None:
            labels_field = next(
                (name for name in fields if name.casefold() == "labels"),
                "labels",
            )
            labels = fields.get(labels_field)
            if labels is None:
                fields[labels_field] = [marker]
            elif isinstance(labels, list) and all(isinstance(label, str) for label in labels):
                fields[labels_field] = [*labels, *([] if marker in labels else [marker])]
            else:
                raise ValueError("jira labels must be a list of strings for idempotent create")
        fields["project"] = {"key": self._project_key}
        response = await self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        created = _json_object(response, "created issue")
        key = str(created.get("key", ""))
        self._require_item_in_project(key)
        return WorkItem(
            key=key,
            title=draft.title,
            kind=draft.kind,
            status="open",
            description=draft.description,
            url=f"{self._base_url}/browse/{key}",
        )

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        """fields -> issue edit (PUT); comment -> ADF comment (POST); re-fetch."""
        self._require_item_in_project(key)
        not_found = f"work item {key!r} not found in jira"
        if enrichment.fields:
            if any(name.casefold() == "project" for name in enrichment.fields):
                raise ValueError("jira project cannot be changed through enrichment.fields")
            if "status" in enrichment.fields:
                raise ValueError(
                    "jira status changes require the transitions API, which this "
                    "adapter does not support yet; remove 'status' from enrichment.fields"
                )
        # Jira may preserve an old key as an alias after moving an issue. Resolve
        # and validate its current project before issuing any mutation.
        await self.get_item(key)
        if enrichment.fields:
            fields = {
                name: text_to_adf(value)
                if name == "description" and isinstance(value, str)
                else value
                for name, value in enrichment.fields.items()
            }
            await self._request(
                "PUT", f"/rest/api/3/issue/{key}", json={"fields": fields}, not_found=not_found
            )
        if enrichment.comment:
            await self._request(
                "POST",
                f"/rest/api/3/issue/{key}/comment",
                json={"body": text_to_adf(enrichment.comment)},
                not_found=not_found,
            )
        return await self.get_item(key)

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None:
        """Set exact field values; replaying the same PUT is idempotent."""

        self._validate_enrichment_fields(fields)
        await self.get_item(key)
        if not fields:
            return
        converted = {
            name: text_to_adf(value) if name == "description" and isinstance(value, str) else value
            for name, value in fields.items()
        }
        await self._request(
            "PUT",
            f"/rest/api/3/issue/{key}",
            json={"fields": converted},
            not_found=f"work item {key!r} not found in jira",
        )

    def validate_update_item_fields_idempotent(self, fields: dict[str, object]) -> None:
        self._validate_enrichment_fields(fields)

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        """Scan Jira comments for the durable marker before another POST."""

        self._validate_mutation_marker(marker)
        await self.get_item(key)
        token = _comment_marker(marker)
        start_at = 0
        while start_at < _MAX_COMMENT_SCAN:
            response = await self._request(
                "GET",
                f"/rest/api/3/issue/{key}/comment",
                params={"startAt": start_at, "maxResults": _COMMENT_PAGE_SIZE},
                not_found=f"work item {key!r} not found in jira",
            )
            data = _json_object(response, "comments page")
            raw_comments = _list_field(data, "comments", context="comments response")
            if len(raw_comments) > _COMMENT_PAGE_SIZE:
                raise RuntimeError("jira comments response exceeded the requested page-size budget")
            if any(not isinstance(comment, dict) for comment in raw_comments):
                raise RuntimeError("jira comments response contains a non-object comment")
            comments = raw_comments
            if any(_adf_contains_text(comment.get("body"), token) for comment in comments):
                return True
            start_at += len(comments)
            total = _bounded_provider_int(
                data.get("total"),
                context="comments response total",
                minimum=start_at,
                maximum=_MAX_COMMENT_SCAN,
            )
            if total < start_at or total > _MAX_COMMENT_SCAN:
                raise RuntimeError("jira comments response exceeds the safe scan limit")
            if not comments or start_at >= total:
                return False
        raise RuntimeError("jira comment reconciliation exceeded its safe scan limit")

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None:
        self._validate_mutation_marker(marker)
        await self.get_item(key)
        await self._request(
            "POST",
            f"/rest/api/3/issue/{key}/comment",
            json={"body": text_to_adf(f"{comment}\n\n{_comment_marker(marker)}")},
            not_found=f"work item {key!r} not found in jira",
        )

    @staticmethod
    def _validate_mutation_marker(marker: str) -> None:
        if _MUTATION_MARKER.fullmatch(marker) is None:
            raise ValueError("invalid APEX work-item mutation marker")

    @staticmethod
    def _validate_enrichment_fields(fields: dict[str, object]) -> None:
        if any(name.casefold() == "project" for name in fields):
            raise ValueError("jira project cannot be changed through enrichment.fields")
        if "status" in fields:
            raise ValueError(
                "jira status changes require the transitions API, which this "
                "adapter does not support yet; remove 'status' from enrichment.fields"
            )

    def _require_item_in_project(self, key: str) -> None:
        match = _ISSUE_KEY.fullmatch(key)
        if match is None or int(match.group("number")) < 1:
            raise WorkTrackingMutationTargetNotFoundError(
                "work item key is not a valid jira issue key"
            )
        project = match.group("project")
        if self._project_key is not None and project.casefold() != self._project_key.casefold():
            raise WorkTrackingMutationTargetNotFoundError(
                f"work item {key!r} not found in configured jira project"
            )

    # ── mapping ───────────────────────────────────────────────────────────────

    def _work_item_from_issue(self, issue: dict[str, Any]) -> WorkItem:
        raw_fields = issue.get("fields")
        if not isinstance(raw_fields, dict):
            raise RuntimeError("jira issue response has malformed fields")
        fields = raw_fields
        raw_status = fields.get("status")
        if raw_status is None:
            status_field: dict[str, Any] = {}
        elif isinstance(raw_status, dict):
            status_field = raw_status
        else:
            raise RuntimeError("jira issue response has malformed status")
        raw_category = status_field.get("statusCategory")
        if raw_category is None:
            category_field: dict[str, Any] = {}
        elif isinstance(raw_category, dict):
            category_field = raw_category
        else:
            raise RuntimeError("jira issue response has malformed status category")
        raw_category_key = category_field.get("key", "")
        raw_status_name = status_field.get("name", "open")
        if not isinstance(raw_category_key, str) or not isinstance(raw_status_name, str):
            raise RuntimeError("jira issue response contains malformed status text")
        category = raw_category_key.lower()
        status = _STATUS_BY_CATEGORY.get(category) or raw_status_name.lower()
        raw_issue_type = fields.get("issuetype")
        if raw_issue_type is None:
            issue_type = "story"
        elif isinstance(raw_issue_type, dict):
            issue_type = raw_issue_type.get("name") or "story"
        else:
            raise RuntimeError("jira issue response has malformed issue type")
        if not isinstance(issue_type, str):
            raise RuntimeError("jira issue response contains malformed issue type name")
        key = issue.get("key", "")
        if not isinstance(key, str):
            raise RuntimeError("jira issue response contains a non-string key")
        self._require_item_in_project(key)
        raw_project = fields.get("project")
        if not isinstance(raw_project, dict):
            raise RuntimeError("jira issue response has malformed project")
        project_key = raw_project.get("key") or ""
        if not isinstance(project_key, str):
            raise RuntimeError("jira issue response contains a non-string project key")
        if self._project_key is not None and project_key.casefold() != self._project_key.casefold():
            raise WorkTrackingMutationTargetNotFoundError(
                f"work item {key!r} not found in configured jira project"
            )
        try:
            summary = fields.get("summary") or ""
            description = fields.get("description")
            if not isinstance(summary, str) or not isinstance(description, (dict, str, type(None))):
                raise ValueError("jira issue response contains malformed content fields")
            return WorkItem(
                key=key,
                title=summary,
                kind=issue_type.lower(),
                status=status,
                description=adf_to_text(description),
                url=f"{self._base_url}/browse/{key}",
            )
        except (ValidationError, ValueError) as exc:
            raise RuntimeError("jira returned an invalid work item") from exc


def _error_text(response: httpx.Response) -> str:
    try:
        data = parse_json_response(response, context="jira error response")
    except RuntimeError:
        return bounded_diagnostic(response.text)
    if not isinstance(data, dict):
        return bounded_diagnostic(response.text)
    raw_messages = data.get("errorMessages") or []
    raw_errors = data.get("errors") or {}
    if not isinstance(raw_messages, list) or not isinstance(raw_errors, dict):
        return bounded_diagnostic(response.text)
    messages = list(raw_messages[:128])
    messages.extend(f"{field}: {msg}" for field, msg in list(raw_errors.items())[:128])
    return (
        bounded_diagnostic("; ".join(str(message) for message in messages))
        if messages
        else bounded_diagnostic(response.text)
    )


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    data = parse_json_response(response, context=f"jira {context} response")
    if not isinstance(data, dict):
        raise RuntimeError(f"jira {context} response must be a JSON object")
    return data


def _comment_marker(marker: str) -> str:
    return f"[APEX-IDEMPOTENCY:{marker}]"
