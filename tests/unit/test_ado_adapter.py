"""Azure DevOps adapter against respx-recorded REST 7.1 wire fixtures."""

import base64
import json

import httpx
import pytest
import respx

from apex.adapters.ado.work_tracking import (
    AdoWorkTrackingAdapter,
    html_to_text,
    render_wiql,
)
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    SecretValue,
    TranslatedQuery,
    WorkItemDraft,
    WorkItemFilters,
)

BASE = "https://dev.azure.com/acme"
EXPECTED_AUTH = "Basic " + base64.b64encode(b":ado-pat-secret").decode()


def make_adapter() -> AdoWorkTrackingAdapter:
    conn = ConnectionConfig(
        id="ado-acme",
        kind=PortKind.WORK_TRACKING,
        provider="ado",
        name="Acme ADO",
        options={"base_url": BASE, "project": "Phoenix"},
    )
    return AdoWorkTrackingAdapter(conn, SecretValue(value="ado-pat-secret"))


def assert_all_calls_authed() -> None:
    """Every mocked exchange must carry the basic :PAT header."""
    assert respx.calls, "expected at least one mocked call"
    for call in respx.calls:
        assert call.request.headers["Authorization"] == EXPECTED_AUTH


def work_item_row(
    item_id: int, state: str = "Active", project: str = "Phoenix"
) -> dict[str, object]:
    return {
        "id": item_id,
        "rev": 5,
        "url": f"{BASE}/_apis/wit/workItems/{item_id}",
        "fields": {
            "System.Id": item_id,
            "System.Title": f"Work item {item_id}",
            "System.State": state,
            "System.WorkItemType": "User Story",
            "System.TeamProject": project,
            "System.Description": "<div>Watch <b>RSS</b> growth &amp; restarts</div>",
        },
    }


def wiql_response(*ids: int) -> dict[str, object]:
    return {
        "queryType": "flat",
        "queryResultType": "workItem",
        "asOf": "2026-06-11T09:00:00.000Z",
        "columns": [
            {
                "referenceName": "System.Id",
                "name": "ID",
                "url": f"{BASE}/_apis/wit/fields/System.Id",
            }
        ],
        "workItems": [{"id": i, "url": f"{BASE}/_apis/wit/workItems/{i}"} for i in ids],
    }


# ── construction / helpers ────────────────────────────────────────────────────


def test_constructor_validates_options_and_secret() -> None:
    conn = ConnectionConfig(
        id="ado-bad", kind=PortKind.WORK_TRACKING, provider="ado", name="bad", options={}
    )
    with pytest.raises(ValueError, match="base_url"):
        AdoWorkTrackingAdapter(conn, SecretValue(value="t"))
    conn.options = {"base_url": BASE}
    with pytest.raises(ValueError, match="project"):
        AdoWorkTrackingAdapter(conn, SecretValue(value="t"))
    conn.options = {"base_url": BASE, "project": "Phoenix"}
    with pytest.raises(ValueError, match="Personal Access Token"):
        AdoWorkTrackingAdapter(conn, None)


def test_html_to_text_strips_tags_and_entities() -> None:
    assert html_to_text("<div>Watch <b>RSS</b> growth &amp; restarts</div>") == (
        "Watch RSS growth & restarts"
    )
    assert html_to_text(None) == ""
    assert html_to_text("plain") == "plain"


# ── execute_query (WIQL ids + batch hydration, client-side paging) ────────────


@respx.mock
async def test_execute_query_pages_id_list_client_side() -> None:
    wiql_route = respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response(42, 43, 44))
    )
    batch_route = respx.get(f"{BASE}/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200, json={"count": 2, "value": [work_item_row(43), work_item_row(44, "Closed")]}
        )
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")
    result = await make_adapter().execute_query(query, page=Page(offset=1, limit=2))

    assert json.loads(wiql_route.calls[0].request.content) == {
        "query": "SELECT [System.Id] FROM WorkItems"
    }
    assert wiql_route.calls[0].request.url.params["api-version"] == "7.1"
    params = batch_route.calls[0].request.url.params
    assert params["ids"] == "43,44"  # offset=1 skipped id 42
    assert "System.Title" in params["fields"]
    assert result.total == 3  # full WIQL id list length
    assert [item.key for item in result.items] == ["43", "44"]
    first = result.items[0]
    assert first.title == "Work item 43"
    assert first.kind == "story"  # "User Story" normalized
    assert first.status == "in_progress"  # Active
    assert first.description == "Watch RSS growth & restarts"
    assert first.url == f"{BASE}/Phoenix/_workitems/edit/43"
    assert result.items[1].status == "closed"
    assert_all_calls_authed()


@respx.mock
async def test_execute_query_empty_window_skips_batch_call() -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response(42))
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")
    result = await make_adapter().execute_query(query, page=Page(offset=5, limit=10))
    assert result.items == []
    assert result.total == 1
    assert len(respx.calls) == 1  # no workitems batch GET


async def test_execute_query_rejects_provider_mismatch() -> None:
    query = TranslatedQuery(provider="stub", query="SELECT [System.Id] FROM WorkItems")
    with pytest.raises(ValueError, match="provider"):
        await make_adapter().execute_query(query, page=Page())


@respx.mock
async def test_execute_query_invalid_wiql_raises_value_error() -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(
            400,
            json={
                "$id": "1",
                "message": "TF51005: The query references a field that does not exist.",
                "typeName": "Microsoft.TeamFoundation.WorkItemTracking.Server.ValidationException",
            },
        )
    )
    query = TranslatedQuery(provider="ado", query="SELECT [Bogus] FROM WorkItems")
    with pytest.raises(ValueError, match="TF51005"):
        await make_adapter().execute_query(query, page=Page())


# ── get_item ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_get_item_maps_fields() -> None:
    respx.get(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42, state="New"))
    )
    item = await make_adapter().get_item("42")
    assert item.key == "42"
    assert item.status == "open"  # New
    assert item.kind == "story"
    assert item.url == f"{BASE}/Phoenix/_workitems/edit/42"
    assert_all_calls_authed()


@respx.mock
async def test_get_item_404_raises_key_error() -> None:
    respx.get(f"{BASE}/_apis/wit/workitems/999").mock(
        return_value=httpx.Response(
            404,
            json={
                "$id": "1",
                "innerException": None,
                "message": "TF401232: Work item 999 does not exist, or you do not have "
                "permissions to read it.",
                "typeName": "Microsoft.TeamFoundation.WorkItemTracking.Server."
                "WorkItemUnauthorizedAccessException",
            },
        )
    )
    with pytest.raises(KeyError, match="999"):
        await make_adapter().get_item("999")


@respx.mock
async def test_get_item_hides_item_from_another_project() -> None:
    respx.get(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42, project="Apollo"))
    )
    with pytest.raises(KeyError, match="configured azure devops project"):
        await make_adapter().get_item("42")


@respx.mock
async def test_get_item_rejects_malformed_id_without_http() -> None:
    with pytest.raises(KeyError, match="valid azure devops work item id"):
        await make_adapter().get_item("../wiql")
    assert respx.calls == []


@respx.mock
async def test_unauthorized_is_actionable_runtime_error() -> None:
    # ADO answers bad PATs on API routes with a 203 + sign-in page.
    respx.get(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(203, text="<html>Sign in</html>")
    )
    with pytest.raises(RuntimeError, match="PAT"):
        await make_adapter().get_item("42")


# ── list / create / enrich ────────────────────────────────────────────────────


@respx.mock
async def test_list_items_builds_wiql_from_filters() -> None:
    wiql_route = respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response())
    )
    await make_adapter().list_items(
        WorkItemFilters(status="open", kind="bug", text="cart"), page=Page()
    )
    body = json.loads(wiql_route.calls[0].request.content)
    assert body["query"] == (
        "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'Phoenix' "
        "AND [System.State] IN ('New', 'To Do', 'Proposed', 'Approved') "
        "AND [System.WorkItemType] IN ('Bug') AND [System.Title] CONTAINS 'cart' "
        "ORDER BY [System.ChangedDate] DESC"
    )


@respx.mock
async def test_create_item_posts_json_patch() -> None:
    route = respx.post(f"{BASE}/Phoenix/_apis/wit/workitems/$Bug").mock(
        return_value=httpx.Response(200, json=work_item_row(77, state="New"))
    )
    draft = WorkItemDraft(
        title="Cart soak failure",
        kind="bug",
        description="RSS climbs steadily",
        fields={"Microsoft.VSTS.Common.Priority": 1},
    )
    item = await make_adapter().create_item(draft)
    assert item.key == "77"
    request = route.calls[0].request
    assert request.headers["Content-Type"] == "application/json-patch+json"
    assert request.url.params["api-version"] == "7.1"
    assert json.loads(request.content) == [
        {"op": "add", "path": "/fields/System.Title", "value": "Cart soak failure"},
        {"op": "add", "path": "/fields/System.Description", "value": "RSS climbs steadily"},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": 1},
    ]
    assert_all_calls_authed()


async def test_create_item_rejects_unknown_bare_field() -> None:
    draft = WorkItemDraft(title="x", fields={"priority": 1})
    with pytest.raises(ValueError, match="reference name"):
        await make_adapter().create_item(draft)


@pytest.mark.parametrize(
    "fields",
    [
        {"System.TeamProject": "Apollo"},
        {"System.AreaPath": "Apollo\\Payments"},
        {"System.IterationPath": "Apollo\\Sprint 1"},
    ],
)
async def test_create_item_rejects_project_override(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="project|configured"):
        await make_adapter().create_item(WorkItemDraft(title="Wrong home", fields=fields))


@respx.mock
async def test_enrich_item_patches_fields_and_posts_comment() -> None:
    patch_route = respx.patch(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    comment_route = respx.post(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        return_value=httpx.Response(200, json={"id": 1, "text": "load profile attached"})
    )
    respx.get(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42, state="Active"))
    )
    enrichment = Enrichment(
        fields={"status": "Active", "System.Tags": "perf; checkout"},
        comment="load profile attached",
    )
    item = await make_adapter().enrich_item("42", enrichment)
    assert item.status == "in_progress"
    patch_request = patch_route.calls[0].request
    assert patch_request.headers["Content-Type"] == "application/json-patch+json"
    assert json.loads(patch_request.content) == [
        {"op": "add", "path": "/fields/System.State", "value": "Active"},
        {"op": "add", "path": "/fields/System.Tags", "value": "perf; checkout"},
    ]
    comment_request = comment_route.calls[0].request
    assert comment_request.url.params["api-version"] == "7.1-preview.3"
    assert json.loads(comment_request.content) == {"text": "load profile attached"}
    assert_all_calls_authed()


@respx.mock
async def test_enrich_item_checks_project_before_mutation() -> None:
    respx.get(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42, project="Apollo"))
    )
    patch_route = respx.patch(f"{BASE}/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )

    with pytest.raises(KeyError, match="configured azure devops project"):
        await make_adapter().enrich_item("42", Enrichment(fields={"status": "Active"}))

    assert not patch_route.called


# ── translate_query (deterministic ruleset, WIQL rendering) ───────────────────

_PREFIX = "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'Phoenix' AND "
_SUFFIX = " ORDER BY [System.ChangedDate] DESC"


@pytest.mark.parametrize(
    ("text", "expected_wiql", "expected_confidence"),
    [
        (
            "open bugs",
            _PREFIX + "[System.State] IN ('New', 'To Do', 'Proposed', 'Approved') "
            "AND [System.WorkItemType] IN ('Bug')" + _SUFFIX,
            0.6,
        ),
        (
            "my tasks",
            _PREFIX + "[System.WorkItemType] IN ('Task') AND [System.AssignedTo] = @Me" + _SUFFIX,
            0.6,
        ),
        (
            "stories in the current sprint",
            _PREFIX + "[System.WorkItemType] IN ('User Story') "
            "AND [System.IterationPath] = @CurrentIteration" + _SUFFIX,
            0.6,
        ),
        (
            "tasks in sprint 42",
            _PREFIX + "[System.WorkItemType] IN ('Task') "
            "AND [System.IterationPath] UNDER 'Phoenix\\42'" + _SUFFIX,
            0.6,
        ),
        (
            'items mentioning "checkout latency"',
            _PREFIX + "[System.Title] CONTAINS 'checkout latency'" + _SUFFIX,
            0.45,
        ),
        (
            "closed last week",
            _PREFIX + "[System.State] IN ('Resolved', 'Closed', 'Done', 'Completed') "
            "AND [System.CreatedDate] >= @StartOfWeek('-1') "
            "AND [System.CreatedDate] < @StartOfWeek" + _SUFFIX,
            0.6,
        ),
        (
            "done bugs in project Apollo",
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = 'Apollo' "
            "AND [System.State] IN ('Resolved', 'Closed', 'Done', 'Completed') "
            "AND [System.WorkItemType] IN ('Bug')" + _SUFFIX,
            0.75,
        ),
        (
            "frobnicate the widgets",
            _PREFIX + "[System.Title] CONTAINS 'frobnicate the widgets'" + _SUFFIX,
            0.3,
        ),
        (
            "in progress stories created today",
            _PREFIX + "[System.State] IN ('Active', 'In Progress', 'Doing', 'Committed') "
            "AND [System.WorkItemType] IN ('User Story') "
            "AND [System.CreatedDate] >= @StartOfDay" + _SUFFIX,
            0.75,
        ),
    ],
)
async def test_translate_query_ruleset(
    text: str, expected_wiql: str, expected_confidence: float
) -> None:
    translated = await make_adapter().translate_query(text, context=QueryContext())
    assert translated.provider == "ado"
    assert translated.query == expected_wiql
    assert translated.confidence == pytest.approx(expected_confidence)


async def test_translate_query_confidence_ordering() -> None:
    adapter = make_adapter()
    context = QueryContext()
    rich = await adapter.translate_query("my open bugs from this week", context=context)
    medium = await adapter.translate_query("open bugs", context=context)
    poor = await adapter.translate_query("synergize the roadmap", context=context)
    assert rich.confidence > medium.confidence > poor.confidence


def test_render_wiql_escapes_single_quotes() -> None:
    from apex.services.work_tracking import WorkQuerySpec

    spec = WorkQuerySpec(project="O'Brien", phrases=("can't repro",))
    wiql = render_wiql(spec)
    assert "[System.TeamProject] = 'O''Brien'" in wiql
    assert "[System.Title] CONTAINS 'can''t repro'" in wiql
