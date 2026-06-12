"""Jira Cloud adapter against respx-recorded REST v3 wire fixtures."""

import base64
import json

import httpx
import pytest
import respx

from apex.adapters.jira.work_tracking import (
    JiraWorkTrackingAdapter,
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


def issue_json(key: str, summary: str = "Issue summary") -> dict[str, object]:
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
        "fields": ["summary", "status", "issuetype", "description"],
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
        'project = PHX AND statusCategory = "To Do" AND issuetype in (Bug) '
        'AND text ~ "checkout" ORDER BY updated DESC'
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


async def test_create_item_without_project_is_value_error() -> None:
    adapter = make_adapter(project_key="")
    with pytest.raises(ValueError, match="project"):
        await adapter.create_item(WorkItemDraft(title="No home"))


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


# ── translate_query (deterministic ruleset) ───────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_jql", "expected_confidence"),
    [
        (
            "open bugs",
            'project = PHX AND statusCategory = "To Do" AND issuetype in (Bug) '
            "ORDER BY updated DESC",
            0.6,
        ),
        (
            "my open bugs",
            'project = PHX AND statusCategory = "To Do" AND issuetype in (Bug) '
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
            'project = APEX AND statusCategory = "Done" AND issuetype in (Bug) '
            "AND created >= startOfWeek() ORDER BY updated DESC",
            0.9,
        ),
        (
            "stories in the current sprint",
            "project = PHX AND issuetype in (Story) AND sprint in openSprints() "
            "ORDER BY updated DESC",
            0.6,
        ),
        (
            "tasks assigned to me in sprint 42",
            "project = PHX AND issuetype in (Task) AND assignee = currentUser() "
            'AND sprint = "42" ORDER BY updated DESC',
            0.75,
        ),
        (
            "in progress stories",
            'project = PHX AND statusCategory = "In Progress" AND issuetype in (Story) '
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
