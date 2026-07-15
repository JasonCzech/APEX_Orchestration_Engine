"""Azure DevOps work-tracking adapter (provider "ado", PortKind.WORK_TRACKING).

Wire surface: Azure DevOps Services REST 7.1 (WIQL + work items). Connection
options: {"base_url": "https://dev.azure.com/<org>", "project": "<project>"};
the secret is a Personal Access Token sent as HTTP basic auth with an empty
username (":PAT" base64-encoded).

Wire-format decisions (documented deviations / mappings):
- execute_query: POST {project}/_apis/wit/wiql?api-version=7.1 uses ADO's `$top`
  cap to request only the visible prefix plus one look-ahead id. Page.offset/limit
  are applied client-side to that bounded id list before one batch
  GET _apis/wit/workitems?ids=...&fields=... hydration call.
  WorkItemPage.total is exact when the response is shorter than `$top`, otherwise
  it is a lower bound.
- System.Description is HTML; a small tag-strip + entity-unescape pass turns it
  into plain text. System.State is normalized: New/To Do/Proposed/Approved ->
  open, Active/In Progress/Doing/Committed -> in_progress,
  Resolved/Closed/Done/Completed/Removed -> closed; unknown states pass through
  lowercased. Kind maps "User Story" <-> "story"; other types lowercase.
- create_item: POST {project}/_apis/wit/workitems/${Type}?api-version=7.1 with
  an application/json-patch+json body (System.Title, System.Description, then
  draft.fields: dotted keys verbatim as /fields/<key>, bare keys must be
  title/description — anything else raises ValueError). Project fields and
  area/iteration paths outside the configured project are rejected.
- enrich_item: enrichment.fields -> PATCH {project}/_apis/wit/workitems/{id} JSON-patch
  ("add" ops — ADO treats add as upsert). Bare keys map title -> System.Title,
  description -> System.Description, status -> System.State (the raw value is
  sent; callers supply a real ADO state name); dotted keys are used verbatim;
  other bare keys raise ValueError. The item and its revision are fetched from
  the project route first, and every field patch starts with an atomic `/rev`
  test so a concurrent cross-project move cannot race the ownership check.
  enrichment.comment -> POST
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
from urllib.parse import quote

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

PROVIDER = "ado"
_TIMEOUT_S = 15.0
_API_VERSION = "7.1"
_COMMENTS_API_VERSION = "7.1-preview.3"
_MUTATION_MARKER = re.compile(r"\Aapex-idem-[0-9a-f]{32}\Z")
_FIELD_REFERENCE_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+\Z")
_COMMENT_PAGE_SIZE = 200
_MAX_COMMENT_SCAN = 10_000
_MAX_ITEM_ID = 2_147_483_647
_ITEM_FIELDS = [
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.TeamProject",
    "System.Description",
    "System.Tags",
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
    """Strip HTML tags + unescape entities into a bounded domain description."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    normalized = " ".join(html.unescape(text).split())
    if len(normalized) > MAX_DESCRIPTION_CHARS:
        return normalized[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return normalized


def _wiql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _project_path_segment(value: str) -> str:
    """Return one encoded ADO route segment without permitting route traversal."""

    if (
        value != value.strip()
        or len(value) > 255
        or value in {".", ".."}
        # Percent syntax is rejected as well as literal delimiters. A double-
        # decoding proxy must not turn an encoded separator or dot traversal
        # into a second authenticated route segment.
        or any(char in value for char in "/\\?#%")
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError("azure devops project contains unsafe path characters")
    return quote(value, safe="")


def _work_item_type_path_segment(kind: str) -> str:
    """Return the ADO ``$type`` route suffix as exactly one encoded segment."""

    work_item_type = _KIND_TO_TYPE.get(kind, kind.capitalize())
    if (
        not work_item_type
        or work_item_type != work_item_type.strip()
        or len(work_item_type) > 64
        or work_item_type in {".", ".."}
        # Reject percent syntax as well as delimiters. Encoding it again would
        # be safe for httpx, but downstream proxies have historically differed
        # on whether percent-encoded separators are decoded once or twice.
        or any(char in work_item_type for char in "/\\?#%")
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in work_item_type)
    ):
        raise ValueError("azure devops work item type contains unsafe path characters")
    return quote(work_item_type, safe="")


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
    if name.casefold() == "system.history":
        raise WorkTrackingMutationRejectedError(
            "azure devops System.History is append-only and cannot be used in "
            "replayable field updates; use enrichment.comment instead"
        )
    if "." in name:
        # ADO field reference names are identifiers, not arbitrary JSON
        # pointers.  In particular, '/' and '~' would change JSON Pointer
        # semantics instead of naming a field.  Keep the provider path flat and
        # reject ambiguous input before the PAT-authenticated request.
        if len(name) > 128 or _FIELD_REFERENCE_NAME.fullmatch(name) is None:
            raise ValueError(
                "ado field must be a bounded reference name using letters, digits, "
                "underscores, hyphens, and periods"
            )
        return f"/fields/{name}"
    mapped = _BARE_FIELD_PATHS.get(name.lower())
    if mapped is None:
        raise ValueError(
            f"ado field {name!r} is ambiguous: use a fully-qualified reference name "
            "like 'System.Tags' or 'Microsoft.VSTS.Common.Priority' "
            "(bare names supported: title, description, status)"
        )
    return f"/fields/{mapped}"


def _item_id(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{context} has no valid id")
    if value < 1 or value > _MAX_ITEM_ID:
        raise RuntimeError(f"{context} has no valid id")
    return value


def _list_field(data: dict[str, Any], field: str, *, context: str) -> list[Any]:
    value = data.get(field)
    if not isinstance(value, list):
        raise RuntimeError(f"azure devops {context} field {field!r} must be a list")
    return value


@AdapterRegistry.register(PortKind.WORK_TRACKING, PROVIDER)
class AdoWorkTrackingAdapter:
    provider = PROVIDER

    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        raw_base_url = options.get("base_url")
        if not isinstance(raw_base_url, str) or not raw_base_url:
            raise ValueError(
                f"ado connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://dev.azure.com/<org>")'
            )
        base_url = raw_base_url.rstrip("/")
        raw_project = options.get("project")
        if not isinstance(raw_project, str) or not raw_project:
            raise ValueError(f"ado connection {conn.id!r} is missing options['project']")
        project = raw_project
        project_path = _project_path_segment(project)
        if secret is None:
            raise ValueError(
                f"ado connection {conn.id!r} requires a Personal Access Token; set "
                'secret_ref on the connection (e.g. "env:APEX_INTEGRATION_ADO_PAT")'
            )
        pat = require_bounded_credential(
            secret.value,
            label="azure devops PAT",
        )
        self._base_url = base_url
        self._project = project
        self._project_path = project_path
        self._allow_private_hosts = private_hosts_allowed(options)
        token = base64.b64encode(f":{pat}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    @property
    def project_id(self) -> str:
        """Configured project boundary used by scoped direct-item routes."""

        return self._project

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
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
        headers: dict[str, str] | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        try:
            response = await resilient_request(
                self._client(), method, path, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            raise RuntimeError(
                bounded_diagnostic(
                    f"ado request {method} {path} failed before a response arrived: {detail}"
                )
            ) from exc
        if response.status_code == 404 and not_found is not None:
            raise WorkTrackingMutationTargetNotFoundError(not_found)
        # ADO answers unauthenticated API calls with a 203 + sign-in HTML page.
        if response.status_code in (203, 401, 403):
            raise RuntimeError(
                f"azure devops rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's PAT secret "
                "and its work-items scope"
            )
        if response.status_code == 400:
            raise WorkTrackingMutationRejectedError(
                f"azure devops rejected the request: {_error_text(response)}"
            )
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
        """Request one bounded id prefix, then hydrate only the visible window."""
        if query.provider.casefold() != PROVIDER:
            raise ValueError("translated query provider does not match azure devops")
        validate_provider_page(page)
        target = page.offset + page.limit
        requested_ids = target + 1
        response = await self._request(
            "POST",
            f"/{self._project_path}/_apis/wit/wiql",
            params={"api-version": _API_VERSION, "$top": requested_ids},
            json={"query": query.query},
        )
        data = _json_object(response, "WIQL query")
        raw_refs = _list_field(data, "workItems", context="WIQL response")
        refs = raw_refs[:requested_ids]
        ids: list[int] = []
        seen_ids: set[int] = set()
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                raise RuntimeError(
                    f"azure devops WIQL response workItems[{index}] must be an object"
                )
            item_id = _item_id(
                ref.get("id"), context=f"azure devops WIQL response workItems[{index}]"
            )
            if item_id in seen_ids:
                raise RuntimeError(
                    f"azure devops WIQL response workItems[{index}] has a duplicate id"
                )
            seen_ids.add(item_id)
            ids.append(item_id)
        window = ids[page.offset : target]
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
            batch_data = _json_object(batch, "work item batch")
            raw_rows = _list_field(batch_data, "value", context="work item batch response")
            if len(raw_rows) > len(window):
                raise RuntimeError(
                    "azure devops work item batch response has an invalid 'value' list"
                )
            by_id: dict[int, dict[str, Any]] = {}
            expected_ids = set(window)
            for index, row in enumerate(raw_rows):
                if not isinstance(row, dict):
                    raise RuntimeError(
                        f"azure devops work item batch row {index} must be an object"
                    )
                row_id = _item_id(
                    row.get("id"), context=f"azure devops work item batch row {index}"
                )
                if row_id not in expected_ids:
                    raise RuntimeError(
                        f"azure devops work item batch row {index} has an unexpected id"
                    )
                if row_id in by_id:
                    raise RuntimeError(
                        f"azure devops work item batch row {index} has a duplicate id"
                    )
                by_id[row_id] = row
            items = [self._work_item_from_row(by_id[i]) for i in window if i in by_id]
        total = len(ids) if len(raw_refs) < requested_ids else requested_ids
        return WorkItemPage(items=items, total=total, page=page)

    async def get_item(self, key: str) -> WorkItem:
        _row, item = await self._get_item_snapshot(key)
        return item

    async def _get_item_snapshot(self, key: str) -> tuple[dict[str, Any], WorkItem]:
        """Read and validate one project-owned item plus its mutation revision."""

        if (
            not key.isascii()
            or not key.isdecimal()
            or len(key) > 10
            or int(key) < 1
            or int(key) > _MAX_ITEM_ID
        ):
            raise WorkTrackingMutationTargetNotFoundError(
                f"work item {key!r} is not a valid azure devops work item id"
            )
        response = await self._request(
            "GET",
            f"/{self._project_path}/_apis/wit/workitems/{key}",
            params={"fields": ",".join(_ITEM_FIELDS), "api-version": _API_VERSION},
            not_found=f"work item {key!r} not found in azure devops",
        )
        row = _json_object(response, "work item")
        item = self._work_item_from_row(row)
        _item_revision(row)
        return row, item

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
        return await self._create_item(draft, marker=None)

    async def find_item_by_idempotency_marker(self, marker: str) -> WorkItem | None:
        """Reconcile an ambiguous create through the APEX System.Tags marker."""

        self._validate_mutation_marker(marker)
        response = await self._request(
            "POST",
            f"/{self._project_path}/_apis/wit/wiql",
            params={"api-version": _API_VERSION, "$top": 2},
            json={
                "query": (
                    "SELECT [System.Id] FROM WorkItems WHERE "
                    f"[System.TeamProject] = {_wiql_quote(self._project)} AND "
                    f"[System.Tags] CONTAINS {_wiql_quote(marker)}"
                )
            },
        )
        data = _json_object(response, "idempotency marker query")
        raw_items = _list_field(data, "workItems", context="idempotency query response")
        if len(raw_items) > 2:
            raise RuntimeError("azure devops idempotency marker is attached to multiple items")
        ids: list[str] = []
        seen_ids: set[int] = set()
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise RuntimeError(f"azure devops idempotency query row {index} has no valid id")
            item_id = _item_id(
                item.get("id"), context=f"azure devops idempotency query row {index}"
            )
            if item_id in seen_ids:
                raise RuntimeError("azure devops idempotency marker is attached to multiple items")
            seen_ids.add(item_id)
            ids.append(str(item_id))
        if len(ids) > 1:
            raise RuntimeError("azure devops idempotency marker is attached to multiple items")
        return await self.get_item(ids[0]) if ids else None

    async def create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> WorkItem:
        self.validate_create_item_idempotent(draft, marker=marker)
        return await self._create_item(draft, marker=marker)

    def validate_create_item_idempotent(self, draft: WorkItemDraft, *, marker: str) -> None:
        """Validate all local create constraints before fencing the provider POST."""

        self._validate_mutation_marker(marker)
        _work_item_type_path_segment(draft.kind)
        self._reject_project_mutations(draft.fields)
        for name in draft.fields:
            _patch_path(name)
        tags = next(
            (value for name, value in draft.fields.items() if name.casefold() == "system.tags"),
            None,
        )
        if tags is not None and not isinstance(tags, str):
            raise WorkTrackingMutationRejectedError("azure devops System.Tags must be a string")

    async def _create_item(self, draft: WorkItemDraft, *, marker: str | None) -> WorkItem:
        self._reject_project_mutations(draft.fields)
        work_item_type = _work_item_type_path_segment(draft.kind)
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": draft.title}
        ]
        if draft.description:
            patch.append(
                {"op": "add", "path": "/fields/System.Description", "value": draft.description}
            )
        fields = dict(draft.fields)
        if marker is not None:
            tag_name = next(
                (name for name in fields if name.casefold() == "system.tags"),
                "System.Tags",
            )
            current_tags = fields.get(tag_name)
            if current_tags is not None and not isinstance(current_tags, str):
                raise ValueError("azure devops System.Tags must be a string")
            tags = [tag.strip() for tag in str(current_tags or "").split(";") if tag.strip()]
            if marker not in tags:
                tags.append(marker)
            fields[tag_name] = "; ".join(tags)
        for name, value in fields.items():
            patch.append({"op": "add", "path": _patch_path(name), "value": value})
        response = await self._request(
            "POST",
            f"/{self._project_path}/_apis/wit/workitems/${work_item_type}",
            params={"api-version": _API_VERSION},
            json=patch,
            headers={"Content-Type": "application/json-patch+json"},
        )
        return self._work_item_from_row(_json_object(response, "created work item"))

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        """fields -> JSON-patch PATCH; comment -> comments preview API; re-fetch."""
        self._reject_project_mutations(enrichment.fields)
        for name in enrichment.fields:
            _patch_path(name)
        # The item endpoint is organization-wide. Validate TeamProject before any
        # PATCH/comment so a numeric id from another project cannot be mutated.
        row, _item = await self._get_item_snapshot(key)
        revision = _item_revision(row)
        not_found = f"work item {key!r} not found in azure devops"
        if enrichment.fields:
            patch = [
                {"op": "test", "path": "/rev", "value": revision},
                *[
                    {"op": "add", "path": _patch_path(name), "value": value}
                    for name, value in enrichment.fields.items()
                ],
            ]
            await self._request(
                "PATCH",
                f"/{self._project_path}/_apis/wit/workitems/{key}",
                params={"api-version": _API_VERSION},
                json=patch,
                headers={"Content-Type": "application/json-patch+json"},
                not_found=not_found,
            )
        if enrichment.comment:
            await self._request(
                "POST",
                f"/{self._project_path}/_apis/wit/workItems/{key}/comments",
                params={"api-version": _COMMENTS_API_VERSION},
                json={"text": enrichment.comment},
                not_found=not_found,
            )
        return await self.get_item(key)

    async def update_item_fields_idempotent(self, key: str, fields: dict[str, object]) -> None:
        """Upsert exact values; repeating the same JSON patch has no extra effect."""

        self.validate_update_item_fields_idempotent(fields)
        row, _item = await self._get_item_snapshot(key)
        if not fields:
            return
        patch = [
            {"op": "test", "path": "/rev", "value": _item_revision(row)},
            *[
                {"op": "add", "path": _patch_path(name), "value": value}
                for name, value in fields.items()
            ],
        ]
        await self._request(
            "PATCH",
            f"/{self._project_path}/_apis/wit/workitems/{key}",
            params={"api-version": _API_VERSION},
            json=patch,
            headers={"Content-Type": "application/json-patch+json"},
            not_found=f"work item {key!r} not found in azure devops",
        )

    def validate_update_item_fields_idempotent(self, fields: dict[str, object]) -> None:
        """Reject append-style or ambiguous fields before any provider call."""

        self._reject_project_mutations(fields)
        for name in fields:
            _patch_path(name)

    async def has_comment_idempotency_marker(self, key: str, marker: str) -> bool:
        self._validate_mutation_marker(marker)
        await self.get_item(key)
        token = _comment_marker(marker)
        continuation: str | None = None
        scanned = 0
        while scanned < _MAX_COMMENT_SCAN:
            params: dict[str, Any] = {
                "api-version": _COMMENTS_API_VERSION,
                "$top": _COMMENT_PAGE_SIZE,
            }
            if continuation:
                params["continuationToken"] = continuation
            response = await self._request(
                "GET",
                f"/{self._project_path}/_apis/wit/workItems/{key}/comments",
                params=params,
                not_found=f"work item {key!r} not found in azure devops",
            )
            data = _json_object(response, "comments page")
            if "comments" in data:
                raw_comments = data["comments"]
            elif "value" in data:
                raw_comments = data["value"]
            else:
                raise RuntimeError("azure devops comments response has no comments list")
            if not isinstance(raw_comments, list) or len(raw_comments) > _COMMENT_PAGE_SIZE:
                raise RuntimeError(
                    "azure devops comments response exceeded the requested page-size budget"
                )
            if any(not isinstance(comment, dict) for comment in raw_comments):
                raise RuntimeError("azure devops comments response contains a non-object row")
            comments = raw_comments
            if any(token in str(comment.get("text") or "") for comment in comments):
                return True
            scanned += len(comments)
            next_token = data.get("continuationToken") or response.headers.get(
                "x-ms-continuationtoken"
            )
            if not comments or not next_token:
                return False
            if not isinstance(next_token, str):
                raise RuntimeError("azure devops returned an invalid comment continuation token")
            continuation = next_token
            if not continuation or len(continuation) > 2_048 or "\x00" in continuation:
                raise RuntimeError("azure devops returned an invalid comment continuation token")
        raise RuntimeError("azure devops comment reconciliation exceeded its safe scan limit")

    async def add_item_comment_idempotent(self, key: str, comment: str, *, marker: str) -> None:
        self._validate_mutation_marker(marker)
        await self.get_item(key)
        await self._request(
            "POST",
            f"/{self._project_path}/_apis/wit/workItems/{key}/comments",
            params={"api-version": _COMMENTS_API_VERSION},
            json={"text": f"{comment}\n\n{_comment_marker(marker)}"},
            not_found=f"work item {key!r} not found in azure devops",
        )

    @staticmethod
    def _validate_mutation_marker(marker: str) -> None:
        if _MUTATION_MARKER.fullmatch(marker) is None:
            raise ValueError("invalid APEX work-item mutation marker")

    def _reject_project_mutations(self, fields: dict[str, Any] | dict[str, object]) -> None:
        for name, value in fields.items():
            normalized = name.casefold()
            if normalized in {
                "project",
                "teamproject",
                "system.teamproject",
                # These writable classification-node identifiers can select a
                # node without exposing the owning project in the request.
                # Unlike AreaPath/IterationPath below, they therefore cannot be
                # proved to remain inside the configured project locally.
                "system.areaid",
                "system.iterationid",
            }:
                raise ValueError(
                    "azure devops project is fixed by the connection and cannot be changed"
                )
            if normalized in {"system.areapath", "system.iterationpath"}:
                if not isinstance(value, str):
                    raise ValueError(f"azure devops {name} must be a string path")
                root = value.split("\\", 1)[0]
                if root.casefold() != self._project.casefold():
                    raise ValueError(
                        f"azure devops {name} must remain inside the configured project"
                    )

    # ── mapping ───────────────────────────────────────────────────────────────

    def _work_item_from_row(self, row: dict[str, Any]) -> WorkItem:
        raw_fields = row.get("fields")
        if not isinstance(raw_fields, dict):
            raise RuntimeError("azure devops work item response has malformed fields")
        fields = raw_fields
        item_id = str(_item_id(row.get("id"), context="azure devops work item response"))
        raw_state = fields.get("System.State", "")
        raw_work_item_type = fields.get("System.WorkItemType", "story")
        raw_project = fields.get("System.TeamProject", "")
        raw_title = fields.get("System.Title", "")
        raw_description = fields.get("System.Description")
        if (
            not isinstance(raw_state, str)
            or not isinstance(raw_work_item_type, str)
            or not isinstance(raw_project, str)
            or not isinstance(raw_title, str)
            or (raw_description is not None and not isinstance(raw_description, str))
        ):
            raise RuntimeError("azure devops work item response contains malformed text fields")
        state = raw_state.lower()
        work_item_type = raw_work_item_type
        kind = "story" if work_item_type.lower() == "user story" else work_item_type.lower()
        project = raw_project
        if project.casefold() != self._project.casefold():
            raise WorkTrackingMutationTargetNotFoundError(
                f"work item {item_id!r} not found in configured azure devops project"
            )
        try:
            return WorkItem(
                key=item_id,
                title=raw_title,
                kind=kind,
                status=_STATE_TO_STATUS.get(state, state or "open"),
                description=html_to_text(raw_description),
                url=f"{self._base_url}/{quote(project, safe='')}/_workitems/edit/{item_id}",
            )
        except ValidationError as exc:
            raise RuntimeError("azure devops returned an invalid work item") from exc


def _error_text(response: httpx.Response) -> str:
    try:
        data = parse_json_response(response, context="azure devops error response")
    except RuntimeError:
        return bounded_diagnostic(response.text)
    if not isinstance(data, dict):
        return bounded_diagnostic(response.text)
    message = data.get("message")
    return bounded_diagnostic(message) if message else bounded_diagnostic(response.text)


def _item_revision(row: dict[str, Any]) -> int:
    revision = row.get("rev")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision > _MAX_ITEM_ID
    ):
        raise RuntimeError("azure devops work item response has no valid revision")
    return revision


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    data = parse_json_response(response, context=f"azure devops {context} response")
    if not isinstance(data, dict):
        raise RuntimeError(f"azure devops {context} response must be a JSON object")
    return data


def _comment_marker(marker: str) -> str:
    return f"[APEX-IDEMPOTENCY:{marker}]"
