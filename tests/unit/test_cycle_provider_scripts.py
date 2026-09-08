"""Provider export drafts preserve preview identity and fail before mutation on drift."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.domain.cycles import version_digest
from apex.domain.provider_scripts import ImportProviderScript, ProviderScriptExport
from apex.routers import cycle_provider_scripts as api


def request():
    return ImportProviderScript(
        expected_revision=3,
        idempotency_key="import-key",
        connection_id="connection",
        connection_version=datetime(2026, 9, 7, tzinfo=UTC),
        remote_script_id="remote",
        expected_source_checksum="a" * 64,
        plan_version_id="plan",
        title="Checkout",
        parameters="No files",
        coverage="Checkout requirement",
    )


@pytest.mark.asyncio
async def test_import_retry_returns_original_without_provider_io(monkeypatch):
    body = request()
    cycle = SimpleNamespace(id="cycle")
    row = SimpleNamespace(
        id="import",
        version_id="version",
        connection_id="connection",
        connection_version=body.connection_version,
        remote_script_id="remote",
        remote_name="Checkout",
        remote_project_id="remote-project",
        remote_version="1",
        source_checksum="a" * 64,
        imported_by="operator",
        imported_at=body.connection_version,
        request_fingerprint=version_digest(
            body.model_dump(mode="json", exclude={"expected_revision", "idempotency_key"})
        ),
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    provider = AsyncMock()
    monkeypatch.setattr(api, "read_provider", provider)
    session = AsyncMock()
    session.scalar.return_value = row
    result = await api.import_script("cycle", body, session, Mock(), AsyncMock())
    assert result.version_id == "version" and result.executable_content_verified is False
    provider.assert_not_awaited()
    session.commit.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        await api.import_script(
            "cycle",
            body.model_copy(update={"coverage": "Changed coverage"}),
            session,
            Mock(),
            AsyncMock(),
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_preview_drift_does_not_create_a_draft(monkeypatch):
    body = request()
    cycle = SimpleNamespace(id="cycle", mode="live", revision=3, project_id="project", versions=[])
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    monkeypatch.setattr(
        api,
        "choices",
        AsyncMock(
            return_value=[SimpleNamespace(id="connection", runtime_version=body.connection_version)]
        ),
    )
    monkeypatch.setattr(api, "_approved", Mock(return_value={}))
    monkeypatch.setattr(
        api,
        "read_provider",
        AsyncMock(
            return_value=ProviderScriptExport(
                id="remote",
                name="Changed",
                project_id="provider-project",
                version="2",
                protocol="http",
                source='{"changed":true}',
                source_checksum="b" * 64,
            )
        ),
    )
    session = AsyncMock()
    session.scalar.return_value = None
    with pytest.raises(HTTPException) as error:
        await api.import_script("cycle", body, session, Mock(), AsyncMock())
    assert error.value.status_code == 409
    session.commit.assert_not_awaited()
    assert session.scalar.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["static", "generation", "scope"])
async def test_provider_resolution_keeps_scope_and_generation(monkeypatch, change):
    now = datetime.now(UTC)
    metadata = SimpleNamespace(
        persisted=True,
        config=SimpleNamespace(id="connection"),
        connection_version=now,
        internal_project_id="project",
    )
    if change == "static":
        metadata.persisted = False
    if change == "generation":
        metadata.connection_version = None
    if change == "scope":
        metadata.internal_project_id = "other-project"
    resolver = AsyncMock()
    resolver.resolve_metadata.return_value = metadata
    with pytest.raises(HTTPException) as error:
        await api.read_provider(resolver, "project", "connection", now, "remote")
    assert error.value.status_code == 503
    resolver.build_from_metadata.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_import_saves_draft_and_provenance_atomically(monkeypatch, conflict):
    from tests.unit.test_cycles import act, version

    plan = version()
    versions = act(act([plan], plan, "submit", "author"), plan, "approve")
    body = request().model_copy(update={"plan_version_id": plan["id"]})
    cycle = SimpleNamespace(
        id="cycle", mode="live", revision=3, project_id="project", versions=versions
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    monkeypatch.setattr(
        api,
        "choices",
        AsyncMock(
            return_value=[SimpleNamespace(id="connection", runtime_version=body.connection_version)]
        ),
    )
    monkeypatch.setattr(
        api,
        "read_provider",
        AsyncMock(
            return_value=ProviderScriptExport(
                id="remote",
                name="Checkout",
                project_id="provider-project",
                version="1",
                protocol="http",
                source='{"actions":[]}',
                source_checksum="a" * 64,
            )
        ),
    )
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [None, None if conflict else "cycle"]

    async def refresh(row):
        row.id = "import"
        row.imported_at = body.connection_version

    session.refresh.side_effect = refresh
    identity = Mock(consumer_id="operator")
    if conflict:
        with pytest.raises(HTTPException) as error:
            await api.import_script("cycle", body, session, identity, AsyncMock())
        assert error.value.status_code == 409
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
        return
    result = await api.import_script("cycle", body, session, identity, AsyncMock())
    update_statement = session.scalar.await_args_list[1].args[0]
    stored_versions = update_statement.compile().params["versions"]
    assert stored_versions[:-1] == versions
    assert stored_versions[-1]["status"] == "draft"
    assert stored_versions[-1]["dependencies"] == [plan["id"]]
    assert stored_versions[-1]["content"]["source"] == '{"actions":[]}'
    record = session.add.call_args.args[0]
    assert result.version_id == stored_versions[-1]["id"] == record.version_id
    assert record.remote_script_id == "remote"
    assert record.source_checksum == body.expected_source_checksum
    session.commit.assert_awaited_once()
