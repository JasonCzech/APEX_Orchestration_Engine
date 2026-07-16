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
    item_id: int,
    state: str = "Active",
    project: str = "Phoenix",
    *,
    tags: object | None = None,
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
            **({"System.Tags": tags} if tags is not None else {}),
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


@pytest.mark.parametrize(
    ("option", "value", "match"),
    [
        ("base_url", True, "base_url"),
        ("project", True, "project"),
    ],
)
def test_constructor_rejects_coercible_identity_options(
    option: str,
    value: object,
    match: str,
) -> None:
    options: dict[str, object] = {"base_url": BASE, "project": "Phoenix"}
    options[option] = value
    conn = ConnectionConfig(
        id="ado-coercible-option",
        kind=PortKind.WORK_TRACKING,
        provider="ado",
        name="bad",
        options=options,
    )

    with pytest.raises(ValueError, match=match):
        AdoWorkTrackingAdapter(conn, SecretValue(value="ado-pat-secret"))


@pytest.mark.parametrize("pat", ["unsafe\r\npat", "p" * 16_385])
def test_constructor_rejects_unsafe_or_oversized_pat_without_reflection(pat: str) -> None:
    conn = ConnectionConfig(
        id="ado-credential-boundary",
        kind=PortKind.WORK_TRACKING,
        provider="ado",
        name="ADO",
        options={"base_url": BASE, "project": "Phoenix"},
    )

    with pytest.raises(ValueError) as error:
        AdoWorkTrackingAdapter(conn, SecretValue(value=pat))

    assert pat not in str(error.value)


@pytest.mark.parametrize(
    "project",
    [
        ".",
        "..",
        "../Apollo",
        "Phoenix/Apollo",
        "Phoenix\\Apollo",
        "Phoenix?x=Apollo",
        "Phoenix#Apollo",
        "Phoenix%2FApollo",
        "Phoenix%5cApollo",
        "%2e%2e%2fApollo",
    ],
)
@respx.mock
def test_constructor_rejects_project_route_escape_before_authenticated_io(
    project: str,
) -> None:
    conn = ConnectionConfig(
        id="ado-bad-project",
        kind=PortKind.WORK_TRACKING,
        provider="ado",
        name="bad",
        options={"base_url": BASE, "project": project},
    )

    with pytest.raises(ValueError, match="unsafe path characters"):
        AdoWorkTrackingAdapter(conn, SecretValue(value="t"))

    assert respx.calls.call_count == 0


@respx.mock
async def test_project_name_is_one_encoded_route_segment() -> None:
    conn = ConnectionConfig(
        id="ado-spaced-project",
        kind=PortKind.WORK_TRACKING,
        provider="ado",
        name="spaced",
        options={"base_url": BASE, "project": "Phoenix Team"},
    )
    adapter = AdoWorkTrackingAdapter(conn, SecretValue(value="ado-pat-secret"))
    route = respx.post(f"{BASE}/Phoenix%20Team/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response())
    )

    await adapter.execute_query(
        TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems"),
        page=Page(),
    )

    assert route.called


def test_html_to_text_strips_tags_and_entities() -> None:
    assert html_to_text("<div>Watch <b>RSS</b> growth &amp; restarts</div>") == (
        "Watch RSS growth & restarts"
    )
    assert html_to_text(None) == ""
    assert html_to_text("plain") == "plain"


def test_html_to_text_bounds_provider_description() -> None:
    from apex.domain.input_limits import MAX_DESCRIPTION_CHARS

    description = html_to_text("<p>" + "x" * (MAX_DESCRIPTION_CHARS + 100) + "</p>")

    assert len(description) == MAX_DESCRIPTION_CHARS
    assert description.endswith("…")


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
    assert wiql_route.calls[0].request.url.params["$top"] == "4"
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


@pytest.mark.parametrize("work_items", [None, {}, "", False])
@respx.mock
async def test_execute_query_requires_explicit_work_items_list(work_items: object) -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": work_items})
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")

    with pytest.raises(RuntimeError, match="workItems.*must be a list"):
        await make_adapter().execute_query(query, page=Page())


@pytest.mark.parametrize("item_id", [True, 42.0, "42", 0, 2_147_483_648])
@respx.mock
async def test_execute_query_requires_exact_bounded_integer_ids(item_id: object) -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": item_id}]})
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")

    with pytest.raises(RuntimeError, match="no valid id"):
        await make_adapter().execute_query(query, page=Page())


@respx.mock
async def test_execute_query_rejects_duplicate_wiql_ids_before_batch() -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response(42, 42))
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")

    with pytest.raises(RuntimeError, match="duplicate id"):
        await make_adapter().execute_query(query, page=Page())

    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([work_item_row(99)], "unexpected id"),
        ([work_item_row(42), work_item_row(42)], "duplicate id"),
    ],
)
@respx.mock
async def test_execute_query_rejects_unexpected_or_duplicate_batch_ids(
    rows: list[dict[str, object]], message: str
) -> None:
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=wiql_response(42, 43))
    )
    respx.get(f"{BASE}/_apis/wit/workitems").mock(
        return_value=httpx.Response(200, json={"value": rows})
    )
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().execute_query(query, page=Page(limit=2))


async def test_execute_query_rejects_provider_mismatch() -> None:
    query = TranslatedQuery(provider="stub", query="SELECT [System.Id] FROM WorkItems")
    with pytest.raises(ValueError, match="provider"):
        await make_adapter().execute_query(query, page=Page())


@respx.mock
async def test_execute_query_rejects_unbounded_provider_window_before_http() -> None:
    query = TranslatedQuery(provider="ado", query="SELECT [System.Id] FROM WorkItems")

    with pytest.raises(ValueError, match="window"):
        await make_adapter().execute_query(query, page=Page(offset=801, limit=200))

    assert respx.calls == []


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
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
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
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/999").mock(
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
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
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
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
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


@pytest.mark.parametrize(
    "kind",
    [
        ".",
        "..",
        "Bug/../../_apis/projects",
        "Bug?api-version=7.1",
        "Bug#fragment",
        r"Bug\..\_apis\projects",
        "Bug%2f..%2f_apis%2fprojects",
        "Bug%2F..%2F_apis%2Fprojects",
        "Bug%5c..%5c_apis%5cprojects",
        "%2e%2e%2f_apis%2fprojects",
    ],
)
@respx.mock
async def test_create_item_rejects_route_escape_before_authenticated_io(kind: str) -> None:
    adapter = make_adapter()
    draft = WorkItemDraft(title="unsafe route", kind=kind)

    with pytest.raises(ValueError, match="unsafe path characters"):
        await adapter.create_item(draft)
    with pytest.raises(ValueError, match="unsafe path characters"):
        adapter.validate_create_item_idempotent(
            draft,
            marker="apex-idem-0123456789abcdef0123456789abcdef",
        )

    assert respx.calls.call_count == 0


@respx.mock
async def test_custom_work_item_type_is_one_encoded_route_segment() -> None:
    route = respx.post(f"{BASE}/Phoenix/_apis/wit/workitems/$Risk%20item").mock(
        return_value=httpx.Response(200, json=work_item_row(79, state="New"))
    )

    item = await make_adapter().create_item(WorkItemDraft(title="custom type", kind="risk item"))

    assert item.key == "79"
    assert route.called
    assert_all_calls_authed()


@respx.mock
async def test_find_idempotency_marker_fails_closed_when_candidate_budget_is_saturated() -> None:
    from apex.adapters.ado.work_tracking import _IDEMPOTENCY_CANDIDATE_LIMIT

    route = respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(
            200,
            json={
                "workItems": [
                    {"id": item_id} for item_id in range(1, _IDEMPOTENCY_CANDIDATE_LIMIT + 2)
                ]
            },
        )
    )

    with pytest.raises(RuntimeError, match="candidate budget"):
        await make_adapter().find_item_by_idempotency_marker(
            "apex-idem-0123456789abcdef0123456789abcdef"
        )

    request = route.calls[0].request
    assert request.url.params["$top"] == str(_IDEMPOTENCY_CANDIDATE_LIMIT + 1)
    assert request.url.params["api-version"] == "7.1"
    body = json.loads(request.content)
    assert "System.Tags" in body["query"]
    assert_all_calls_authed()


@respx.mock
async def test_find_idempotency_marker_checks_past_substring_only_candidates() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(
            200,
            json={"workItems": [{"id": 71}, {"id": 72}, {"id": 73}]},
        )
    )
    tags_by_id = {
        71: f"prefix-{marker}",
        72: f"{marker}-suffix",
        73: marker,
    }
    for item_id, tags in tags_by_id.items():
        respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/{item_id}").mock(
            return_value=httpx.Response(200, json=work_item_row(item_id, tags=tags))
        )

    item = await make_adapter().find_item_by_idempotency_marker(marker)

    assert item is not None
    assert item.key == "73"
    assert_all_calls_authed()


@pytest.mark.parametrize(
    ("tags", "expected_key"),
    [
        ("performance; apex-idem-0123456789abcdef0123456789abcdef", "71"),
        ("prefix-apex-idem-0123456789abcdef0123456789abcdef-suffix", None),
    ],
)
@respx.mock
async def test_find_idempotency_marker_requires_an_exact_hydrated_tag(
    tags: str,
    expected_key: str | None,
) -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 71}]})
    )
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/71").mock(
        return_value=httpx.Response(200, json=work_item_row(71, tags=tags))
    )

    item = await make_adapter().find_item_by_idempotency_marker(marker)

    assert (item.key if item is not None else None) == expected_key
    assert_all_calls_authed()


@respx.mock
async def test_find_idempotency_marker_rejects_multiple_exact_hydrated_tags() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.post(f"{BASE}/Phoenix/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 71}, {"id": 72}]})
    )
    for item_id in (71, 72):
        respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/{item_id}").mock(
            return_value=httpx.Response(200, json=work_item_row(item_id, tags=marker))
        )

    with pytest.raises(RuntimeError, match="multiple items"):
        await make_adapter().find_item_by_idempotency_marker(marker)


@respx.mock
async def test_idempotent_create_adds_provider_marker_tag() -> None:
    marker = "apex-idem-fedcba9876543210fedcba9876543210"
    route = respx.post(f"{BASE}/Phoenix/_apis/wit/workitems/$Task").mock(
        return_value=httpx.Response(
            200,
            json=work_item_row(78, state="New", tags=f"perf; {marker}"),
        )
    )

    await make_adapter().create_item_idempotent(
        WorkItemDraft(title="Marked", kind="task", fields={"System.Tags": "perf"}),
        marker=marker,
    )

    assert json.loads(route.calls[0].request.content)[-1] == {
        "op": "add",
        "path": "/fields/System.Tags",
        "value": f"perf; {marker}",
    }


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        (None, "did not acknowledge"),
        ("prefix-apex-idem-fedcba9876543210fedcba9876543210", "did not acknowledge"),
        (["apex-idem-fedcba9876543210fedcba9876543210"], "malformed tags"),
    ],
)
@respx.mock
async def test_idempotent_create_requires_exact_provider_marker_acknowledgement(
    tags: object | None,
    message: str,
) -> None:
    marker = "apex-idem-fedcba9876543210fedcba9876543210"
    respx.post(f"{BASE}/Phoenix/_apis/wit/workitems/$Task").mock(
        return_value=httpx.Response(200, json=work_item_row(78, state="New", tags=tags))
    )

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().create_item_idempotent(
            WorkItemDraft(title="Marked", kind="task"),
            marker=marker,
        )


async def test_create_item_rejects_unknown_bare_field() -> None:
    draft = WorkItemDraft(title="x", fields={"priority": 1})
    with pytest.raises(ValueError, match="reference name"):
        await make_adapter().create_item(draft)


@pytest.mark.parametrize(
    "field_name",
    [
        "System.Title/../../relations",
        "System.Title~1relations",
        "System..Title",
        ".System.Title",
    ],
)
@respx.mock
async def test_create_item_rejects_ambiguous_json_pointer_before_io(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="reference name"):
        await make_adapter().create_item(
            WorkItemDraft(title="unsafe pointer", fields={field_name: "value"})
        )

    assert respx.calls.call_count == 0


@respx.mock
async def test_idempotent_field_update_rejects_append_only_history_before_request() -> None:
    get_route = respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    patch_route = respx.patch(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )

    with pytest.raises(ValueError, match="System.History is append-only"):
        await make_adapter().update_item_fields_idempotent(
            "42", {"System.History": "duplicate-prone note"}
        )

    assert not get_route.called
    assert not patch_route.called


@respx.mock
async def test_idempotent_field_update_is_project_scoped_and_revision_fenced() -> None:
    row = work_item_row(42)
    row["rev"] = 17
    get_route = respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=row)
    )
    patch_route = respx.patch(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json={**row, "rev": 18})
    )

    await make_adapter().update_item_fields_idempotent(
        "42",
        {"System.Tags": "performance"},
    )

    assert get_route.called
    assert json.loads(patch_route.calls[0].request.content) == [
        {"op": "test", "path": "/rev", "value": 17},
        {"op": "add", "path": "/fields/System.Tags", "value": "performance"},
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"System.TeamProject": "Apollo"},
        {"System.AreaPath": "Apollo\\Payments"},
        {"System.IterationPath": "Apollo\\Sprint 1"},
        {"System.AreaId": 1234},
        {"System.IterationId": 5678},
    ],
)
@respx.mock
async def test_create_item_rejects_project_override(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="project|configured"):
        await make_adapter().create_item(WorkItemDraft(title="Wrong home", fields=fields))

    assert respx.calls.call_count == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"System.TeamProject": "Apollo"},
        {"System.AreaPath": "Apollo\\Payments"},
        {"System.IterationPath": "Apollo\\Sprint 1"},
        {"System.AreaId": 1234},
        {"System.IterationId": 5678},
    ],
)
@respx.mock
async def test_update_paths_reject_project_override_before_authenticated_io(
    fields: dict[str, object],
) -> None:
    adapter = make_adapter()

    with pytest.raises(ValueError, match="project|configured"):
        await adapter.enrich_item("42", Enrichment(fields=fields))
    with pytest.raises(ValueError, match="project|configured"):
        await adapter.update_item_fields_idempotent("42", fields)
    with pytest.raises(ValueError, match="project|configured"):
        adapter.validate_update_item_fields_idempotent(fields)

    assert respx.calls.call_count == 0


@respx.mock
async def test_enrich_item_patches_fields_and_posts_comment() -> None:
    patch_route = respx.patch(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    comment_route = respx.post(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        return_value=httpx.Response(200, json={"id": 1, "text": "load profile attached"})
    )
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
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
        {"op": "test", "path": "/rev", "value": 5},
        {"op": "add", "path": "/fields/System.State", "value": "Active"},
        {"op": "add", "path": "/fields/System.Tags", "value": "perf; checkout"},
    ]
    comment_request = comment_route.calls[0].request
    assert comment_request.url.params["api-version"] == "7.1-preview.3"
    assert json.loads(comment_request.content) == {"text": "load profile attached"}
    assert_all_calls_authed()


@respx.mock
async def test_enrich_item_checks_project_before_mutation() -> None:
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42, project="Apollo"))
    )
    patch_route = respx.patch(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )

    with pytest.raises(KeyError, match="configured azure devops project"):
        await make_adapter().enrich_item("42", Enrichment(fields={"status": "Active"}))

    assert not patch_route.called


# ── idempotent comment reconciliation ───────────────────────────────────────


@respx.mock
async def test_comment_marker_reconciliation_follows_bounded_continuation_pages() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    comments_route = respx.get(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"comments": [{"text": "ordinary note"}]},
                headers={"x-ms-continuationtoken": "page-2"},
            ),
            httpx.Response(
                200,
                json={
                    "value": [
                        {"text": f"completed\n[APEX-IDEMPOTENCY:{marker}]"},
                    ]
                },
            ),
        ]
    )

    found = await make_adapter().has_comment_idempotency_marker("42", marker)

    assert found is True
    assert comments_route.calls[0].request.url.params["$top"] == "100"
    assert "continuationToken" not in comments_route.calls[0].request.url.params
    assert comments_route.calls[1].request.url.params["continuationToken"] == "page-2"
    assert_all_calls_authed()


@respx.mock
async def test_comment_marker_reconciliation_returns_false_at_provider_end() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    respx.get(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        return_value=httpx.Response(200, json={"value": [{"text": "ordinary note"}]})
    )

    assert await make_adapter().has_comment_idempotency_marker("42", marker) is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "no comments list"),
        ({"comments": [None]}, "non-object row"),
        ({"comments": [{}] * 101}, "page-size budget"),
        (
            {"comments": [{"text": "note"}], "continuationToken": 17},
            "invalid comment continuation token",
        ),
        (
            {"comments": [{"text": "note"}], "continuationToken": "x" * 2_049},
            "invalid comment continuation token",
        ),
    ],
)
@respx.mock
async def test_comment_marker_reconciliation_rejects_unbounded_provider_shapes(
    payload: dict[str, object],
    message: str,
) -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    respx.get(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().has_comment_idempotency_marker("42", marker)


@respx.mock
async def test_add_comment_idempotent_checks_scope_and_writes_durable_marker() -> None:
    marker = "apex-idem-fedcba9876543210fedcba9876543210"
    respx.get(f"{BASE}/Phoenix/_apis/wit/workitems/42").mock(
        return_value=httpx.Response(200, json=work_item_row(42))
    )
    comment_route = respx.post(f"{BASE}/Phoenix/_apis/wit/workItems/42/comments").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )

    await make_adapter().add_item_comment_idempotent("42", "analysis complete", marker=marker)

    assert json.loads(comment_route.calls[0].request.content) == {
        "text": f"analysis complete\n\n[APEX-IDEMPOTENCY:{marker}]"
    }
    assert_all_calls_authed()


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
