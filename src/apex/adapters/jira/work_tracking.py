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
  requires the transitions API, out of scope for M4); enrichment.comment ->
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
from typing import Any

import httpx

from apex.adapters.http_resilience import resilient_request
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
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
from apex.services.work_tracking import (
    CURRENT_SPRINT,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    WorkQuerySpec,
    confidence_for,
    parse_work_query,
)

PROVIDER = "jira"
_TIMEOUT_S = 15.0
_MAX_RESULTS_PER_REQUEST = 100
_ISSUE_FIELDS = ["summary", "status", "issuetype", "description"]

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
    """
    text = _walk_adf(node)
    # collapse the trailing newline every block node emits + runs of blanks
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def _walk_adf(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_walk_adf(child) for child in node)
    if not isinstance(node, dict):
        return str(node)
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("mention", "emoji"):
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    inner = _walk_adf(node.get("content", []))
    if node_type in _ADF_BLOCK_TYPES:
        return inner + "\n"
    return inner


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


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.WORK_TRACKING, PROVIDER)
class JiraWorkTrackingAdapter:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        base_url = str(options.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(
                f"jira connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://<site>.atlassian.net")'
            )
        user_email = str(options.get("user_email") or "")
        if not user_email:
            raise ValueError(f"jira connection {conn.id!r} is missing options['user_email']")
        if secret is None:
            raise ValueError(
                f"jira connection {conn.id!r} requires an API token; set secret_ref "
                'on the connection (e.g. "env:APEX_JIRA_API_TOKEN")'
            )
        self._base_url = base_url
        self._project_key = str(options.get("project_key") or "") or None
        token = base64.b64encode(f"{user_email}:{secret.value}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Lazy client, rebuilt when the running event loop changes (resolver
        caches adapter instances across graph-node loops)."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = httpx.AsyncClient(
                base_url=self._base_url, headers=self._headers, timeout=_TIMEOUT_S
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
            raise RuntimeError(
                f"jira request {method} {path} failed before a response arrived: {exc}"
            ) from exc
        if response.status_code == 404 and not_found is not None:
            raise KeyError(not_found)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"jira rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's user_email "
                "and API token secret"
            )
        if response.status_code == 400:
            raise ValueError(f"jira rejected the request: {_error_text(response)}")
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
        target = page.offset + page.limit
        fetched: list[WorkItem] = []
        token: str | None = None
        is_last = True
        while len(fetched) < target:
            payload: dict[str, Any] = {
                "jql": query.query,
                "maxResults": min(_MAX_RESULTS_PER_REQUEST, target - len(fetched)),
                "fields": _ISSUE_FIELDS,
            }
            if token:
                payload["nextPageToken"] = token
            response = await self._request("POST", "/rest/api/3/search/jql", json=payload)
            data = response.json()
            issues = data.get("issues") or []
            fetched.extend(self._work_item_from_issue(issue) for issue in issues)
            token = data.get("nextPageToken")
            is_last = bool(data.get("isLast", token is None)) or token is None
            if is_last or not issues:
                break
        total = len(fetched) if is_last else len(fetched) + 1  # lower bound when paging on
        return WorkItemPage(items=fetched[page.offset : target], total=total, page=page)

    async def get_item(self, key: str) -> WorkItem:
        response = await self._request(
            "GET",
            f"/rest/api/3/issue/{key}",
            params={"fields": ",".join(_ISSUE_FIELDS)},
            not_found=f"work item {key!r} not found in jira",
        )
        return self._work_item_from_issue(response.json())

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
        fields: dict[str, Any] = {
            "summary": draft.title,
            "issuetype": {"name": _JQL_ISSUE_TYPE.get(draft.kind, draft.kind.capitalize())},
            "description": text_to_adf(draft.description),
        }
        if self._project_key:
            fields["project"] = {"key": self._project_key}
        fields.update(draft.fields)
        if "project" not in fields:
            raise ValueError(
                "jira create_item needs a project: set options['project_key'] on the "
                'connection or pass draft.fields["project"]'
            )
        response = await self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        created = response.json()
        key = str(created.get("key", ""))
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
        not_found = f"work item {key!r} not found in jira"
        if enrichment.fields:
            if "status" in enrichment.fields:
                raise ValueError(
                    "jira status changes require the transitions API, which this "
                    "adapter does not support yet; remove 'status' from enrichment.fields"
                )
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

    # ── mapping ───────────────────────────────────────────────────────────────

    def _work_item_from_issue(self, issue: dict[str, Any]) -> WorkItem:
        fields = issue.get("fields") or {}
        status_field = fields.get("status") or {}
        category = ((status_field.get("statusCategory") or {}).get("key") or "").lower()
        status = (
            _STATUS_BY_CATEGORY.get(category) or str(status_field.get("name") or "open").lower()
        )
        issue_type = (fields.get("issuetype") or {}).get("name") or "story"
        key = str(issue.get("key", ""))
        return WorkItem(
            key=key,
            title=str(fields.get("summary") or ""),
            kind=str(issue_type).lower(),
            status=status,
            description=adf_to_text(fields.get("description")),
            url=f"{self._base_url}/browse/{key}",
        )


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    messages = list(data.get("errorMessages") or [])
    messages.extend(f"{field}: {msg}" for field, msg in (data.get("errors") or {}).items())
    return "; ".join(messages) if messages else response.text[:300]
