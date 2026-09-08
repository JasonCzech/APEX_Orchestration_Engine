import httpx
import pytest
import respx

from apex.adapters.confluence.documents import ConfluenceDocumentsAdapter
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import DocRef, DocScope, SecretValue

BASE = "https://docs.atlassian.net"


def adapter():
    return ConfluenceDocumentsAdapter(
        ConnectionConfig(
            id="docs",
            name="Docs",
            kind=PortKind.DOCUMENTS,
            provider="confluence",
            options={"base_url": BASE, "user_email": "qa@example.test", "space_id": "42"},
        ),
        SecretValue(value="api-token"),
    )


def page():
    return {
        "id": "123",
        "spaceId": "42",
        "status": "current",
        "title": "Checkout plan",
        "version": {"number": 7},
        "body": {"storage": {"value": "<h1>Checkout</h1><p>p95 &lt; 150 ms</p>"}},
    }


@respx.mock
@pytest.mark.asyncio
async def test_search_freezes_revision_and_fetch_returns_readable_content():
    listing = respx.get(BASE + "/wiki/api/v2/pages").mock(
        return_value=httpx.Response(200, json={"results": [page()]})
    )
    detail = respx.get(BASE + "/wiki/api/v2/pages/123").mock(
        return_value=httpx.Response(200, json=page())
    )
    client = adapter()
    hits = await client.search("Checkout plan", scope=DocScope(collections=["42"]), k=1)
    assert hits[0].ref.revision == "7"
    assert listing.calls.last.request.url.params["space-id"] == "42"
    doc = await client.fetch(hits[0].ref)
    assert doc.text == "Checkout\np95 < 150 ms"
    assert detail.calls.last.request.url.params["version"] == "7"
    assert all(call.request.method == "GET" for call in respx.calls)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        DocRef(id="../secrets", source="confluence"),
        DocRef(id="123", source="other"),
        DocRef(
            id="123",
            source="confluence",
            uri="https://other.atlassian.net/wiki/spaces/42/pages/123",
        ),
    ],
)
async def test_foreign_or_malformed_refs_fail_before_io(ref):
    with pytest.raises(ValueError):
        await adapter().fetch(ref)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["space", "revision", "body", "macro"])
async def test_conflicting_and_unsupported_evidence_is_not_successful_content(change):
    data = page()
    if change == "space":
        data["spaceId"] = "43"
    if change == "revision":
        data["version"] = {"number": 8}
    if change == "body":
        data["body"] = {}
    if change == "macro":
        data["body"] = {"storage": {"value": '<ac:structured-macro ac:name="include" />'}}
    respx.get(BASE + "/wiki/api/v2/pages/123").mock(return_value=httpx.Response(200, json=data))
    client = adapter()
    with pytest.raises(RuntimeError):
        await client.fetch(DocRef(id="123", source="confluence", revision="7"))
    await client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_permission_failure_does_not_echo_provider_error_or_credentials():
    respx.get(BASE + "/wiki/api/v2/pages/123").mock(
        return_value=httpx.Response(403, text="api-token")
    )
    client = adapter()
    with pytest.raises(RuntimeError, match="permission denied") as error:
        await client.fetch(DocRef(id="123", source="confluence"))
    assert "api-token" not in str(error.value)
    await client.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_table_cells_remain_separate_in_captured_requirements():
    data = page()
    data["body"]["storage"]["value"] = (
        "<table><tr><th>Transaction</th><th>p95 limit (ms)</th></tr>"
        "<tr><td>Checkout</td><td>150</td></tr></table>"
    )
    respx.get(BASE + "/wiki/api/v2/pages/123").mock(return_value=httpx.Response(200, json=data))
    client = adapter()
    try:
        doc = await client.fetch(DocRef(id="123", source="confluence", revision="7"))
        assert [line.split("\t") for line in doc.text.splitlines() if line.strip()] == [
            ["Transaction", "p95 limit (ms)", ""],
            ["Checkout", "150"],
        ]
    finally:
        await client.aclose()
