"""Script artifact trust, bounded reads and connection-affinity regressions."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.persistence.models import CycleScriptArtifact
from apex.ports.artifact_store import StoredArtifact
from apex.routers import cycle_scripts as api
from apex.services.cycle_scripts import (
    MAX_SCRIPT_BYTES,
    read_verified_script,
    script_bytes,
    store_verified_script,
)


@pytest.mark.asyncio
async def test_exact_utf8_bytes_round_trip_and_detect_tampering():
    store = MemoryArtifactStore()
    data = script_bytes('{"name":"Checkout café"}')
    checksum = await store_verified_script(store, "cycle-scripts/roundtrip", data)
    assert checksum == hashlib.sha256(data).hexdigest()
    assert await read_verified_script(store, "cycle-scripts/roundtrip", checksum) == data
    await store.put("cycle-scripts/roundtrip", b"changed", content_type="text/plain")
    with pytest.raises(ValueError, match="checksum"):
        await read_verified_script(store, "cycle-scripts/roundtrip", checksum)


@pytest.mark.asyncio
async def test_invalid_store_ack_cannot_be_treated_as_verified():
    store = AsyncMock()
    store.put.return_value = StoredArtifact(key="other-key", uri="memory://other-key", size=4)
    with pytest.raises(RuntimeError, match="invalid object metadata"):
        await store_verified_script(store, "expected", b"data")
    store.iter_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_bounded_read_closes_oversized_provider_stream():
    closed = []

    async def chunks(*args, **kwargs):
        try:
            yield b"x" * (MAX_SCRIPT_BYTES + 1)
        finally:
            closed.append(True)

    store = Mock(iter_bytes=chunks)
    with pytest.raises(ValueError, match="bounded"):
        await read_verified_script(store, "key", "checksum")
    assert closed == [True]


@pytest.mark.parametrize("source", ["", "x" * (MAX_SCRIPT_BYTES + 1), "é" * MAX_SCRIPT_BYTES])
def test_source_byte_bound_is_not_character_bound(source):
    with pytest.raises(ValueError):
        script_bytes(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["static", "generation", "provider", "scope", "identity"])
async def test_changed_or_ephemeral_connection_never_builds_an_adapter(change):
    now = datetime.now(UTC)
    metadata = SimpleNamespace(
        persisted=True,
        config=SimpleNamespace(id="store", provider="s3"),
        connection_version=now,
        internal_project_id="project",
    )
    if change == "static":
        metadata.persisted = False
    if change == "generation":
        metadata.connection_version = now + timedelta(seconds=1)
    if change == "provider":
        metadata.config.provider = "stub"
    if change == "scope":
        metadata.internal_project_id = "other-project"
    if change == "identity":
        metadata.config.id = "other-store"
    resolver = AsyncMock()
    resolver.resolve_metadata.return_value = metadata
    with pytest.raises(ValueError):
        await api.resolve_store(
            resolver, CycleScriptArtifact(connection_id="store", connection_version=now), "project"
        )
    resolver.build_from_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_is_checked_before_store_or_download_resolution(monkeypatch):
    monkeypatch.setattr(api, "find", AsyncMock(side_effect=HTTPException(404, "Cycle not found")))
    resolver = AsyncMock()
    with pytest.raises(HTTPException):
        await api.download_script("cycle", "version", AsyncMock(), Mock(), resolver)
    resolver.resolve_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_failure_hides_provider_details_and_releases_adapter(monkeypatch):
    now = datetime.now(UTC)
    record = CycleScriptArtifact(
        id="artifact", artifact_key="key", source_checksum="expected", stored_at=now
    )
    session = AsyncMock()
    session.expunge = Mock()
    session.scalar.return_value = record
    monkeypatch.setattr(
        api, "find", AsyncMock(return_value=SimpleNamespace(id="cycle", project_id="project"))
    )
    store = Mock(aclose=AsyncMock())
    monkeypatch.setattr(api, "resolve_store", AsyncMock(return_value=store))
    monkeypatch.setattr(
        api, "read_verified_script", AsyncMock(side_effect=RuntimeError("provider secret"))
    )
    with pytest.raises(HTTPException) as error:
        await api.download_script("cycle", "version", session, Mock(), AsyncMock())
    assert error.value.status_code == 503 and "secret" not in str(error.value.detail)
    store.aclose.assert_awaited_once()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_stored", [True, False])
async def test_storage_retry_never_overwrites_a_verified_object(monkeypatch, already_stored):
    now = datetime.now(UTC)
    source = '{"steps":[]}'
    checksum = hashlib.sha256(source.encode()).hexdigest()
    record = CycleScriptArtifact(
        id="artifact",
        cycle_id="cycle",
        version_id="version",
        version_checksum="approved",
        source_checksum=checksum,
        size_bytes=len(source),
        artifact_key="key",
        connection_id="store",
        connection_version=now,
        created_by="operator",
        created_at=now,
        stored_at=now if already_stored else None,
    )
    session = AsyncMock()
    session.expunge = Mock()
    session.scalar.return_value = record
    monkeypatch.setattr(
        api,
        "find",
        AsyncMock(return_value=SimpleNamespace(id="cycle", project_id="project", versions=[])),
    )
    monkeypatch.setattr(
        api,
        "_approved",
        Mock(
            return_value={
                "checksum": "approved",
                "content": {"source": source, "format": "apex_native_json"},
            }
        ),
    )
    monkeypatch.setattr(api, "resolve_store", AsyncMock(return_value=Mock(aclose=AsyncMock())))
    read = AsyncMock()
    write = AsyncMock()
    monkeypatch.setattr(api, "read_verified_script", read)
    monkeypatch.setattr(api, "store_verified_script", write)
    await api.store_script(
        "cycle",
        "version",
        api.StoreCycleScript(expected_revision=1, connection_id="store"),
        session,
        Mock(),
        AsyncMock(),
    )
    if already_stored:
        read.assert_awaited_once()
        write.assert_not_awaited()
    else:
        write.assert_awaited_once()
        read.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_write_retains_pending_intent_without_recording_success(monkeypatch):
    source = "{}"
    record = CycleScriptArtifact(
        id="artifact",
        connection_id="store",
        source_checksum=hashlib.sha256(source.encode()).hexdigest(),
        version_checksum="approved",
        stored_at=None,
        artifact_key="key",
    )
    session = AsyncMock()
    session.expunge = Mock()
    session.scalar.return_value = record
    monkeypatch.setattr(
        api,
        "find",
        AsyncMock(return_value=SimpleNamespace(id="cycle", project_id="project", versions=[])),
    )
    monkeypatch.setattr(
        api,
        "_approved",
        Mock(
            return_value={
                "checksum": "approved",
                "content": {"source": source, "format": "apex_native_json"},
            }
        ),
    )
    monkeypatch.setattr(api, "resolve_store", AsyncMock(return_value=Mock(aclose=AsyncMock())))
    monkeypatch.setattr(
        api, "store_verified_script", AsyncMock(side_effect=RuntimeError("secret provider error"))
    )
    with pytest.raises(HTTPException) as error:
        await api.store_script(
            "cycle",
            "version",
            api.StoreCycleScript(expected_revision=1, connection_id="store"),
            session,
            Mock(),
            AsyncMock(),
        )
    assert error.value.status_code == 503 and "secret" not in str(error.value.detail)
    assert record.stored_at is None
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
