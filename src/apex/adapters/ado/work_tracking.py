"""Azure DevOps work-tracking adapter (provider "ado", PortKind.WORK_TRACKING).

Wire surface: Azure DevOps Services REST 7.1 (WIQL + work items). Connection
options: {"base_url": "https://dev.azure.com/<org>", "project": "<project>"};
the secret is a Personal Access Token sent as HTTP basic auth with an empty
username (":PAT" base64-encoded).

Wire-format decisions (documented deviations / mappings):
- execute_query: POST {project}/_apis/wit/wiql?api-version=7.1 returns the FULL
  matching id list (no server paging), so Page.offset/limit are applied
  client-side to that id list before one batch
  GET _apis/wit/workitems?ids=...&fields=... hydration call.
  WorkItemPage.total is therefore exact (len of the WIQL id list).
- System.Description is HTML; a small tag-strip + entity-unescape pass turns it
  into plain text. System.State is normalized: New/To Do/Proposed/Approved ->
  open, Active/In Progress/Doing/Committed -> in_progress,
  Resolved/Closed/Done/Completed/Removed -> closed; unknown states pass through
  lowercased. Kind maps "User Story" <-> "story"; other types lowercase.
- create_item: POST {project}/_apis/wit/workitems/${Type}?api-version=7.1 with
  an application/json-patch+json body (System.Title, System.Description, then
  draft.fields: dotted keys verbatim as /fields/<key>, bare keys must be
  title/description — anything else raises ValueError).
- enrich_item: enrichment.fields -> PATCH _apis/wit/workitems/{id} JSON-patch
  ("add" ops — ADO treats add as upsert). Bare keys map title -> System.Title,
  description -> System.Description, status -> System.State (the raw value is
  sent; callers supply a real ADO state name); dotted keys are used verbatim;
  other bare keys raise ValueError. enrichment.comment -> POST
  {project}/_apis/wit/workItems/{id}/comments?api-version=7.1-preview.3 — the
  comments API has no GA release at 7.1, so the preview version is a deliberate
  choice (documented here). The item is re-fetched afterwards.
- translate_query renders the shared deterministic ruleset
  (apex.services.work_tracking) as WIQL:
  SELECT [System.Id] FROM WorkItems WHERE ... ORDER BY [System.ChangedDate]
  DESC. "current sprint" -> [System.IterationPath] = @CurrentIteration
  (best-effort: @CurrentIteration needs a team context ADO infers from the
  project); a named sprint renders UNDER '<project>\\<name>'. LLM-backed
  translation is a planned upgrade — same port surface.

The httpx.AsyncClient is lazy and per-instance, rebuilt if the running event
loop changes (resolver-cached instances cross graph-node loops).
"""

import asyncio
import base64
import html
import re
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

PROVIDER = "ado"
_TIMEOUT_S = 15.0
_API_VERSION = "7.1"
_COMMENTS_API_VERSION = "7.1-preview.3"
_ITEM_FIELDS = [
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.Description",
]

_STATE_TO_STATUS = {
    "new": STATUS_OPEN,
    "to do": STATUS_OPEN,
    "proposed": STATUS_OPEN,
    "approved": STATUS_OPEN,
    "open": STATUS_OPEN,
    "active": STATUS_IN_PROGRESS,
    "in progress": STATUS_IN_PROGRESS,
    "doing": STATUS_IN_PROGRESS,
    "committed": STATUS_IN_PROGRESS,
    "resolved": STATUS_CLOSED,
    "closed": STATUS_CLOSED,
    "done": STATUS_CLOSED,
    "completed": STATUS_CLOSED,
    "removed": STATUS_CLOSED,
}
_WIQL_STATES = {
    STATUS_OPEN: ("New", "To Do", "Proposed", "Approved"),
    STATUS_IN_PROGRESS: ("Active", "In Progress", "Doing", "Committed"),
    STATUS_CLOSED: ("Resolved", "Closed", "Done", "Completed"),
}
_KIND_TO_TYPE = {"bug": "Bug", "story": "User Story", "task": "Task"}
_BARE_FIELD_PATHS = {
    "title": "System.Title",
    "description": "System.Description",
    "status": "System.State",
}

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(value: Any) -> str:
    """Strip HTML tags + unescape entities (System.Description is HTML)."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    return " ".join(html.unescape(text).split())


def _wiql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_wiql(spec: WorkQuerySpec) -> str:
    """Render a WorkQuerySpec as flat WIQL ordered by ChangedDate DESC."""
    clauses: list[str] = []
    if spec.project:
        clauses.append(f"[System.TeamProject] = {_wiql_quote(spec.project)}")
    if spec.statuses:
        status_terms = []
        for status in spec.statuses:
            states = _WIQL_STATES.get(status)
            if states:
                quoted = ", ".join(_wiql_quote(state) for state in states)
                status_terms.append(f"[System.State] IN ({quoted})")
        if len(status_terms) == 1:
            clauses.append(status_terms[0])
        elif status_terms:
            clauses.append("(" + " OR ".join(status_terms) + ")")
    if spec.kinds:
        types = ", ".join(_wiql_quote(_KIND_TO_TYPE.get(k, k.capitalize())) for k in spec.kinds)
        clauses.append(f"[System.WorkItemType] IN ({types})")
    if spec.assigned_to_me:
        clauses.append("[System.AssignedTo] = @Me")
    if spec.sprint == CURRENT_SPRINT:
        clauses.append("[System.IterationPath] = @CurrentIteration")
    elif spec.sprint:
        if spec.project:
            path = f"{spec.project}\\{spec.sprint}"
            clauses.append(f"[System.IterationPath] UNDER {_wiql_quote(path)}")
        else:
            clauses.append(f"[System.IterationPath] = {_wiql_quote(spec.sprint)}")
    for phrase in spec.phrases:
        clauses.append(f"[System.Title] CONTAINS {_wiql_quote(phrase)}")
    if spec.created_window == "today":
        clauses.append("[System.CreatedDate] >= @StartOfDay")
    elif spec.created_window == "this_week":
        clauses.append("[System.CreatedDate] >= @StartOfWeek")
    elif spec.created_window == "last_week":
        clauses.append(
            "[System.CreatedDate] >= @StartOfWeek('-1') AND [System.CreatedDate] < @StartOfWeek"
        )
    elif spec.created_window == "this_month":
        clauses.append("[System.CreatedDate] >= @StartOfMonth")
    if spec.fallback_text:
        clauses.append(f"[System.Title] CONTAINS {_wiql_quote(spec.fallback_text)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT [System.Id] FROM WorkItems{where} ORDER BY [System.ChangedDate] DESC"


def _patch_path(name: str) -> str:
    """JSON-patch field path for an enrichment/draft field name (see module doc)."""
    if "." in name:
        return f"/fields/{name}"
    mapped = _BARE_FIELD_PATHS.get(name.lower())
    if mapped is None:
        raise ValueError(
            f"ado field {name!r} is ambiguous: use a fully-qualified reference name "
            "like 'System.Tags' or 'Microsoft.VSTS.Common.Priority' "
            "(bare names supported: title, description, status)"
        )
    return f"/fields/{mapped}"


@AdapterRegistry.register(PortKind.WORK_TRACKING, PROVIDER)
class AdoWorkTrackingAdapter:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        base_url = str(options.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(
                f"ado connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://dev.azure.com/<org>")'
            )
        project = str(options.get("project") or "")
        if not project:
            raise ValueError(f"ado connection {conn.id!r} is missing options['project']")
        if secret is None:
            raise ValueError(
                f"ado connection {conn.id!r} requires a Personal Access Token; set "
                'secret_ref on the connection (e.g. "env:APEX_ADO_PAT")'
            )
        self._base_url = base_url
        self._project = project
        token = base64.b64encode(f":{secret.value}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
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
        headers: dict[str, str] | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        try:
            response = await resilient_request(
                self._client(), method, path, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"ado request {method} {path} failed before a response arrived: {exc}"
            ) from exc
        if response.status_code == 404 and not_found is not None:
            raise KeyError(not_found)
        # ADO answers unauthenticated API calls with a 203 + sign-in HTML page.
        if response.status_code in (203, 401, 403):
            raise RuntimeError(
                f"azure devops rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's PAT secret "
                "and its work-items scope"
            )
        if response.status_code == 400:
            raise ValueError(f"azure devops rejected the request: {_error_text(response)}")
        if response.status_code >= 400:
            raise RuntimeError(
                f"azure devops {method} {path} failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        return response

    # ── port surface ──────────────────────────────────────────────────────────

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        """Deterministic keyword/pattern NL->WIQL translation.

        LLM-backed translation is a planned upgrade — same port surface, only
        this method's internals change.
        """
        spec = parse_work_query(natural_language, default_project=self._project, context=context)
        return TranslatedQuery(
            provider=PROVIDER, query=render_wiql(spec), confidence=confidence_for(spec)
        )

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage:
        """WIQL returns every matching id; offset/limit slice that id list
        client-side, then one batch GET hydrates the visible window."""
        response = await self._request(
            "POST",
            f"/{self._project}/_apis/wit/wiql",
            params={"api-version": _API_VERSION},
            json={"query": query.query},
        )
        ids = [int(ref["id"]) for ref in response.json().get("workItems") or []]
        window = ids[page.offset : page.offset + page.limit]
        items: list[WorkItem] = []
        if window:
            batch = await self._request(
                "GET",
                "/_apis/wit/workitems",
                params={
                    "ids": ",".join(str(i) for i in window),
                    "fields": ",".join(_ITEM_FIELDS),
                    "api-version": _API_VERSION,
                },
            )
            by_id = {int(row["id"]): row for row in batch.json().get("value") or []}
            items = [self._work_item_from_row(by_id[i]) for i in window if i in by_id]
        return WorkItemPage(items=items, total=len(ids), page=page)

    async def get_item(self, key: str) -> WorkItem:
        response = await self._request(
            "GET",
            f"/_apis/wit/workitems/{key}",
            params={"fields": ",".join(_ITEM_FIELDS), "api-version": _API_VERSION},
            not_found=f"work item {key!r} not found in azure devops",
        )
        return self._work_item_from_row(response.json())

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
        spec = WorkQuerySpec(
            project=self._project,
            statuses=(filters.status,) if filters.status else (),
            kinds=(filters.kind,) if filters.kind else (),
            phrases=(filters.text,) if filters.text else (),
        )
        query = TranslatedQuery(provider=PROVIDER, query=render_wiql(spec))
        return await self.execute_query(query, page=page)

    async def create_item(self, draft: WorkItemDraft) -> WorkItem:
        work_item_type = _KIND_TO_TYPE.get(draft.kind, draft.kind.capitalize())
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": draft.title}
        ]
        if draft.description:
            patch.append(
                {"op": "add", "path": "/fields/System.Description", "value": draft.description}
            )
        for name, value in draft.fields.items():
            patch.append({"op": "add", "path": _patch_path(name), "value": value})
        response = await self._request(
            "POST",
            f"/{self._project}/_apis/wit/workitems/${work_item_type}",
            params={"api-version": _API_VERSION},
            json=patch,
            headers={"Content-Type": "application/json-patch+json"},
        )
        return self._work_item_from_row(response.json())

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        """fields -> JSON-patch PATCH; comment -> comments preview API; re-fetch."""
        not_found = f"work item {key!r} not found in azure devops"
        if enrichment.fields:
            patch = [
                {"op": "add", "path": _patch_path(name), "value": value}
                for name, value in enrichment.fields.items()
            ]
            await self._request(
                "PATCH",
                f"/_apis/wit/workitems/{key}",
                params={"api-version": _API_VERSION},
                json=patch,
                headers={"Content-Type": "application/json-patch+json"},
                not_found=not_found,
            )
        if enrichment.comment:
            await self._request(
                "POST",
                f"/{self._project}/_apis/wit/workItems/{key}/comments",
                params={"api-version": _COMMENTS_API_VERSION},
                json={"text": enrichment.comment},
                not_found=not_found,
            )
        return await self.get_item(key)

    # ── mapping ───────────────────────────────────────────────────────────────

    def _work_item_from_row(self, row: dict[str, Any]) -> WorkItem:
        fields = row.get("fields") or {}
        item_id = str(row.get("id", ""))
        state = str(fields.get("System.State") or "").lower()
        work_item_type = str(fields.get("System.WorkItemType") or "story")
        kind = "story" if work_item_type.lower() == "user story" else work_item_type.lower()
        project = str(fields.get("System.TeamProject") or self._project)
        return WorkItem(
            key=item_id,
            title=str(fields.get("System.Title") or ""),
            kind=kind,
            status=_STATE_TO_STATUS.get(state, state or "open"),
            description=html_to_text(fields.get("System.Description")),
            url=f"{self._base_url}/{project}/_workitems/edit/{item_id}",
        )


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    message = data.get("message")
    return str(message) if message else response.text[:300]
