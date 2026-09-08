from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.domain.cycle_sources import CaptureSource
from apex.domain.cycles import VersionCreate, add_version, version_digest
from apex.domain.integrations import DocRef, DocumentContent
from apex.routers import cycle_sources as api


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("legacy_null", [False, True])
async def test_source_capture_preserves_revision_and_never_writes_changed_content(
    monkeypatch, drift, legacy_null
):
    source = add_version(
        [],
        VersionCreate(
            expected_revision=1,
            kind="scope",
            title="Scope",
            content={"requirements": "Checkout"},
            change_summary="Initial",
        ),
        "author",
    )
    if legacy_null:
        source["content"]["_source_evidence"] = None
        source["checksum"] = version_digest(source["content"])
    doc = DocumentContent(
        title="Runbook",
        ref=DocRef(id="123", source="confluence", revision="7"),
        text="p95 < 150 ms",
    )
    generation = datetime.now(UTC)
    body = CaptureSource(
        expected_revision=2,
        idempotency_key="capture",
        connection_id="conn",
        connection_version=generation,
        ref=doc.ref,
        expected_checksum=version_digest(doc.model_dump(mode="json")),
        title="Runbook",
    )
    cycle = SimpleNamespace(
        id="cycle", project_id="project", mode="live", revision=2, versions=[source]
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    monkeypatch.setattr(
        api,
        "source_connections",
        AsyncMock(return_value=[SimpleNamespace(id="conn", runtime_version=generation)]),
    )
    if drift:
        doc.text = "Changed"
    provider = AsyncMock(return_value=doc)
    monkeypatch.setattr(api, "provider_read", provider)
    save = AsyncMock()
    monkeypatch.setattr(api, "save_versions", save)
    if drift:
        with pytest.raises(HTTPException) as error:
            await api.capture_source(
                "cycle", source["id"], body, AsyncMock(), Mock(consumer_id="operator"), AsyncMock()
            )
        assert error.value.status_code == 409
        save.assert_not_awaited()
        return
    await api.capture_source(
        "cycle", source["id"], body, AsyncMock(), Mock(consumer_id="operator"), AsyncMock()
    )
    assert save.await_args is not None
    versions = save.await_args.args[3]
    assert versions[0] == source
    record = versions[-1]["content"]["_source_evidence"][0]
    assert record["document"]["ref"]["revision"] == "7"
    assert versions[-1]["status"] == "draft"
    cycle.versions = versions
    monkeypatch.setattr(api, "view", Mock(return_value="replayed"))
    provider.reset_mock()
    assert (
        await api.capture_source("cycle", source["id"], body, AsyncMock(), Mock(), AsyncMock())
        == "replayed"
    )
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_resolver_rejects_static_or_foreign_scope_before_adapter_io():
    resolver = AsyncMock()
    now = datetime.now(UTC)
    resolver.resolve_metadata.return_value = SimpleNamespace(persisted=False)
    with pytest.raises(HTTPException) as error:
        await api.provider_read(
            resolver,
            "project",
            api.ConnectionChoice(
                id="conn", name="Confluence", provider="confluence", runtime_version=now
            ),
            None,
            "",
        )
    assert error.value.status_code == 503
    resolver.build_from_metadata.assert_not_awaited()


def test_generic_scope_write_cannot_forge_captured_source_evidence():
    with pytest.raises(ValueError, match="Frozen evidence"):
        add_version(
            [],
            VersionCreate(
                expected_revision=1,
                kind="scope",
                title="Fake",
                content={"_source_evidence": [{"title": "Forged"}]},
                change_summary="Initial",
            ),
            "author",
        )


@pytest.mark.parametrize(
    "kind,key", [("scope", "_source_evidence"), ("report", "_execution_evidence")]
)
def test_generic_write_rejects_null_reserved_evidence(kind, key):
    with pytest.raises(ValueError, match="Frozen evidence"):
        add_version(
            [],
            VersionCreate(
                expected_revision=1,
                kind=kind,
                title="Draft",
                content={key: None},
                change_summary="Initial",
            ),
            "author",
        )
