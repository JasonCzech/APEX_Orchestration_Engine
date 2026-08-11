"""Jira Align work-tracking adapter (provider "jira_align", PortKind.WORK_TRACKING).

Wire surface: Jira Align REST API 2.0
(https://<instance>.jiraalign.com/rest/align/api/2/<Resource>).
Connection options: {"base_url": "https://<instance>.jiraalign.com",
"program_id"?: "42", "user_email"?: "<atlassian account email>"}.

Auth. Two schemes, selected by whether ``user_email`` is configured:
- absent  -> ``Authorization: Bearer <token>`` with a Jira Align API token
  (generated on the user's Jira Align profile page; it does not expire).
- present -> HTTP basic ``email:token`` with an Atlassian API token, which is
  what an instance using Atlassian Guard authentication requires.

READ-ONLY. `create_item` / `enrich_item` raise
`WorkTrackingMutationRejectedError`; work items are authored in Jira Align
itself. The durable mutation coordinator also refuses this adapter outright
because it does not implement `IdempotentWorkTrackingMutationPort`.

Wire-format decisions (documented deviations / mappings):
- One endpoint per object type, so a query spanning types fans out across
  resources. `WorkItem.kind` selects the resource; with no kind the adapter
  reads Stories and Defects, the two types story analysis and defect tracking
  care about.
- Jira Align ids are numeric and only unique WITHIN a resource, so `13` is
  ambiguous. Keys are therefore `"<kind>:<id>"` ("story:13", "bug:481"), which
  makes `get_item` a pure function of the key.
- `TranslatedQuery.query` holds a canonical OData predicate:
      [programId eq <n> and] [itemtype in ('story','bug') and] <residual>
  `itemtype` is synthetic — the adapter strips it to pick resources and never
  sends it upstream — and `programId` is re-rendered per resource because the
  program field is named differently on portfolio objects. The router's
  project-scope wrapper may parenthesize the rest; the parser unwraps that.
- Status comes from the numeric `state` field via a per-kind table; an
  unrecognized value passes through as "state-<n>" rather than being dropped.
- Paging is `$skip`/`$top` with an explicit `$orderby=id desc` so a window is
  reproducible. `WorkItemPage.total` is exact only when every resource returned
  a short page; otherwise it is a lower bound (fetched + 1), matching the Jira
  Cloud adapter's contract.
- `assigned_to_me` and "current sprint" are dropped from translation: neither
  has a server-side equivalent here (no `currentUser()` function, no open-sprint
  predicate). A named numeric sprint is still rendered.

VERIFY AGAINST THE INSTANCE. Jira Align sites customize workflows, and the
per-instance Swagger at /rest/align/api/docs/index.html is authoritative for:
the `_RESOURCES` state tables, the per-resource program field, and whether
`contains()` is available in `$filter`. Everything unrecognized degrades to a
passthrough label rather than an error, so a stale table shows odd statuses
instead of breaking reads. `_TEXT_FILTER_SUPPORTED` turns off `contains()`
rendering for a site that rejects it.

The httpx.AsyncClient is lazy and per-instance, but rebuilt if the running
event loop changes: resolver-cached adapter instances are reused across the
short-lived loops that graph nodes spin up, and a pooled connection bound to a
closed loop would otherwise fail.
"""

import asyncio
import base64
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
from apex.services.work_items import safe_work_item_url
from apex.services.work_tracking import (
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    WorkQuerySpec,
    confidence_for,
    parse_work_query,
    validate_provider_page,
)

PROVIDER = "jira_align"
_BASE_PATH = "/rest/align/api/2"
_TIMEOUT_S = 15.0
_MAX_RESULTS_PER_REQUEST = 100

# Sites that reject `contains()` in $filter can flip this off; phrases are then
# dropped from translation instead of producing a query the server rejects.
_TEXT_FILTER_SUPPORTED = True

_READ_ONLY = "jira_align connections are read-only; author work items in Jira Align itself"

# Documented story workflow: Pending, Ready, Building, Testing, Tested,
# Accepted. Defects are open/closed. Portfolio objects reuse the story shape.
_STORY_STATES = {
    0: STATUS_OPEN,
    1: STATUS_OPEN,
    2: STATUS_IN_PROGRESS,
    3: STATUS_IN_PROGRESS,
    4: STATUS_IN_PROGRESS,
    5: STATUS_CLOSED,
}
_DEFECT_STATES = {0: STATUS_OPEN, 1: STATUS_CLOSED}


@dataclass(frozen=True)
class _ResourceSpec:
    """Everything kind-specific about one Jira Align object type."""

    resource: str
    program_field: str
    states: dict[int, str]
    parent_field: str | None = None
    parent_kind: str | None = None


_RESOURCES: dict[str, _ResourceSpec] = {
    "story": _ResourceSpec("Stories", "programId", _STORY_STATES, "featureId", "feature"),
    "bug": _ResourceSpec("Defects", "programId", _DEFECT_STATES, "storyId", "story"),
    "task": _ResourceSpec("Tasks", "programId", _STORY_STATES, "storyId", "story"),
    "feature": _ResourceSpec(
        "Features", "primaryProgramId", _STORY_STATES, "parentId", "capability"
    ),
    "capability": _ResourceSpec(
        "Capabilities", "primaryProgramId", _STORY_STATES, "parentId", "epic"
    ),
    "epic": _ResourceSpec("Epics", "primaryProgramId", _STORY_STATES, "themeId", "theme"),
    "objective": _ResourceSpec("Objectives", "programId", _STORY_STATES),
    "theme": _ResourceSpec("Themes", "primaryProgramId", _STORY_STATES),
}
# Story analysis and defect tracking are the two features this adapter serves,
# so an untyped query reads exactly those two resources.
_DEFAULT_QUERY_KINDS = ("story", "bug")

# Jira Align has real Features, Epics, and Capabilities, so it must not reuse
# the shared default table where "feature" is a synonym for "story".
_ALIGN_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("bug", r"\b(?:bugs?|defects?)\b"),
    ("story", r"\b(?:user\s+stor(?:y|ies)|stor(?:y|ies))\b"),
    ("feature", r"\bfeatures?\b"),
    ("epic", r"\bepics?\b"),
    ("capability", r"\bcapabilit(?:y|ies)\b"),
    ("objective", r"\b(?:objectives?|okrs?)\b"),
    ("theme", r"\bthemes?\b"),
    ("task", r"\btasks?\b"),
)

_KEY = re.compile(r"\A(?P<kind>[a-z]{3,16}):(?P<id>[0-9]{1,18})\Z")
_NUMERIC_ID = re.compile(r"\A[0-9]{1,18}\Z")
_PROGRAM_TERM = re.compile(r"\AprogramId\s+eq\s+(?P<id>[0-9]{1,18})\Z", re.IGNORECASE)
_ITEMTYPE_TERM = re.compile(r"\Aitemtype\s+in\s*\((?P<kinds>[^()]*)\)\Z", re.IGNORECASE)
_QUOTED_KIND = re.compile(r"'([A-Za-z]{3,16})'")
_AND = re.compile(r"\band\b", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

_ASSIGNEE_KEYS = ("ownerName", "assigneeName", "owner")
_PRIORITY_KEYS = ("priority", "priorityName")
_SEVERITY_KEYS = ("severity", "severityName")
_SPRINT_KEYS = ("sprintName", "sprintId")
_URL_KEYS = ("url", "link", "self")


# ── canonical query grammar ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AlignQuery:
    """Parsed canonical query: which resources, which program, what filter."""

    program_id: str | None
    kinds: tuple[str, ...]
    residual: str


# A canonical status term. Each resource numbers its states independently, so
# a status stays symbolic until fan-out expands it per resource; the placeholder
# never reaches the wire.
_STATUS_PLACEHOLDER = "itemstatus in ({})"
_ITEMSTATUS_TERM = re.compile(r"\Aitemstatus\s+in\s*\((?P<statuses>[^()]*)\)\Z", re.IGNORECASE)
# Jira Align rejects an empty $filter, and every object carries a positive id.
_MATCH_ALL = "id gt 0"
_MATCH_NONE = "id lt 0"


def _odata_quote(value: str) -> str:
    """Render a string literal; OData escapes a quote by doubling it."""

    return "'" + value.replace("'", "''") + "'"


def _state_predicate(kind: str, statuses: tuple[str, ...]) -> str | None:
    """Render canonical statuses as a disjunction over this kind's states.

    Derived from the kind's state table so a filter can never disagree with the
    mapping used to label the results it returns. `in` is avoided because the
    documented $filter grammar only guarantees comparison operators.
    """

    spec = _RESOURCES.get(kind)
    if spec is None:
        return None
    wanted = set(statuses)
    values = sorted(state for state, status in spec.states.items() if status in wanted)
    if not values:
        return None
    terms = [f"state eq {value}" for value in values]
    return terms[0] if len(terms) == 1 else "(" + " or ".join(terms) + ")"


def render_align_query(spec: WorkQuerySpec) -> str:
    """Render a WorkQuerySpec as the canonical Jira Align predicate."""

    kinds = tuple(kind for kind in spec.kinds if kind in _RESOURCES)
    terms: list[str] = []
    if spec.project:
        if _NUMERIC_ID.fullmatch(spec.project) is None:
            raise ValueError("jira_align program scope must be a numeric program id")
        terms.append(f"programId eq {spec.project}")
    if kinds:
        names = ",".join(_odata_quote(kind) for kind in kinds)
        terms.append(f"itemtype in ({names})")
    if spec.statuses:
        terms.append(_STATUS_PLACEHOLDER.format(",".join(spec.statuses)))
    if spec.sprint and _NUMERIC_ID.fullmatch(spec.sprint):
        terms.append(f"sprintId eq {spec.sprint}")
    if _TEXT_FILTER_SUPPORTED:
        for phrase in spec.phrases:
            terms.append(f"contains(title,{_odata_quote(phrase)})")
        if spec.fallback_text:
            terms.append(f"contains(title,{_odata_quote(spec.fallback_text)})")
    return " and ".join(terms) if terms else _MATCH_ALL


def _split_top_level_and(raw: str) -> list[str]:
    """Split on `and` outside quotes and parentheses."""

    terms: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(raw) and raw[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("jira_align query has unbalanced parentheses")
        elif depth == 0 and char in {"a", "A"}:
            match = _AND.match(raw, index)
            if match is not None:
                terms.append(raw[start : match.start()])
                index = match.end()
                start = index
                continue
        index += 1
    if quote is not None or depth != 0:
        raise ValueError("jira_align query has unbalanced quotes or parentheses")
    terms.append(raw[start:])
    return [term.strip() for term in terms if term.strip()]


def _contains_unquoted_word(raw: str, word: str, *, top_level_only: bool = False) -> bool:
    """True when `word` appears outside string literals (optionally at depth 0)."""

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(raw) and raw[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif (not top_level_only or depth == 0) and raw[
            index : index + len(word)
        ].casefold() == word:
            before = raw[index - 1] if index > 0 else " "
            after_index = index + len(word)
            after = raw[after_index] if after_index < len(raw) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
        index += 1
    return False


def _unwrap(raw: str) -> str | None:
    """Strip one fully-enclosing parenthesis pair, or None when there isn't one."""

    text = raw.strip()
    if not text.startswith("(") or not text.endswith(")"):
        return None
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return None
    return text[1:-1].strip() if depth == 0 else None


_MAX_QUERY_DEPTH = 8


def parse_align_query(raw: str) -> AlignQuery:
    """Extract the synthetic scope/type terms from a canonical query.

    The router wraps a caller's predicate in its own project-scope clause, so
    the synthetic terms can end up a parenthesis level down. The walk descends
    the `and` spine only, and re-wraps whatever it did not consume, because
    hoisting a term out of an `or` group would change what the query means.
    """

    program_id, kinds, statuses, residual = _parse_align_terms(raw, depth=0)
    terms = [*residual]
    if statuses:
        terms.append(_STATUS_PLACEHOLDER.format(",".join(statuses)))
    return AlignQuery(
        program_id=program_id,
        kinds=tuple(kinds),
        residual=" and ".join(term for term in terms if term and term != _MATCH_ALL),
    )


def _parse_align_terms(
    raw: str, *, depth: int
) -> tuple[str | None, list[str], list[str], list[str]]:
    if depth > _MAX_QUERY_DEPTH:
        raise ValueError("jira_align query nests too deeply")
    program_id: str | None = None
    kinds: list[str] = []
    statuses: list[str] = []
    residual: list[str] = []

    def absorb_program(found: str) -> None:
        nonlocal program_id
        if program_id is not None and program_id != found:
            raise ValueError("jira_align query names conflicting programs")
        program_id = found

    for term in _split_top_level_and(raw):
        program = _PROGRAM_TERM.match(term)
        if program is not None:
            absorb_program(program.group("id"))
            continue
        itemtype = _ITEMTYPE_TERM.match(term)
        if itemtype is not None:
            # Validate entry by entry, not via findall: a name the quoted-kind
            # regex cannot match ("story2") must raise like any other unknown
            # type, never silently vanish and widen to the default fan-out.
            for entry in itemtype.group("kinds").split(","):
                stripped = entry.strip()
                if not stripped:
                    continue
                quoted = _QUOTED_KIND.fullmatch(stripped)
                name = quoted.group(1) if quoted else stripped
                kind = name.casefold() if quoted else None
                if kind is None or kind not in _RESOURCES:
                    raise ValueError(f"jira_align query names unknown type {name!r}")
                if kind not in kinds:
                    kinds.append(kind)
            continue
        itemstatus = _ITEMSTATUS_TERM.match(term)
        if itemstatus is not None:
            if statuses:
                # Two ANDed status terms cannot merge — the union would turn
                # "open and closed" (matches nothing) into "open or closed".
                raise ValueError("jira_align queries support at most one status term")
            statuses.extend(
                status.strip()
                for status in itemstatus.group("statuses").split(",")
                if status.strip()
            )
            continue
        inner = _unwrap(term)
        if inner is None:
            residual.append(term)
            continue
        nested_program, nested_kinds, nested_statuses, nested_residual = _parse_align_terms(
            inner, depth=depth + 1
        )
        if nested_program is not None:
            absorb_program(nested_program)
        kinds.extend(kind for kind in nested_kinds if kind not in kinds)
        if nested_statuses:
            if statuses:
                raise ValueError("jira_align queries support at most one status term")
            statuses.extend(nested_statuses)
        if nested_residual:
            residual.append("(" + " and ".join(nested_residual) + ")")
    return program_id, kinds, statuses, residual


# ── payload helpers ───────────────────────────────────────────────────────────


def align_html_to_text(value: Any) -> str:
    """Strip HTML tags + unescape entities into a bounded domain description."""

    if not value or not isinstance(value, str):
        return ""
    text = _TAG.sub(" ", value[: MAX_DESCRIPTION_CHARS * 4])
    normalized = " ".join(html.unescape(text).split())
    if len(normalized) > MAX_DESCRIPTION_CHARS:
        return normalized[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return normalized


def _scalar_text(payload: dict[str, Any], keys: tuple[str, ...], *, limit: int) -> str | None:
    """First present scalar among `keys`, rendered as bounded display text."""

    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                return trimmed[:limit]
        elif isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _identifier(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and _NUMERIC_ID.fullmatch(value.strip()):
        return value.strip()
    return None


def _item_rows(data: Any) -> list[dict[str, Any]]:
    """Accept either a bare array or a common list envelope."""

    rows: Any = data
    if isinstance(data, dict):
        for key in ("data", "items", "results", "value"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            raise RuntimeError("jira_align list response has no recognizable item array")
    if not isinstance(rows, list):
        raise RuntimeError("jira_align list response must be a JSON array")
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("jira_align list response contains a non-object item")
    return rows


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.WORK_TRACKING, PROVIDER)
class JiraAlignWorkTrackingAdapter:
    provider = PROVIDER
    # Declares the whole surface read-only so the durable mutation coordinator
    # rejects writes as a client error (422) instead of an upstream 502.
    read_only = True

    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        raw_base_url = options.get("base_url")
        if not isinstance(raw_base_url, str) or not raw_base_url:
            raise ValueError(
                f"jira_align connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://<instance>.jiraalign.com")'
            )
        self._base_url = raw_base_url.rstrip("/")
        raw_program_id = options.get("program_id")
        if raw_program_id in (None, ""):
            self._program_id = None
        elif isinstance(raw_program_id, str) and _NUMERIC_ID.fullmatch(raw_program_id.strip()):
            # String-only on purpose: the scoped-connection validator
            # (services/connections.py) requires a string for every tracker, so
            # accepting a JSON number here would build a global connection that
            # then fails the moment it is project-scoped.
            self._program_id = raw_program_id.strip()
        else:
            raise ValueError('jira_align program_id must be a numeric string, e.g. "42"')
        if secret is None:
            raise ValueError(
                f"jira_align connection {conn.id!r} requires an API token; set "
                'secret_ref on the connection (e.g. "env:APEX_INTEGRATION_JIRA_ALIGN_TOKEN")'
            )
        api_token = require_bounded_credential(secret.value, label="jira_align API token")
        raw_user_email = options.get("user_email")
        if raw_user_email in (None, ""):
            # Jira Align API token: sent as a bearer credential.
            require_bounded_credential(api_token, label="jira_align API token", header_token=True)
            authorization = f"Bearer {api_token}"
        else:
            user_email = require_bounded_credential(
                raw_user_email,
                label="jira_align user_email",
                max_bytes=320,
            )
            if ":" in user_email:
                raise ValueError("jira_align user_email must not contain ':'")
            encoded = base64.b64encode(f"{user_email}:{api_token}".encode()).decode()
            authorization = f"Basic {encoded}"
        self._allow_private_hosts = private_hosts_allowed(options)
        self._headers = {"Authorization": authorization, "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    @property
    def project_id(self) -> str | None:
        """Configured program boundary used by scoped direct-item routes."""

        return self._program_id

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
        path: str,
        *,
        params: dict[str, Any] | None = None,
        not_found: str | None = None,
    ) -> Any:
        response: httpx.Response | None = None
        transport_failure: RuntimeError | None = None
        try:
            response = await resilient_request(self._client(), "GET", path, params=params)
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            transport_failure = RuntimeError(
                bounded_diagnostic(
                    f"jira_align request GET {path} failed before a response arrived: {detail}"
                )
            )
        if transport_failure is not None:
            raise transport_failure
        if response is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("jira_align request completed without a response")
        if response.status_code == 404 and not_found is not None:
            raise WorkTrackingMutationTargetNotFoundError(not_found)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"jira_align rejected credentials for GET {path} "
                f"(HTTP {response.status_code}): check the connection's API token "
                "secret and, for an Atlassian Guard instance, its user_email"
            )
        if response.status_code == 400:
            raise WorkTrackingMutationRejectedError(
                f"jira_align rejected the request: {bounded_diagnostic(response.text)}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"jira_align request GET {path} failed with HTTP {response.status_code}: "
                f"{bounded_diagnostic(response.text)}"
            )
        return parse_json_response(response, context="jira_align response")

    # ── port surface ──────────────────────────────────────────────────────────

    async def translate_query(
        self, natural_language: str, *, context: QueryContext
    ) -> TranslatedQuery:
        spec = parse_work_query(
            natural_language,
            default_project=self._program_id,
            context=context,
            kind_patterns=_ALIGN_KIND_PATTERNS,
        )
        return TranslatedQuery(
            provider=PROVIDER,
            query=render_align_query(spec),
            confidence=confidence_for(spec),
        )

    async def execute_query(self, query: TranslatedQuery, *, page: Page) -> WorkItemPage:
        """Fan out across the selected resources and slice the merged window."""

        if query.provider.casefold() != PROVIDER:
            raise ValueError("translated query provider does not match jira_align")
        validate_provider_page(page)
        parsed = parse_align_query(query.query)
        if (
            parsed.program_id is not None
            and self._program_id is not None
            and parsed.program_id != self._program_id
        ):
            raise ValueError("jira_align query targets a program outside the connection")
        program_id = parsed.program_id or self._program_id
        kinds = parsed.kinds or _DEFAULT_QUERY_KINDS
        target = page.offset + page.limit
        merged: list[tuple[int, str, WorkItem]] = []
        exhausted = True
        for kind in kinds:
            items, kind_exhausted = await self._read_resource(
                kind,
                program_id=program_id,
                residual=parsed.residual,
                limit=target,
            )
            merged.extend(items)
            exhausted = exhausted and kind_exhausted
        # Ids are per-resource, so a merged ordering is arbitrary; sort newest
        # first with the kind as a tiebreak so a window is at least stable.
        merged.sort(key=lambda entry: (-entry[0], entry[1]))
        items = [entry[2] for entry in merged]
        total = len(items) if exhausted else len(items) + 1
        return WorkItemPage(items=items[page.offset : target], total=total, page=page)

    async def get_item(self, key: str) -> WorkItem:
        kind, identifier = self._parse_key(key)
        spec = _RESOURCES[kind]
        payload = await self._request(
            f"{_BASE_PATH}/{spec.resource}/{identifier}",
            not_found=f"work item {key!r} not found in jira_align",
        )
        if not isinstance(payload, dict):
            raise RuntimeError("jira_align work item response must be a JSON object")
        self._require_item_in_program(key, kind, payload)
        return self._work_item(kind, payload)

    async def list_items(self, filters: WorkItemFilters, *, page: Page) -> WorkItemPage:
        # An explicit kind filter must fail loudly rather than be dropped by
        # the renderer: silently falling back to the default story+defect
        # fan-out would answer a different question than the caller asked.
        kind = filters.kind.strip().casefold() if filters.kind else None
        if kind is not None and kind not in _RESOURCES:
            raise ValueError(f"jira_align does not expose work items of kind {filters.kind!r}")
        spec = WorkQuerySpec(
            project=self._program_id,
            statuses=(filters.status,) if filters.status else (),
            kinds=(kind,) if kind else (),
            phrases=(filters.text,) if filters.text else (),
        )
        query = TranslatedQuery(provider=PROVIDER, query=render_align_query(spec))
        return await self.execute_query(query, page=page)

    async def create_item(self, draft: WorkItemDraft) -> WorkItem:
        raise WorkTrackingMutationRejectedError(_READ_ONLY)

    async def enrich_item(self, key: str, enrichment: Enrichment) -> WorkItem:
        raise WorkTrackingMutationRejectedError(_READ_ONLY)

    # ── reading ───────────────────────────────────────────────────────────────

    async def _read_resource(
        self,
        kind: str,
        *,
        program_id: str | None,
        residual: str,
        limit: int,
    ) -> tuple[list[tuple[int, str, WorkItem]], bool]:
        """Page one resource up to `limit` items; also report whether it ended."""

        spec = _RESOURCES.get(kind)
        if spec is None:
            raise ValueError(f"jira_align does not expose work items of kind {kind!r}")
        query_filter = self._resource_filter(kind, program_id=program_id, residual=residual)
        collected: list[tuple[int, str, WorkItem]] = []
        seen: set[int] = set()
        exhausted = False
        while len(collected) < limit:
            requested = min(_MAX_RESULTS_PER_REQUEST, limit - len(collected))
            params: dict[str, Any] = {
                "$top": requested,
                "$skip": len(collected),
                "$orderby": "id desc",
            }
            if query_filter:
                params["$filter"] = query_filter
            rows = _item_rows(await self._request(f"{_BASE_PATH}/{spec.resource}", params=params))
            if len(rows) > requested:
                raise RuntimeError("jira_align response exceeded the requested page-size budget")
            for row in rows:
                identifier = _identifier(row, "id")
                if identifier is None:
                    raise RuntimeError("jira_align response contains an item without an id")
                numeric = int(identifier)
                if numeric in seen:
                    raise RuntimeError("jira_align response contains a duplicate item id")
                seen.add(numeric)
                key = f"{kind}:{identifier}"
                # The $filter carries the program clause, but a search result is
                # still provider-controlled: prove the boundary per item.
                self._require_item_in_program(key, kind, row)
                collected.append((numeric, kind, self._work_item(kind, row)))
            if len(rows) < requested:
                exhausted = True
                break
        return collected, exhausted

    def _resource_filter(self, kind: str, *, program_id: str | None, residual: str) -> str | None:
        """Rewrite the canonical predicate into this resource's own dialect."""

        spec = _RESOURCES[kind]
        terms: list[str] = []
        if program_id is not None:
            terms.append(f"{spec.program_field} eq {program_id}")
        for term in _split_top_level_and(residual) if residual else []:
            status_term = _ITEMSTATUS_TERM.match(term)
            if status_term is None:
                if _contains_unquoted_word(term, "itemstatus"):
                    # The synthetic status term must never reach the wire, and
                    # rewriting it inside grouped expressions is not supported;
                    # refuse clearly instead of shipping a field the provider
                    # has never heard of and surfacing its opaque 400.
                    raise ValueError(
                        "jira_align status terms cannot appear inside grouped expressions"
                    )
                # A term with a top-level `or` must keep its own grouping, or
                # `A and B or C` re-associates as `(A and B) or C` and the
                # program clause stops bounding the whole predicate.
                grouped = _contains_unquoted_word(term, "or", top_level_only=True)
                terms.append(f"({term})" if grouped else term)
                continue
            statuses = tuple(
                status.strip()
                for status in status_term.group("statuses").split(",")
                if status.strip()
            )
            predicate = _state_predicate(kind, statuses)
            if predicate is None:
                # No state on this resource maps to the requested status, so
                # nothing here can match; keep it explicit rather than silently
                # widening the query to every item.
                return _MATCH_NONE
            terms.append(predicate)
        return " and ".join(terms) if terms else None

    def _parse_key(self, key: str) -> tuple[str, str]:
        match = _KEY.fullmatch(key)
        if match is None or match.group("kind") not in _RESOURCES:
            raise WorkTrackingMutationTargetNotFoundError(
                "work item key is not a valid jira_align key (expected '<type>:<id>')"
            )
        identifier = match.group("id")
        # Non-canonical ids ("story:013", "story:0") are rejected here as
        # not-found (404): the adapter re-derives keys from payload ids, so a
        # non-canonical request key could never equal the returned item's key
        # and would otherwise surface as a bogus upstream failure.
        if int(identifier) < 1 or identifier != str(int(identifier)):
            raise WorkTrackingMutationTargetNotFoundError(
                "work item key is not a valid jira_align key (expected '<type>:<id>')"
            )
        return match.group("kind"), identifier

    def _require_item_in_program(self, key: str, kind: str, payload: dict[str, Any]) -> None:
        """Enforce the connection's program boundary on a direct read."""

        if self._program_id is None:
            return
        program = _identifier(payload, _RESOURCES[kind].program_field)
        if program is not None and program != self._program_id:
            raise WorkTrackingMutationTargetNotFoundError(
                f"work item {key!r} not found in configured jira_align program"
            )

    # ── mapping ───────────────────────────────────────────────────────────────

    def _status(self, kind: str, payload: dict[str, Any]) -> str:
        raw_state = payload.get("state")
        if isinstance(raw_state, int) and not isinstance(raw_state, bool):
            return _RESOURCES[kind].states.get(raw_state) or f"state-{raw_state}"
        for key in ("state", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()[:255]
        return STATUS_OPEN

    def _item_url(self, payload: dict[str, Any]) -> str | None:
        """Use a link the payload supplies, never a guessed UI route."""

        for key in _URL_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                continue
            # The central validator's own acceptance, so a payload URL this
            # adapter keeps can never fail the whole item read downstream —
            # an unacceptable link is dropped, not fatal.
            if not safe_work_item_url(value):
                continue
            try:
                hostname = urlsplit(value).hostname
            except ValueError:
                continue
            if hostname is not None and hostname == urlsplit(self._base_url).hostname:
                return value
        return None

    def _parent_key(self, kind: str, payload: dict[str, Any]) -> str | None:
        spec = _RESOURCES[kind]
        if spec.parent_field is None or spec.parent_kind is None:
            return None
        parent = _identifier(payload, spec.parent_field)
        return f"{spec.parent_kind}:{parent}" if parent else None

    def _work_item(self, kind: str, payload: dict[str, Any]) -> WorkItem:
        identifier = _identifier(payload, "id")
        if identifier is None:
            raise RuntimeError("jira_align response contains an item without an id")
        title = _scalar_text(payload, ("title", "name"), limit=500) or f"{kind} {identifier}"
        item: WorkItem | None = None
        invalid_item = False
        try:
            item = WorkItem(
                key=f"{kind}:{identifier}",
                title=title,
                kind=kind,
                status=self._status(kind, payload),
                description=align_html_to_text(payload.get("description")),
                url=self._item_url(payload),
                priority=_scalar_text(payload, _PRIORITY_KEYS, limit=64),
                severity=_scalar_text(payload, _SEVERITY_KEYS, limit=64),
                sprint=_scalar_text(payload, _SPRINT_KEYS, limit=255),
                assignee=_scalar_text(payload, _ASSIGNEE_KEYS, limit=255),
                parent_key=self._parent_key(kind, payload),
            )
        except (ValidationError, ValueError):
            invalid_item = True
        if invalid_item or item is None:
            raise RuntimeError("jira_align returned an invalid work item")
        return item
