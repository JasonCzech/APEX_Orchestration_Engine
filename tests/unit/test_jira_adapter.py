"""Jira Cloud adapter against respx-recorded REST v3 wire fixtures."""

import base64
import json

import httpx
import pytest
import respx

from apex.adapters.jira.work_tracking import (
    JiraWorkTrackingAdapter,
    _adf_contains_text,
    adf_to_text,
    render_jql,
    text_to_adf,
)
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import (
    Enrichment,
    Page,
    QueryContext,
    SecretValue,
    TranslatedQuery,
    WorkItemDraft,
)

BASE = "https://acme.atlassian.net"
EXPECTED_AUTH = "Basic " + base64.b64encode(b"qa@acme.test:jira-api-token").decode()


def make_adapter(**option_overrides: object) -> JiraWorkTrackingAdapter:
    options: dict[str, object] = {
        "base_url": BASE,
        "project_key": "PHX",
        "user_email": "qa@acme.test",
    }
    options.update(option_overrides)
    conn = ConnectionConfig(
        id="jira-acme",
        kind=PortKind.WORK_TRACKING,
        provider="jira",
        name="Acme Jira",
        options=options,
    )
    return JiraWorkTrackingAdapter(conn, SecretValue(value="jira-api-token"))


def assert_all_calls_authed() -> None:
    """Every mocked exchange must carry the basic email:token header."""
    assert respx.calls, "expected at least one mocked call"
    for call in respx.calls:
        assert call.request.headers["Authorization"] == EXPECTED_AUTH


def issue_json(
    key: str,
    summary: str = "Issue summary",
    *,
    labels: object | None = None,
) -> dict[str, object]:
    return {
        "id": "10241",
        "key": key,
        "self": f"{BASE}/rest/api/3/issue/10241",
        "fields": {
            "summary": summary,
            "status": {
                "name": "In Progress",
                "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"},
            },
            "issuetype": {"name": "Bug", "subtask": False},
            "project": {"key": key.rpartition("-")[0]},
            **({"labels": labels} if labels is not None else {}),
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "p95 rose from "},
                            {"type": "text", "text": "220ms", "marks": [{"type": "strong"}]},
                            {"type": "text", "text": " to 870ms."},
                        ],
                    },
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "Reproduce under load"}
                                        ],
                                    }
                                ],
                            },
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "Identify bottleneck"}
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        },
    }


def search_issue(key: str) -> dict[str, object]:
    return {
        "id": key.replace("PHX-", "1"),
        "key": key,
        "fields": {
            "summary": f"Summary for {key}",
            "status": {"name": "To Do", "statusCategory": {"key": "new", "name": "To Do"}},
            "issuetype": {"name": "Story"},
            "project": {"key": key.rpartition("-")[0]},
            "description": None,
        },
    }


# ── construction ──────────────────────────────────────────────────────────────


def test_constructor_validates_options_and_secret() -> None:
    conn = ConnectionConfig(
        id="jira-bad", kind=PortKind.WORK_TRACKING, provider="jira", name="bad", options={}
    )
    with pytest.raises(ValueError, match="base_url"):
        JiraWorkTrackingAdapter(conn, SecretValue(value="t"))
    conn.options = {"base_url": BASE}
    with pytest.raises(ValueError, match="user_email"):
        JiraWorkTrackingAdapter(conn, SecretValue(value="t"))
    conn.options = {"base_url": BASE, "user_email": "qa@acme.test"}
    with pytest.raises(ValueError, match="secret_ref"):
        JiraWorkTrackingAdapter(conn, None)


@pytest.mark.parametrize(
    ("option", "value", "match"),
    [
        ("base_url", True, "base_url"),
        ("user_email", True, "user_email"),
        ("user_email", "bad:user@example.test", "must not contain"),
        ("project_key", True, "project_key"),
        ("project_key", "P" * 256, "255"),
    ],
)
def test_constructor_rejects_coercible_or_unbounded_identity_options(
    option: str,
    value: object,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        make_adapter(**{option: value})


@pytest.mark.parametrize("token", ["unsafe\r\ntoken", "t" * 16_385])
def test_constructor_rejects_unsafe_or_oversized_token_without_reflection(token: str) -> None:
    conn = ConnectionConfig(
        id="jira-credential-boundary",
        kind=PortKind.WORK_TRACKING,
        provider="jira",
        name="Jira",
        options={
            "base_url": BASE,
            "project_key": "PHX",
            "user_email": "qa@acme.test",
        },
    )

    with pytest.raises(ValueError) as error:
        JiraWorkTrackingAdapter(conn, SecretValue(value=token))

    assert token not in str(error.value)


# ── ADF helpers ───────────────────────────────────────────────────────────────


def test_adf_to_text_flattens_blocks_and_marks() -> None:
    description = issue_json("PHX-241")["fields"]["description"]  # type: ignore[index]
    assert adf_to_text(description) == (
        "p95 rose from 220ms to 870ms.\nReproduce under load\n\nIdentify bottleneck"
    )


def test_adf_to_text_tolerates_non_adf_values() -> None:
    assert adf_to_text("plain string description") == "plain string description"
    assert adf_to_text(None) == ""
    assert adf_to_text({"type": "mention", "attrs": {"text": "@qa-bot"}}) == "@qa-bot"


def test_adf_to_text_is_iterative_and_bounded() -> None:
    from apex.domain.input_limits import MAX_DESCRIPTION_CHARS

    node: object = {"type": "text", "text": "x" * (MAX_DESCRIPTION_CHARS + 100)}
    for _ in range(5_000):
        node = {"type": "doc", "content": [node]}

    flattened = adf_to_text(node)

    assert len(flattened) == MAX_DESCRIPTION_CHARS
    assert flattened.endswith("…")


def test_adf_marker_search_is_iterative_and_matches_across_text_nodes() -> None:
    node: object = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "prefix [APEX-IDEM"},
            {"type": "text", "text": "POTENCY:marker] suffix"},
        ],
    }
    for _ in range(5_000):
        node = {"type": "doc", "content": [node]}

    assert _adf_contains_text(node, "[APEX-IDEMPOTENCY:marker]") is True
    assert _adf_contains_text(node, "missing-marker") is False


@pytest.mark.parametrize(
    ("node", "message"),
    [
        (17, "non-node value"),
        ({"type": 17}, "non-string node type"),
        ({"type": "text", "text": 17}, "non-string value"),
        ({"type": "mention", "attrs": [17]}, "malformed attrs"),
        ({"type": "emoji", "attrs": {"shortName": 17}}, "non-string value"),
        ({"type": "doc", "content": {}}, "malformed content"),
    ],
)
def test_adf_to_text_rejects_malformed_provider_nodes(node: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        adf_to_text(node)


def test_adf_to_text_rejects_provider_node_exhaustion() -> None:
    from apex.adapters.jira.work_tracking import _MAX_ADF_NODES

    with pytest.raises(ValueError, match="node limit"):
        adf_to_text([None] * (_MAX_ADF_NODES + 1))


def test_text_to_adf_round_trips_lines() -> None:
    doc = text_to_adf("line one\n\nline two")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert [len(p["content"]) for p in doc["content"]] == [1, 0, 1]
    assert adf_to_text(doc) == "line one\n\nline two"


# ── get_item ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_get_item_maps_issue_fields() -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241", "Checkout latency regression"))
    )
    item = await make_adapter().get_item("PHX-241")
    assert item.key == "PHX-241"
    assert item.title == "Checkout latency regression"
    assert item.kind == "bug"
    assert item.status == "in_progress"  # statusCategory indeterminate
    assert item.description.startswith("p95 rose from 220ms to 870ms.")
    assert item.url == f"{BASE}/browse/PHX-241"
    assert_all_calls_authed()


@respx.mock
async def test_get_item_404_raises_key_error() -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PHX-999").mock(
        return_value=httpx.Response(
            404,
            json={
                "errorMessages": ["Issue does not exist or you do not have permission to see it."],
                "errors": {},
            },
        )
    )
    with pytest.raises(KeyError, match="PHX-999"):
        await make_adapter().get_item("PHX-999")


@respx.mock
async def test_get_item_rejects_key_from_another_configured_project_without_http() -> None:
    with pytest.raises(KeyError, match="configured jira project"):
        await make_adapter().get_item("CAT-42")
    assert respx.calls == []


@respx.mock
@pytest.mark.parametrize(
    "key",
    [
        "../search",
        "A/B-1",
        "A\\B-1",
        "A?B-1",
        "A#B-1",
        "PHX-0",
        f"PHX-{'9' * 20}",
    ],
)
async def test_get_item_rejects_malformed_key_without_http(key: str) -> None:
    with pytest.raises(KeyError, match="valid jira issue key"):
        await make_adapter(project_key="").get_item(key)
    assert respx.calls == []


@respx.mock
async def test_get_item_rejects_issue_moved_to_another_project() -> None:
    moved = issue_json("PHX-241")
    moved["fields"]["project"] = {"key": "CAT"}  # type: ignore[index]
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(return_value=httpx.Response(200, json=moved))

    with pytest.raises(KeyError, match="configured jira project"):
        await make_adapter().get_item("PHX-241")


@respx.mock
async def test_unauthorized_is_actionable_runtime_error() -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(return_value=httpx.Response(401))
    with pytest.raises(RuntimeError, match="user_email"):
        await make_adapter().get_item("PHX-241")


# ── execute_query (token paging mapped onto offset) ───────────────────────────


@respx.mock
async def test_execute_query_follows_next_page_token() -> None:
    route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "issues": [search_issue("PHX-1"), search_issue("PHX-2")],
                    "nextPageToken": "tok-2",
                    "isLast": False,
                },
            ),
            httpx.Response(200, json={"issues": [search_issue("PHX-3")], "isLast": True}),
        ]
    )
    query = TranslatedQuery(provider="jira", query="project = PHX ORDER BY updated DESC")
    result = await make_adapter().execute_query(query, page=Page(offset=0, limit=3))
    assert [item.key for item in result.items] == ["PHX-1", "PHX-2", "PHX-3"]
    assert result.total == 3  # last page reached -> exact
    first_body = json.loads(route.calls[0].request.content)
    assert first_body == {
        "jql": "project = PHX ORDER BY updated DESC",
        "maxResults": 3,
        "fields": [
            "summary",
            "status",
            "issuetype",
            "description",
            "project",
        ],
    }
    second_body = json.loads(route.calls[1].request.content)
    assert second_body["nextPageToken"] == "tok-2"
    assert second_body["maxResults"] == 1
    assert_all_calls_authed()


@respx.mock
async def test_execute_query_offset_slices_fetched_window() -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "issues": [search_issue("PHX-1"), search_issue("PHX-2")],
                    "nextPageToken": "tok-2",
                    "isLast": False,
                },
            ),
            httpx.Response(200, json={"issues": [search_issue("PHX-3")], "isLast": True}),
        ]
    )
    query = TranslatedQuery(provider="jira", query="project = PHX ORDER BY updated DESC")
    result = await make_adapter().execute_query(query, page=Page(offset=1, limit=2))
    assert [item.key for item in result.items] == ["PHX-2", "PHX-3"]
    assert result.total == 3


@respx.mock
async def test_execute_query_total_is_lower_bound_when_more_pages_remain() -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [search_issue("PHX-1"), search_issue("PHX-2")],
                "nextPageToken": "tok-2",
                "isLast": False,
            },
        )
    )
    query = TranslatedQuery(provider="jira", query="project = PHX ORDER BY updated DESC")
    result = await make_adapter().execute_query(query, page=Page(offset=0, limit=2))
    assert len(result.items) == 2
    assert result.total == 3  # fetched + 1 signals "more available"


@pytest.mark.parametrize("issues", [None, {}, "", False])
@respx.mock
async def test_execute_query_requires_explicit_issues_list(issues: object) -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": issues, "isLast": True})
    )
    query = TranslatedQuery(provider="jira", query="project = PHX")

    with pytest.raises(RuntimeError, match="issues.*must be a list"):
        await make_adapter().execute_query(query, page=Page())


@pytest.mark.parametrize("is_last", ["false", 0, 1, None])
@respx.mock
async def test_execute_query_requires_boolean_is_last(is_last: object) -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={"issues": [], "nextPageToken": "next", "isLast": is_last},
        )
    )
    query = TranslatedQuery(provider="jira", query="project = PHX")

    with pytest.raises(RuntimeError, match="isLast.*boolean"):
        await make_adapter().execute_query(query, page=Page())


@pytest.mark.parametrize(
    "payload",
    [
        {"issues": [search_issue("PHX-1")], "isLast": False},
        {
            "issues": [search_issue("PHX-1")],
            "nextPageToken": "next",
            "isLast": True,
        },
    ],
)
@respx.mock
async def test_execute_query_rejects_inconsistent_pagination_fields(
    payload: dict[str, object],
) -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(return_value=httpx.Response(200, json=payload))
    query = TranslatedQuery(provider="jira", query="project = PHX")

    with pytest.raises(RuntimeError, match="inconsistent pagination"):
        await make_adapter().execute_query(query, page=Page())


@respx.mock
async def test_execute_query_rejects_duplicate_issue_keys() -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [search_issue("PHX-1"), search_issue("PHX-1")],
                "isLast": True,
            },
        )
    )
    query = TranslatedQuery(provider="jira", query="project = PHX")

    with pytest.raises(RuntimeError, match="duplicate issue key"):
        await make_adapter().execute_query(query, page=Page(limit=2))


@respx.mock
async def test_execute_query_invalid_jql_raises_value_error() -> None:
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            400,
            json={"errorMessages": ["Error in the JQL Query: Expecting operator but got 'zz'."]},
        )
    )
    query = TranslatedQuery(provider="jira", query="project zz PHX")
    with pytest.raises(ValueError, match="Expecting operator"):
        await make_adapter().execute_query(query, page=Page())


async def test_execute_query_rejects_provider_mismatch() -> None:
    query = TranslatedQuery(provider="stub", query="status = Open")
    with pytest.raises(ValueError, match="provider"):
        await make_adapter().execute_query(query, page=Page())


@respx.mock
async def test_execute_query_rejects_unbounded_provider_window_before_http() -> None:
    query = TranslatedQuery(provider="jira", query="project = PHX")

    with pytest.raises(ValueError, match="window"):
        await make_adapter().execute_query(query, page=Page(offset=801, limit=200))

    assert respx.calls == []


# ── list / create / enrich ────────────────────────────────────────────────────


@respx.mock
async def test_list_items_builds_jql_from_filters() -> None:
    from apex.domain.integrations import WorkItemFilters

    route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [], "isLast": True})
    )
    await make_adapter().list_items(
        WorkItemFilters(status="open", kind="bug", text="checkout"), page=Page()
    )
    body = json.loads(route.calls[0].request.content)
    assert body["jql"] == (
        'project = PHX AND statusCategory = "To Do" AND issuetype in ("Bug") '
        'AND text ~ "checkout" ORDER BY updated DESC'
    )
    assert_all_calls_authed()


@respx.mock
async def test_list_items_quotes_custom_kind_as_jql_data() -> None:
    from apex.domain.integrations import WorkItemFilters

    route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [], "isLast": True})
    )
    await make_adapter().list_items(
        WorkItemFilters(kind='bug) OR project = CAT OR issuetype in ("task'),
        page=Page(),
    )

    body = json.loads(route.calls[0].request.content)
    assert body["jql"] == (
        "project = PHX AND issuetype in "
        '("Bug) or project = cat or issuetype in (\\"task") '
        "ORDER BY updated DESC"
    )
    assert_all_calls_authed()


@respx.mock
async def test_create_item_posts_adf_description() -> None:
    route = respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(
            201,
            json={"id": "10500", "key": "PHX-900", "self": f"{BASE}/rest/api/3/issue/10500"},
        )
    )
    draft = WorkItemDraft(
        title="Soak the cart service",
        kind="bug",
        description="Steps to reproduce",
        fields={"labels": ["perf"]},
    )
    item = await make_adapter().create_item(draft)
    assert item.key == "PHX-900"
    assert item.url == f"{BASE}/browse/PHX-900"
    body = json.loads(route.calls[0].request.content)
    assert body["fields"]["project"] == {"key": "PHX"}
    assert body["fields"]["summary"] == "Soak the cart service"
    assert body["fields"]["issuetype"] == {"name": "Bug"}
    assert body["fields"]["labels"] == ["perf"]
    assert body["fields"]["description"] == {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Steps to reproduce"}]}
        ],
    }
    assert_all_calls_authed()


@respx.mock
async def test_idempotent_create_adds_marker_label_and_can_reconcile_it() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    create_route = respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "10501", "key": "PHX-901"})
    )
    get_route = respx.get(f"{BASE}/rest/api/3/issue/PHX-901").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-901", labels=[marker]))
    )
    search_route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={"issues": [issue_json("PHX-901", labels=[marker])]},
        )
    )

    created = await make_adapter().create_item_idempotent(
        WorkItemDraft(title="Marked issue", fields={"labels": ["perf"]}),
        marker=marker,
    )
    found = await make_adapter().find_item_by_idempotency_marker(marker)

    assert created.key == found.key == "PHX-901"  # type: ignore[union-attr]
    create_body = json.loads(create_route.calls[0].request.content)
    assert create_body["fields"]["labels"] == ["perf", marker]
    assert get_route.calls[0].request.url.params["fields"] == ",".join(
        ["summary", "status", "issuetype", "description", "project", "labels"]
    )
    search_body = json.loads(search_route.calls[0].request.content)
    assert search_body["maxResults"] == 2
    assert search_body["fields"] == [
        "summary",
        "status",
        "issuetype",
        "description",
        "project",
        "labels",
    ]
    assert marker in search_body["jql"]


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (None, "malformed labels"),
        ("apex-idem-0123456789abcdef0123456789abcdef", "malformed labels"),
        ([17], "malformed labels"),
        (["prefix-apex-idem-0123456789abcdef0123456789abcdef"], "without the exact"),
    ],
)
@respx.mock
async def test_idempotency_lookup_requires_exact_returned_marker_label(
    labels: object | None,
    message: str,
) -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    issue = issue_json("PHX-901", labels=labels)
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [issue]})
    )

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().find_item_by_idempotency_marker(marker)


@respx.mock
async def test_idempotency_lookup_quotes_configured_project_as_jql_data() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    project = 'PHX" OR project = OTHER OR project = "PHX'
    route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )

    assert await make_adapter(project_key=project).find_item_by_idempotency_marker(marker) is None

    body = json.loads(route.calls[0].request.content)
    assert body["jql"] == (
        f'project = "PHX\\" OR project = OTHER OR project = \\"PHX" AND labels = "{marker}"'
    )


@respx.mock
async def test_idempotent_create_reuses_case_variant_labels_field() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    route = respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "10501", "key": "PHX-901"})
    )
    respx.get(f"{BASE}/rest/api/3/issue/PHX-901").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-901", labels=[marker]))
    )

    await make_adapter().create_item_idempotent(
        WorkItemDraft(title="Marked issue", fields={"Labels": ["perf"]}),
        marker=marker,
    )

    fields = json.loads(route.calls[0].request.content)["fields"]
    assert fields["Labels"] == ["perf", marker]
    assert "labels" not in fields


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (None, "malformed labels"),
        (["prefix-apex-idem-0123456789abcdef0123456789abcdef"], "did not acknowledge"),
        ([17], "malformed labels"),
    ],
)
@respx.mock
async def test_idempotent_create_requires_exact_provider_marker_acknowledgement(
    labels: object | None,
    message: str,
) -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "10501", "key": "PHX-901"})
    )
    respx.get(f"{BASE}/rest/api/3/issue/PHX-901").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-901", labels=labels))
    )

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().create_item_idempotent(
            WorkItemDraft(title="Marked issue"),
            marker=marker,
        )


async def test_create_item_without_project_is_value_error() -> None:
    adapter = make_adapter(project_key="")
    with pytest.raises(ValueError, match="project"):
        await adapter.create_item(WorkItemDraft(title="No home"))


async def test_create_item_rejects_project_override() -> None:
    with pytest.raises(ValueError, match="fixed by the connection"):
        await make_adapter().create_item(
            WorkItemDraft(title="Wrong home", fields={"project": {"key": "CAT"}})
        )


@respx.mock
async def test_create_item_rejects_response_key_from_another_project() -> None:
    respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"id": "1", "key": "CAT-1"})
    )
    with pytest.raises(KeyError, match="configured jira project"):
        await make_adapter().create_item(WorkItemDraft(title="Wrong response"))


@respx.mock
async def test_enrich_item_comments_then_refetches() -> None:
    comment_route = respx.post(f"{BASE}/rest/api/3/issue/PHX-241/comment").mock(
        return_value=httpx.Response(201, json={"id": "20001"})
    )
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    item = await make_adapter().enrich_item("PHX-241", Enrichment(comment="repro attached"))
    assert item.key == "PHX-241"
    body = json.loads(comment_route.calls[0].request.content)
    assert body["body"]["content"][0]["content"][0]["text"] == "repro attached"
    assert_all_calls_authed()


@respx.mock
async def test_enrich_item_validates_current_project_before_mutation() -> None:
    moved = issue_json("PHX-241")
    moved["fields"]["project"] = {"key": "CAT"}  # type: ignore[index]
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(return_value=httpx.Response(200, json=moved))
    put_route = respx.put(f"{BASE}/rest/api/3/issue/PHX-241").mock(return_value=httpx.Response(204))

    with pytest.raises(KeyError, match="configured jira project"):
        await make_adapter().enrich_item(
            "PHX-241", Enrichment(fields={"description": "must not write"})
        )
    assert not put_route.called


@respx.mock
async def test_enrich_item_field_update_converts_description_to_adf() -> None:
    put_route = respx.put(f"{BASE}/rest/api/3/issue/PHX-241").mock(return_value=httpx.Response(204))
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    await make_adapter().enrich_item(
        "PHX-241", Enrichment(fields={"description": "new text", "labels": ["triaged"]})
    )
    body = json.loads(put_route.calls[0].request.content)
    assert body["fields"]["description"]["type"] == "doc"
    assert body["fields"]["labels"] == ["triaged"]
    assert_all_calls_authed()


async def test_enrich_item_rejects_status_field() -> None:
    with pytest.raises(ValueError, match="transitions"):
        await make_adapter().enrich_item("PHX-241", Enrichment(fields={"status": "Done"}))


async def test_enrich_item_rejects_project_change() -> None:
    with pytest.raises(ValueError, match="project cannot be changed"):
        await make_adapter().enrich_item("PHX-241", Enrichment(fields={"project": {"key": "CAT"}}))


# ── idempotent comment reconciliation ───────────────────────────────────────


@respx.mock
async def test_comment_marker_reconciliation_follows_bounded_offset_pages() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    comments_route = respx.get(f"{BASE}/rest/api/3/issue/PHX-241/comment").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "comments": [{"body": text_to_adf("ordinary note")}],
                    "total": 2,
                },
            ),
            httpx.Response(
                200,
                json={
                    "comments": [{"body": text_to_adf(f"completed\n[APEX-IDEMPOTENCY:{marker}]")}],
                    "total": 2,
                },
            ),
        ]
    )

    found = await make_adapter().has_comment_idempotency_marker("PHX-241", marker)

    assert found is True
    assert comments_route.calls[0].request.url.params["startAt"] == "0"
    assert comments_route.calls[0].request.url.params["maxResults"] == "100"
    assert comments_route.calls[1].request.url.params["startAt"] == "1"
    assert_all_calls_authed()


@respx.mock
async def test_comment_marker_reconciliation_returns_false_at_total() -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241/comment").mock(
        return_value=httpx.Response(
            200,
            json={"comments": [{"body": text_to_adf("ordinary note")}], "total": 1},
        )
    )

    assert await make_adapter().has_comment_idempotency_marker("PHX-241", marker) is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"comments": [{}] * 101, "total": 101}, "page-size budget"),
        ({"comments": [None], "total": 1}, "non-object comment"),
        ({"comments": [], "total": "0"}, "must be an integer"),
        ({"comments": [], "total": 10_001}, "outside the allowed range"),
    ],
)
@respx.mock
async def test_comment_marker_reconciliation_rejects_unbounded_provider_shapes(
    payload: dict[str, object],
    message: str,
) -> None:
    marker = "apex-idem-0123456789abcdef0123456789abcdef"
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241/comment").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(RuntimeError, match=message):
        await make_adapter().has_comment_idempotency_marker("PHX-241", marker)


@respx.mock
async def test_add_comment_idempotent_checks_scope_and_writes_adf_marker() -> None:
    marker = "apex-idem-fedcba9876543210fedcba9876543210"
    respx.get(f"{BASE}/rest/api/3/issue/PHX-241").mock(
        return_value=httpx.Response(200, json=issue_json("PHX-241"))
    )
    comment_route = respx.post(f"{BASE}/rest/api/3/issue/PHX-241/comment").mock(
        return_value=httpx.Response(201, json={"id": "9"})
    )

    await make_adapter().add_item_comment_idempotent("PHX-241", "analysis complete", marker=marker)

    body = json.loads(comment_route.calls[0].request.content)["body"]
    assert adf_to_text(body) == f"analysis complete\n\n[APEX-IDEMPOTENCY:{marker}]"
    assert_all_calls_authed()


# ── translate_query (deterministic ruleset) ───────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_jql", "expected_confidence"),
    [
        (
            "open bugs",
            'project = PHX AND statusCategory = "To Do" AND issuetype in ("Bug") '
            "ORDER BY updated DESC",
            0.6,
        ),
        (
            "my open bugs",
            'project = PHX AND statusCategory = "To Do" AND issuetype in ("Bug") '
            "AND assignee = currentUser() ORDER BY updated DESC",
            0.75,
        ),
        (
            'issues mentioning "checkout latency"',
            'project = PHX AND text ~ "checkout latency" ORDER BY updated DESC',
            0.45,
        ),
        (
            "bugs closed this week in project APEX",
            'project = APEX AND statusCategory = "Done" AND issuetype in ("Bug") '
            "AND created >= startOfWeek() ORDER BY updated DESC",
            0.9,
        ),
        (
            "stories in the current sprint",
            'project = PHX AND issuetype in ("Story") AND sprint in openSprints() '
            "ORDER BY updated DESC",
            0.6,
        ),
        (
            "tasks assigned to me in sprint 42",
            'project = PHX AND issuetype in ("Task") AND assignee = currentUser() '
            'AND sprint = "42" ORDER BY updated DESC',
            0.75,
        ),
        (
            "in progress stories",
            'project = PHX AND statusCategory = "In Progress" AND issuetype in ("Story") '
            "ORDER BY updated DESC",
            0.6,
        ),
        (
            "what changed last week",
            "project = PHX AND created >= startOfWeek(-1w) AND created < startOfWeek() "
            "ORDER BY updated DESC",
            0.45,
        ),
        (
            "frobnicate the widgets",
            'project = PHX AND text ~ "frobnicate the widgets" ORDER BY updated DESC',
            0.3,
        ),
    ],
)
async def test_translate_query_ruleset(
    text: str, expected_jql: str, expected_confidence: float
) -> None:
    translated = await make_adapter().translate_query(text, context=QueryContext())
    assert translated.provider == "jira"
    assert translated.query == expected_jql
    assert translated.confidence == pytest.approx(expected_confidence)


async def test_translate_query_confidence_ordering() -> None:
    adapter = make_adapter()
    context = QueryContext()
    rich = await adapter.translate_query("open bugs assigned to me this week", context=context)
    medium = await adapter.translate_query("open bugs", context=context)
    poor = await adapter.translate_query("synergize the roadmap", context=context)
    assert rich.confidence > medium.confidence > poor.confidence
    assert rich.confidence == pytest.approx(0.9)
    assert poor.confidence == pytest.approx(0.3)


async def test_translate_query_context_hint_overrides_default_project() -> None:
    context = QueryContext(hints={"project": "OPS"})
    translated = await make_adapter().translate_query("open bugs", context=context)
    assert translated.query.startswith("project = OPS AND ")


def test_render_jql_quotes_unsafe_project_and_phrase() -> None:
    from apex.services.work_tracking import WorkQuerySpec

    spec = WorkQuerySpec(project="my proj", phrases=('say "hi"',))
    jql = render_jql(spec)
    assert jql == 'project = "my proj" AND text ~ "say \\"hi\\"" ORDER BY updated DESC'
