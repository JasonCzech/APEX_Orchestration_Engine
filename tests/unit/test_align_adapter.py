"""Jira Align adapter against respx-recorded REST API 2.0 wire fixtures."""

import base64
from typing import Any

import httpx
import pytest
import respx

from apex.adapters.jira_align.work_tracking import (
    JiraAlignWorkTrackingAdapter,
    align_html_to_text,
    parse_align_query,
    render_align_query,
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
from apex.ports.work_tracking import (
    WorkTrackingMutationRejectedError,
    WorkTrackingMutationTargetNotFoundError,
)
from apex.services.work_items import validated_provider_work_item
from apex.services.work_tracking import WorkQuerySpec

BASE = "https://acme.jiraalign.com"
API = f"{BASE}/rest/align/api/2"
TOKEN = "align-api-token"
BEARER_AUTH = f"Bearer {TOKEN}"
BASIC_AUTH = "Basic " + base64.b64encode(f"qa@acme.test:{TOKEN}".encode()).decode()


def make_adapter(**option_overrides: object) -> JiraAlignWorkTrackingAdapter:
    options: dict[str, object] = {"base_url": BASE, "program_id": "42"}
    options.update(option_overrides)
    conn = ConnectionConfig(
        id="align-acme",
        kind=PortKind.WORK_TRACKING,
        provider="jira_align",
        name="Acme Jira Align",
        options=options,
    )
    return JiraAlignWorkTrackingAdapter(conn, SecretValue(value=TOKEN))


def assert_all_calls_authed(expected: str = BEARER_AUTH) -> None:
    assert respx.calls, "expected at least one mocked call"
    for call in respx.calls:
        assert call.request.headers["Authorization"] == expected


def story_json(identifier: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": identifier,
        "title": f"Story {identifier}",
        "description": "<p>Checkout p95 rose to 870ms.</p>",
        "state": 3,
        "programId": 42,
        "featureId": 7,
        "ownerName": "Jamie Rivera",
        "priority": "High",
        "sprintName": "PI 24.3 / S2",
    }
    payload.update(overrides)
    return payload


def defect_json(identifier: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": identifier,
        "title": f"Defect {identifier}",
        "description": "Intermittent 500 on submit",
        "state": 0,
        "programId": 42,
        "storyId": 13,
        "severity": "Sev-2",
    }
    payload.update(overrides)
    return payload


# ── construction ──────────────────────────────────────────────────────────────


def test_constructor_requires_base_url_and_secret() -> None:
    conn = ConnectionConfig(
        id="align-bad",
        kind=PortKind.WORK_TRACKING,
        provider="jira_align",
        name="bad",
        options={},
    )
    with pytest.raises(ValueError, match="base_url"):
        JiraAlignWorkTrackingAdapter(conn, SecretValue(value=TOKEN))
    conn.options = {"base_url": BASE}
    with pytest.raises(ValueError, match="secret_ref"):
        JiraAlignWorkTrackingAdapter(conn, None)


@pytest.mark.parametrize("program_id", ["forty-two", "4 2", "-1", True, 12.5, 7])
def test_constructor_rejects_a_non_string_or_non_numeric_program_id(program_id: object) -> None:
    """String-only: the scoped-connection validator requires a string, and an
    int accepted here would only fail later, when the row is project-scoped."""

    with pytest.raises(ValueError, match="numeric string"):
        make_adapter(program_id=program_id)


def test_program_id_is_the_upstream_project_boundary() -> None:
    assert make_adapter().project_id == "42"
    assert make_adapter(program_id="").project_id is None


@respx.mock
async def test_bearer_auth_is_used_without_a_configured_user_email() -> None:
    respx.get(f"{API}/Stories/13").mock(return_value=httpx.Response(200, json=story_json(13)))
    await make_adapter().get_item("story:13")
    assert_all_calls_authed(BEARER_AUTH)


@respx.mock
async def test_basic_auth_is_used_when_a_user_email_is_configured() -> None:
    """An Atlassian Guard instance authenticates with email:atlassian-token."""

    respx.get(f"{API}/Stories/13").mock(return_value=httpx.Response(200, json=story_json(13)))
    await make_adapter(user_email="qa@acme.test").get_item("story:13")
    assert_all_calls_authed(BASIC_AUTH)


def test_constructor_rejects_a_user_email_that_would_split_the_basic_credential() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        make_adapter(user_email="bad:user@acme.test")


# ── canonical query grammar ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_query", "expected_confidence"),
    [
        (
            "open defects",
            "programId eq 42 and itemtype in ('bug') and itemstatus in (open)",
            0.6,
        ),
        (
            "features in progress",
            "programId eq 42 and itemtype in ('feature') and itemstatus in (in_progress)",
            0.6,
        ),
        (
            "epics and capabilities",
            "programId eq 42 and itemtype in ('epic','capability')",
            0.45,
        ),
        (
            'stories about "checkout latency"',
            "programId eq 42 and itemtype in ('story') and contains(title,'checkout latency')",
            0.6,
        ),
        (
            "sprint 118 stories",
            "programId eq 42 and itemtype in ('story') and sprintId eq 118",
            0.6,
        ),
        (
            "synergize the roadmap",
            "programId eq 42 and contains(title,'synergize the roadmap')",
            0.3,
        ),
    ],
)
async def test_translate_query_ruleset(
    text: str, expected_query: str, expected_confidence: float
) -> None:
    translated = await make_adapter().translate_query(text, context=QueryContext())
    assert translated.provider == "jira_align"
    assert translated.query == expected_query
    assert translated.confidence == pytest.approx(expected_confidence)


async def test_translate_query_treats_features_as_a_type_not_a_story_synonym() -> None:
    """The shared default table folds "feature" into "story"; Align must not."""

    translated = await make_adapter().translate_query("open features", context=QueryContext())
    assert "itemtype in ('feature')" in translated.query
    assert "'story'" not in translated.query


async def test_translate_query_context_hint_overrides_the_configured_program() -> None:
    context = QueryContext(hints={"project": "77"})
    translated = await make_adapter().translate_query("open bugs", context=context)
    assert translated.query.startswith("programId eq 77 and ")


def test_render_rejects_a_non_numeric_program_scope() -> None:
    with pytest.raises(ValueError, match="numeric program id"):
        render_align_query(WorkQuerySpec(project="ACME"))


def test_render_escapes_a_quote_in_a_phrase() -> None:
    rendered = render_align_query(WorkQuerySpec(phrases=("say 'hi'",)))
    assert rendered == "contains(title,'say ''hi''')"


def test_render_emits_a_match_all_predicate_for_an_empty_spec() -> None:
    assert render_align_query(WorkQuerySpec()) == "id gt 0"


def test_parse_round_trips_a_rendered_query() -> None:
    rendered = render_align_query(
        WorkQuerySpec(project="42", kinds=("story", "bug"), statuses=("open",))
    )
    parsed = parse_align_query(rendered)
    assert parsed.program_id == "42"
    assert parsed.kinds == ("story", "bug")
    assert parsed.residual == "itemstatus in (open)"


def test_parse_finds_synthetic_terms_through_the_routers_scope_wrapper() -> None:
    """_constrain_align parenthesizes the caller predicate; parsing must descend."""

    parsed = parse_align_query("programId eq 42 and (itemtype in ('epic') and state eq 5)")
    assert parsed.program_id == "42"
    assert parsed.kinds == ("epic",)
    assert parsed.residual == "(state eq 5)"


def test_parse_ignores_and_inside_quotes_and_parentheses() -> None:
    assert parse_align_query("contains(title,'brand and expand')").residual == (
        "contains(title,'brand and expand')"
    )


def test_parse_never_hoists_a_term_out_of_an_or_group() -> None:
    """Flattening `(a or b) and c` into `c and a or b` would silently widen it."""

    parsed = parse_align_query("itemtype in ('story') and (state eq 1 or state eq 2) and id gt 5")
    assert parsed.kinds == ("story",)
    assert parsed.residual == "(state eq 1 or state eq 2) and id gt 5"


@pytest.mark.parametrize(
    ("query", "match"),
    [
        ("programId eq 1 and programId eq 2", "conflicting programs"),
        ("itemtype in ('roadmap')", "unknown type"),
        ("state eq 1)", "unbalanced"),
        ("contains(title,'unclosed", "unbalanced"),
    ],
)
def test_parse_rejects_malformed_canonical_queries(query: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_align_query(query)


# ── reading ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_get_item_maps_a_story() -> None:
    respx.get(f"{API}/Stories/13").mock(return_value=httpx.Response(200, json=story_json(13)))
    item = await make_adapter().get_item("story:13")
    assert item.key == "story:13"
    assert item.kind == "story"
    assert item.title == "Story 13"
    assert item.status == "in_progress"  # state 3 -> Testing
    assert item.description == "Checkout p95 rose to 870ms."
    assert item.assignee == "Jamie Rivera"
    assert item.priority == "High"
    assert item.sprint == "PI 24.3 / S2"
    assert item.parent_key == "feature:7"
    assert item.url is None  # no link in the payload; never a guessed route
    assert validated_provider_work_item(item) == item


@respx.mock
async def test_get_item_maps_a_defect_with_its_own_state_table() -> None:
    respx.get(f"{API}/Defects/481").mock(return_value=httpx.Response(200, json=defect_json(481)))
    item = await make_adapter().get_item("bug:481")
    assert (item.key, item.kind, item.status) == ("bug:481", "bug", "open")
    assert item.severity == "Sev-2"
    assert item.parent_key == "story:13"
    assert validated_provider_work_item(item) == item


@respx.mock
async def test_get_item_maps_portfolio_kinds() -> None:
    respx.get(f"{API}/Capabilities/9").mock(
        return_value=httpx.Response(
            200,
            json={"id": 9, "title": "Unified checkout", "state": 5, "primaryProgramId": 42},
        )
    )
    item = await make_adapter().get_item("capability:9")
    assert (item.key, item.kind, item.status) == ("capability:9", "capability", "closed")


@respx.mock
async def test_unknown_state_passes_through_instead_of_being_dropped() -> None:
    respx.get(f"{API}/Stories/13").mock(
        return_value=httpx.Response(200, json=story_json(13, state=99))
    )
    item = await make_adapter().get_item("story:13")
    assert item.status == "state-99"


@respx.mock
async def test_item_url_is_used_only_when_the_payload_supplies_one_on_the_instance() -> None:
    respx.get(f"{API}/Stories/13").mock(
        return_value=httpx.Response(200, json=story_json(13, url=f"{BASE}/story/13"))
    )
    assert (await make_adapter().get_item("story:13")).url == f"{BASE}/story/13"
    respx.get(f"{API}/Stories/14").mock(
        return_value=httpx.Response(200, json=story_json(14, url="https://evil.test/story/14"))
    )
    assert (await make_adapter().get_item("story:14")).url is None


@pytest.mark.parametrize(
    "key", ["13", "PHX-241", "story:", "roadmap:1", "story:0", "story:-1", "story:013"]
)
async def test_get_item_rejects_keys_that_are_not_align_keys(key: str) -> None:
    with pytest.raises(WorkTrackingMutationTargetNotFoundError):
        await make_adapter().get_item(key)


@respx.mock
async def test_get_item_enforces_the_configured_program_boundary() -> None:
    respx.get(f"{API}/Stories/13").mock(
        return_value=httpx.Response(200, json=story_json(13, programId=99))
    )
    with pytest.raises(WorkTrackingMutationTargetNotFoundError, match="configured jira_align"):
        await make_adapter().get_item("story:13")


@respx.mock
async def test_get_item_maps_a_404_to_a_target_not_found() -> None:
    respx.get(f"{API}/Stories/13").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(WorkTrackingMutationTargetNotFoundError, match="not found"):
        await make_adapter().get_item("story:13")


@respx.mock
async def test_rejected_credentials_name_the_connection_fields_to_check() -> None:
    respx.get(f"{API}/Stories/13").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(RuntimeError, match="user_email"):
        await make_adapter().get_item("story:13")


# ── query execution ───────────────────────────────────────────────────────────


@respx.mock
async def test_execute_query_fans_out_across_stories_and_defects_by_default() -> None:
    stories = respx.get(f"{API}/Stories").mock(
        return_value=httpx.Response(200, json=[story_json(13), story_json(11)])
    )
    defects = respx.get(f"{API}/Defects").mock(
        return_value=httpx.Response(200, json=[defect_json(12)])
    )
    query = TranslatedQuery(provider="jira_align", query="programId eq 42 and state eq 3")
    result = await make_adapter().execute_query(query, page=Page(offset=0, limit=10))

    assert [item.key for item in result.items] == ["story:13", "bug:12", "story:11"]
    assert result.total == 3  # every resource returned a short page -> exact
    story_params = stories.calls[0].request.url.params
    assert story_params["$filter"] == "programId eq 42 and state eq 3"
    assert story_params["$orderby"] == "id desc"
    assert (story_params["$top"], story_params["$skip"]) == ("10", "0")
    assert defects.calls[0].request.url.params["$filter"] == "programId eq 42 and state eq 3"
    assert_all_calls_authed()


@respx.mock
async def test_execute_query_renders_the_program_field_each_resource_uses() -> None:
    features = respx.get(f"{API}/Features").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(
        provider="jira_align", query="programId eq 42 and itemtype in ('feature')"
    )
    await make_adapter().execute_query(query, page=Page(limit=5))
    assert features.calls[0].request.url.params["$filter"] == "primaryProgramId eq 42"


@respx.mock
async def test_execute_query_expands_a_status_into_each_resources_state_numbers() -> None:
    stories = respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=[]))
    defects = respx.get(f"{API}/Defects").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(provider="jira_align", query="itemstatus in (closed)")
    await make_adapter(program_id="").execute_query(query, page=Page(limit=5))
    # Stories close at state 5; defects at state 1.
    assert stories.calls[0].request.url.params["$filter"] == "state eq 5"
    assert defects.calls[0].request.url.params["$filter"] == "state eq 1"


@respx.mock
async def test_a_status_no_state_maps_to_yields_an_empty_resource_not_everything() -> None:
    defects = respx.get(f"{API}/Defects").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(
        provider="jira_align", query="itemtype in ('bug') and itemstatus in (in_progress)"
    )
    result = await make_adapter(program_id="").execute_query(query, page=Page(limit=5))
    assert defects.calls[0].request.url.params["$filter"] == "id lt 0"
    assert result.items == []


@respx.mock
async def test_execute_query_slices_the_requested_window_out_of_the_prefix() -> None:
    """Offset is served by reading the prefix, like the Jira Cloud adapter."""

    respx.get(f"{API}/Stories").mock(
        return_value=httpx.Response(
            200, json=[story_json(identifier) for identifier in (20, 19, 18, 17)]
        )
    )
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    result = await make_adapter().execute_query(query, page=Page(offset=2, limit=2))

    assert [item.key for item in result.items] == ["story:18", "story:17"]
    assert result.total == 5  # a full final page -> fetched + 1 lower bound


@respx.mock
async def test_execute_query_chains_skip_and_top_beyond_one_request() -> None:
    first = [story_json(identifier) for identifier in range(250, 150, -1)]
    second = [story_json(identifier) for identifier in range(150, 140, -1)]
    stories = respx.get(f"{API}/Stories").mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
        ]
    )
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    result = await make_adapter().execute_query(query, page=Page(offset=0, limit=120))

    assert len(stories.calls) == 2
    assert stories.calls[0].request.url.params["$top"] == "100"  # per-request ceiling
    assert stories.calls[0].request.url.params["$skip"] == "0"
    assert stories.calls[1].request.url.params["$top"] == "20"  # only the remainder
    assert stories.calls[1].request.url.params["$skip"] == "100"
    assert result.total == 110  # short second page -> exact
    assert len(result.items) == 110


@respx.mock
async def test_execute_query_refuses_a_program_outside_the_connection() -> None:
    query = TranslatedQuery(provider="jira_align", query="programId eq 99 and state eq 1")
    with pytest.raises(ValueError, match="outside the connection"):
        await make_adapter().execute_query(query, page=Page(limit=5))


async def test_execute_query_refuses_another_providers_query() -> None:
    query = TranslatedQuery(provider="jira", query="project = PHX")
    with pytest.raises(ValueError, match="does not match jira_align"):
        await make_adapter().execute_query(query, page=Page(limit=5))


@respx.mock
async def test_list_items_filters_through_the_same_canonical_path() -> None:
    defects = respx.get(f"{API}/Defects").mock(
        return_value=httpx.Response(200, json=[defect_json(481)])
    )
    page = await make_adapter().list_items(
        WorkItemFilters(kind="bug", status="open", text="submit"), page=Page(limit=5)
    )
    assert [item.key for item in page.items] == ["bug:481"]
    # The status placeholder resolves last; `and` order does not change meaning.
    assert defects.calls[0].request.url.params["$filter"] == (
        "programId eq 42 and contains(title,'submit') and state eq 0"
    )


@respx.mock
async def test_list_response_envelope_is_accepted_alongside_a_bare_array() -> None:
    respx.get(f"{API}/Stories").mock(
        return_value=httpx.Response(200, json={"data": [story_json(13)]})
    )
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    result = await make_adapter().execute_query(query, page=Page(limit=5))
    assert [item.key for item in result.items] == ["story:13"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"unexpected": 1}, "no recognizable item array"),
        ("not-a-list", "must be a JSON array"),
        ([17], "non-object item"),
        ([{"title": "no id"}], "without an id"),
        ([{"id": 13}, {"id": 13}], "duplicate item id"),
    ],
)
@respx.mock
async def test_malformed_list_responses_fail_closed(body: object, match: str) -> None:
    respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=body))
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    with pytest.raises(RuntimeError, match=match):
        await make_adapter().execute_query(query, page=Page(limit=5))


@respx.mock
async def test_an_oversized_page_is_rejected_rather_than_trusted() -> None:
    respx.get(f"{API}/Stories").mock(
        return_value=httpx.Response(200, json=[story_json(identifier) for identifier in (3, 2, 1)])
    )
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    with pytest.raises(RuntimeError, match="page-size budget"):
        await make_adapter().execute_query(query, page=Page(limit=2))


@respx.mock
async def test_a_result_outside_the_program_is_rejected_even_though_the_filter_scoped_it() -> None:
    respx.get(f"{API}/Stories").mock(
        return_value=httpx.Response(200, json=[story_json(13, programId=99)])
    )
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('story')")
    with pytest.raises(WorkTrackingMutationTargetNotFoundError):
        await make_adapter().execute_query(query, page=Page(limit=5))


# ── read-only surface ─────────────────────────────────────────────────────────


async def test_create_and_enrich_are_rejected_with_an_actionable_message() -> None:
    adapter = make_adapter()
    with pytest.raises(WorkTrackingMutationRejectedError, match="read-only"):
        await adapter.create_item(WorkItemDraft(title="New story"))
    with pytest.raises(WorkTrackingMutationRejectedError, match="read-only"):
        await adapter.enrich_item("story:13", Enrichment(comment="note"))


def test_adapter_is_not_offered_to_the_durable_mutation_coordinator() -> None:
    from apex.ports.work_tracking import IdempotentWorkTrackingMutationPort

    assert not isinstance(make_adapter(), IdempotentWorkTrackingMutationPort)


# ── helpers ───────────────────────────────────────────────────────────────────


def test_align_html_to_text_strips_markup_and_unescapes_entities() -> None:
    assert align_html_to_text("<p>a &amp; b</p><p>c</p>") == "a & b c"
    assert align_html_to_text(None) == ""
    assert align_html_to_text(17) == ""


async def test_bootstrap_registers_the_provider_and_can_build_it() -> None:
    """Without the register_builtin_adapters import the provider is unusable."""

    from apex.adapters import register_builtin_adapters
    from apex.adapters.registry import AdapterRegistry

    register_builtin_adapters()
    assert "jira_align" in AdapterRegistry.providers_for(PortKind.WORK_TRACKING)
    conn = ConnectionConfig(
        id="align-acme",
        kind=PortKind.WORK_TRACKING,
        provider="jira_align",
        name="Acme Jira Align",
        options={"base_url": BASE, "program_id": "42"},
        secret_ref="env:APEX_INTEGRATION_JIRA_ALIGN_TOKEN",
    )

    class Secrets:
        async def resolve(self, secret_ref: str) -> SecretValue:
            return SecretValue(value=TOKEN)

    adapter = await AdapterRegistry.build(conn, Secrets())
    assert isinstance(adapter, JiraAlignWorkTrackingAdapter)
    assert adapter.project_id == "42"


async def test_list_items_rejects_a_kind_the_tracker_does_not_expose() -> None:
    """An unknown kind filter must not silently widen to the default fan-out."""

    with pytest.raises(ValueError, match="does not expose work items of kind"):
        await make_adapter().list_items(WorkItemFilters(kind="issue"), page=Page(limit=5))


@respx.mock
async def test_list_items_normalizes_the_kind_filter_case() -> None:
    defects = respx.get(f"{API}/Defects").mock(return_value=httpx.Response(200, json=[]))
    await make_adapter().list_items(WorkItemFilters(kind="Bug"), page=Page(limit=5))
    assert len(defects.calls) == 1


# ── second-review regression coverage ─────────────────────────────────────────


@respx.mock
async def test_a_top_level_or_residual_stays_bounded_by_the_program_clause() -> None:
    """`programId eq N and A or B` would re-associate as `(... and A) or B`."""

    stories = respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(
        provider="jira_align", query="itemtype in ('story') and (state eq 2 or id gt 0)"
    )
    await make_adapter().execute_query(query, page=Page(limit=5))
    assert stories.calls[0].request.url.params["$filter"] == (
        "programId eq 42 and (state eq 2 or id gt 0)"
    )


@respx.mock
async def test_unparenthesized_or_from_an_unscoped_caller_is_grouped_before_the_wire() -> None:
    stories = respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(
        provider="jira_align", query="itemtype in ('story') and state eq 2 or id gt 0"
    )
    await make_adapter().execute_query(query, page=Page(limit=5))
    # parse keeps `state eq 2 or id gt 0` as one residual term; the program
    # clause must bound BOTH sides of that `or`.
    assert stories.calls[0].request.url.params["$filter"] == (
        "programId eq 42 and (state eq 2 or id gt 0)"
    )


@respx.mock
async def test_itemtype_matching_is_case_insensitive_and_still_fails_closed() -> None:
    stories = respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=[]))
    query = TranslatedQuery(provider="jira_align", query="itemtype in ('Story')")
    await make_adapter().execute_query(query, page=Page(limit=5))
    assert len(stories.calls) == 1  # only Stories — not the default fan-out

    with pytest.raises(ValueError, match="unknown type 'BADKIND'"):
        parse_align_query("itemtype in ('story','BADKIND')")


async def test_a_grouped_status_term_is_refused_not_shipped() -> None:
    """`itemstatus` is synthetic; sending it upstream yields an opaque 400."""

    query = TranslatedQuery(provider="jira_align", query="(itemstatus in (closed) or state eq 1)")
    with pytest.raises(ValueError, match="grouped expressions"):
        await make_adapter().execute_query(query, page=Page(limit=5))


@respx.mock
async def test_a_userinfo_bearing_payload_url_is_dropped_not_fatal() -> None:
    """The central validator rejects userinfo URLs; keeping one would turn a
    benign link into a failed item read."""

    respx.get(f"{API}/Stories/13").mock(
        return_value=httpx.Response(
            200, json=story_json(13, url="https://user@acme.jiraalign.com/story/13")
        )
    )
    item = await make_adapter().get_item("story:13")
    assert item.url is None
    assert validated_provider_work_item(item) == item


async def test_the_durable_mutation_coordinator_rejects_align_as_a_client_error() -> None:
    """502 would invite retries against a permanently read-only provider."""

    from apex.services.work_item_mutations import _require_idempotent_adapter

    with pytest.raises(WorkTrackingMutationRejectedError, match="read-only"):
        _require_idempotent_adapter(make_adapter())


@respx.mock
async def test_a_provider_400_mid_fan_out_discards_partial_results() -> None:
    respx.get(f"{API}/Stories").mock(return_value=httpx.Response(200, json=[story_json(13)]))
    respx.get(f"{API}/Defects").mock(
        return_value=httpx.Response(400, json={"message": "bad filter"})
    )
    query = TranslatedQuery(provider="jira_align", query="state eq 0")
    with pytest.raises(WorkTrackingMutationRejectedError, match="rejected the request"):
        await make_adapter().execute_query(query, page=Page(limit=5))


def test_align_html_to_text_truncates_with_an_ellipsis_at_the_domain_limit() -> None:
    from apex.domain.input_limits import MAX_DESCRIPTION_CHARS

    text = align_html_to_text("<p>" + "word " * MAX_DESCRIPTION_CHARS + "</p>")
    assert len(text) <= MAX_DESCRIPTION_CHARS
    assert text.endswith("…")


async def test_phrases_are_dropped_when_the_site_rejects_contains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.adapters.jira_align import work_tracking as align_module

    monkeypatch.setattr(align_module, "_TEXT_FILTER_SUPPORTED", False)
    translated = await make_adapter().translate_query(
        'stories about "checkout latency"', context=QueryContext()
    )
    assert "contains(" not in translated.query
    assert translated.query == "programId eq 42 and itemtype in ('story')"


@pytest.mark.parametrize(
    "url",
    [
        "https://[::bad",  # urlsplit raises
        f"{BASE}:99999/story/13",  # invalid port
        f"{BASE}/story/api_token=abcdef0123456789",  # credential-shaped path
    ],
)
@respx.mock
async def test_unacceptable_payload_urls_are_dropped_not_fatal(url: str) -> None:
    """Any URL the central validator would reject must be dropped here, or a
    droppable link becomes a failed item read."""

    respx.get(f"{API}/Stories/13").mock(
        return_value=httpx.Response(200, json=story_json(13, url=url))
    )
    item = await make_adapter().get_item("story:13")
    assert item.url is None
    assert validated_provider_work_item(item) == item


def test_parse_rejects_an_unmatchable_kind_name_instead_of_widening() -> None:
    """'story2' cannot match the quoted-kind shape; silently dropping it would
    widen the query to the default story+defect fan-out."""

    with pytest.raises(ValueError, match="unknown type"):
        parse_align_query("itemtype in ('story2')")
    with pytest.raises(ValueError, match="unknown type"):
        parse_align_query("itemtype in (story)")  # unquoted


def test_parse_rejects_a_second_status_term_instead_of_unioning() -> None:
    """`open and closed` matches nothing; a status union would match nearly all."""

    with pytest.raises(ValueError, match="at most one status term"):
        parse_align_query("itemstatus in (open) and itemstatus in (closed)")
    with pytest.raises(ValueError, match="at most one status term"):
        parse_align_query("itemstatus in (open) and (itemstatus in (closed))")
