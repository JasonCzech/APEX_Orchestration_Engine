"""Audit service row construction and hash-chain behavior."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.services.audit import AuditEvent, AuditService, request_audit_event


class FakeAuditSession:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.executed: list[Any] = []

    async def scalar(self, statement: Any) -> Any | None:
        return max(self.rows, key=lambda row: row.chain_seq) if self.rows else None

    async def scalars(self, statement: Any) -> list[Any]:
        return sorted(self.rows, key=lambda row: row.chain_seq)

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    def add(self, row: Any) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        return None

    async def refresh(self, row: Any) -> None:
        return None


class SharedAuditStore:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.lock = asyncio.Lock()


class ConcurrentAuditSession:
    """Fake Postgres session that makes advisory-lock behavior observable."""

    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __init__(self, store: SharedAuditStore) -> None:
        self._store = store
        self._pending: Any | None = None
        self._locked = False

    async def execute(self, statement: Any) -> None:
        await self._store.lock.acquire()
        self._locked = True

    async def scalar(self, statement: Any) -> Any | None:
        return max(self._store.rows, key=lambda row: row.chain_seq) if self._store.rows else None

    def add(self, row: Any) -> None:
        self._pending = row

    async def commit(self) -> None:
        if self._pending is not None:
            self._store.rows.append(self._pending)
        if self._locked:
            self._locked = False
            self._store.lock.release()

    async def refresh(self, row: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_audit_service_appends_hash_chained_rows() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]

    first = await service.append(
        AuditEvent(
            category="authz_decision",
            action="POST /v1/admin/consumers",
            decision="denied",
            reason="role below admin",
            principal_id="consumer-1",
            principal_type="dashboard",
            principal_role="viewer",
            principal_scopes={"scopes": [{"project_id": "p1"}]},
            request_path="/v1/admin/consumers",
            status_code=403,
        )
    )
    second = await service.append(
        AuditEvent(
            category="security_event",
            action="consumer.create",
            decision="allowed",
            principal_id="admin-1",
            resource_type="api_consumer",
            resource_id="consumer-2",
        )
    )

    assert first.previous_hash is None
    assert first.chain_seq == 1
    assert len(first.event_hash) == 64
    assert second.previous_hash == first.event_hash
    assert second.chain_seq == 2
    assert second.event_hash != first.event_hash
    assert first.event_nonce != second.event_nonce
    assert first.at is not None and second.at is not None
    assert session.rows == [first, second]


@pytest.mark.asyncio
async def test_audit_service_verifies_chain_and_detects_tampering() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]

    first = await service.append(AuditEvent(category="authz", action="read", decision="allowed"))
    second = await service.append(AuditEvent(category="authz", action="write", decision="denied"))
    first.id = "r1"
    second.id = "r2"

    verification = await service.verify_chain()
    assert verification.ok is True
    assert verification.checked == 2
    assert verification.last_hash == second.event_hash

    first.reason = "tampered"
    tampered = await service.verify_chain()
    assert tampered.ok is False
    assert tampered.checked == 1
    assert tampered.first_error == "row r1 event_hash mismatch"


@pytest.mark.asyncio
async def test_audit_service_exports_jsonl_and_cef() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    row = await service.append(
        AuditEvent(
            category="authz_decision",
            action="threads.create",
            decision="denied",
            reason="outside scope",
            principal_id="consumer-1",
            principal_role="viewer",
            request_path="/threads",
            status_code=403,
        )
    )
    row.id = "r1"

    jsonl = await service.export_jsonl()
    assert json.loads(jsonl)["id"] == "r1"
    assert json.loads(jsonl)["chain_seq"] == 1
    assert json.loads(jsonl)["event_hash"] == row.event_hash

    cef = await service.export_cef()
    assert cef.startswith("CEF:0|APEX|Orchestration Engine|1|authz_decision|threads.create|8|")
    assert "outcome=denied" in cef
    assert "cn1Label=chain_seq cn1=1" in cef
    assert f"cs3={row.event_hash}" in cef


@pytest.mark.asyncio
async def test_cef_export_escapes_header_delimiters() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]
    await service.append(
        AuditEvent(category="authz_decision", action="/missing|forged", decision="denied")
    )

    cef = await service.export_cef()

    assert "|/missing\\|forged|8|" in cef


@pytest.mark.asyncio
async def test_audit_chain_uses_sequence_when_clock_moves_backwards() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    now = datetime.now(UTC)

    first = await service.append(
        AuditEvent(
            category="authz",
            action="first",
            decision="allowed",
            at=now + timedelta(hours=1),
        )
    )
    second = await service.append(
        AuditEvent(
            category="authz",
            action="second",
            decision="allowed",
            at=now - timedelta(hours=1),
        )
    )

    third = await service.append(
        AuditEvent(category="authz", action="third", decision="allowed", at=now)
    )

    assert [row.chain_seq for row in session.rows] == [1, 2, 3]
    assert second.previous_hash == first.event_hash
    assert third.previous_hash == second.event_hash
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_chain_uses_sequence_when_timestamps_are_equal() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    at = datetime.now(UTC)

    first = await service.append(
        AuditEvent(category="authz", action="first", decision="allowed", at=at)
    )
    second = await service.append(
        AuditEvent(category="authz", action="second", decision="allowed", at=at)
    )
    first.id = "z-last-by-id"
    second.id = "a-first-by-id"

    third = await service.append(
        AuditEvent(category="authz", action="third", decision="allowed", at=at)
    )

    assert third.previous_hash == second.event_hash
    assert json.loads((await service.export_jsonl()).splitlines()[-1])["chain_seq"] == 3
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_verification_detects_sequence_gap() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    first = await service.append(AuditEvent(category="authz", action="first", decision="allowed"))
    second = await service.append(AuditEvent(category="authz", action="second", decision="allowed"))
    first.id = "r1"
    second.id = "r2"
    second.chain_seq = 3

    verification = await service.verify_chain()

    assert verification.ok is False
    assert verification.checked == 2
    assert verification.first_error == "row r2 chain_seq mismatch: expected 2, found 3"


@pytest.mark.asyncio
async def test_audit_service_retention_preserves_anchor_by_default() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    rows = [
        await service.append(AuditEvent(category="authz", action=str(index), decision="allowed"))
        for index in range(3)
    ]
    for index, row in enumerate(rows, start=1):
        row.id = f"r{index}"
        row.at = row.at.replace(tzinfo=UTC) + timedelta(seconds=index)

    summary = await service.retention_summary(before=rows[-1].at + timedelta(seconds=1))
    assert summary.candidates == 2
    assert summary.preserved_anchor_id == "r3"

    deleted = await service.prune_before(before=rows[-1].at + timedelta(seconds=1))
    assert deleted == 2
    assert session.executed


@pytest.mark.asyncio
async def test_audit_service_serializes_concurrent_identical_events() -> None:
    store = SharedAuditStore()
    event = AuditEvent(
        category="authz_decision",
        action="POST /v1/admin/consumers",
        decision="denied",
        reason="Requires role 'admin' or higher",
        principal_id="consumer-1",
        status_code=403,
    )

    async def append_one() -> None:
        await AuditService(ConcurrentAuditSession(store)).append(event)  # type: ignore[arg-type]

    await asyncio.gather(*(append_one() for _ in range(25)))

    assert len(store.rows) == 25
    assert len({row.event_hash for row in store.rows}) == 25
    assert len({row.event_nonce for row in store.rows}) == 25
    assert store.rows[0].previous_hash is None
    assert [row.chain_seq for row in store.rows] == list(range(1, 26))
    for previous, current in zip(store.rows[:-1], store.rows[1:], strict=True):
        assert current.previous_hash == previous.event_hash


def test_request_audit_event_extracts_decision_and_request_context() -> None:
    identity = ConsumerIdentity(
        consumer_id="consumer-1",
        name="consumer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p1")],
    )
    event = request_audit_event(
        {
            "method": "POST",
            "path": "/threads",
            "headers": [(b"x-request-id", b"req-1"), (b"user-agent", b"pytest")],
            "client": ("203.0.113.10", 12345),
            "state": {"identity": identity},
        },
        status_code=429,
        reason="rate limit exceeded",
    )

    assert event.category == "authz_decision"
    assert event.decision == "rate_limited"
    assert event.reason == "rate limit exceeded"
    assert event.request_method == "POST"
    assert event.request_path == "/threads"
    assert event.request_id == "req-1"
    assert event.user_agent == "pytest"
    assert event.ip_address == "203.0.113.10"
    assert event.principal_id == "consumer-1"
    assert event.principal_type == "dashboard"
    assert event.principal_role == "viewer"
    assert event.principal_scopes == {"scopes": [{"project_id": "p1", "app_id": None}]}
