"""Audit service row construction and hash-chain behavior."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import apex.services.audit as audit_module
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import AuditLog, Base
from apex.services.audit import (
    AuditEvent,
    AuditReadConsistencyError,
    AuditService,
    request_audit_event,
)


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


class SqliteAuditFacade:
    """Exercise the production SQL retention branch against a sync SQLite session."""

    stream_scalars = object()

    def __init__(self, session: Session) -> None:
        self._session = session

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def execute(self, statement: Any) -> Any:
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()


class WatermarkedPagedAuditSession:
    """Makes page transaction release and concurrent appends observable."""

    stream_scalars = object()
    new: tuple[()] = ()
    dirty: tuple[()] = ()
    deleted: tuple[()] = ()

    def __init__(
        self,
        rows: list[AuditLog],
        *,
        page_size: int,
        prune_through_chain_seq_after_first_page: int | None = None,
    ) -> None:
        self.rows = rows
        self.page_size = page_size
        self.prune_through_chain_seq_after_first_page = prune_through_chain_seq_after_first_page
        self.transaction_active = False
        self.rollback_calls = 0
        self.page_calls = 0
        self.watermark: int | None = None
        self.last_returned: int | None = None

    async def execute(self, statement: Any) -> Any:
        self.transaction_active = True
        sql = str(statement)
        assert "min(apex.audit_log.chain_seq)" in sql
        assert "max(apex.audit_log.chain_seq)" in sql
        first_chain_seq = min((row.chain_seq for row in self.rows), default=None)
        self.watermark = max((row.chain_seq for row in self.rows), default=None)
        return SimpleNamespace(one=lambda: (first_chain_seq, self.watermark))

    async def scalars(self, statement: Any) -> list[AuditLog]:
        self.transaction_active = True
        assert self.watermark is not None
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert f"apex.audit_log.chain_seq <= {self.watermark}" in sql
        assert f"LIMIT {self.page_size}" in sql
        if self.last_returned is not None:
            assert f"apex.audit_log.chain_seq > {self.last_returned}" in sql
        eligible = [
            row
            for row in sorted(self.rows, key=lambda candidate: candidate.chain_seq)
            if row.chain_seq <= self.watermark
            and (self.last_returned is None or row.chain_seq > self.last_returned)
        ]
        page = eligible[: self.page_size]
        self.page_calls += 1
        if self.page_calls == 1:
            self.rows.append(_audit_row(self.watermark + 1))
        if page:
            self.last_returned = page[-1].chain_seq
        return page

    def expunge(self, row: AuditLog) -> None:
        assert row in self.rows

    async def commit(self) -> None:
        raise AssertionError("a read-only audit export must never commit its session")

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_calls == 2 and self.prune_through_chain_seq_after_first_page is not None:
            self.rows[:] = [
                row
                for row in self.rows
                if row.chain_seq > self.prune_through_chain_seq_after_first_page
            ]
        self.transaction_active = False


def _audit_row(chain_seq: int) -> AuditLog:
    return AuditLog(
        id=f"r{chain_seq}",
        chain_seq=chain_seq,
        at=datetime.now(UTC),
        category="authz",
        action=f"event-{chain_seq}",
        decision="allowed",
        principal_scopes={},
        extra={},
        event_nonce=f"nonce-{chain_seq}",
        previous_hash=None if chain_seq == 1 else f"{chain_seq - 1:064x}",
        event_hash=f"{chain_seq:064x}",
    )


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
async def test_audit_service_bounds_untrusted_varchar_evidence() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]

    row = await service.append(
        AuditEvent(
            category="c" * 100,
            action="a" * 200,
            decision="d" * 100,
            request_method="M" * 30,
            request_path="/" + "p" * 3000,
            request_id="r" * 500,
            user_agent="u" * 2000,
        )
    )

    assert len(row.category) == 64
    assert len(row.action) == 128
    assert len(row.decision) == 32
    assert row.request_method is not None and len(row.request_method) == 16
    assert row.request_path is not None and len(row.request_path) == 2048
    assert row.request_id is not None and len(row.request_id) == 255
    assert row.user_agent is not None and len(row.user_agent) == 1024
    assert "[truncated:" in row.user_agent
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_sanitizes_postgres_incompatible_nul_evidence() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]

    row = await service.append(
        AuditEvent(
            category="authz\x00decision",
            action="/forged%00\x00path",
            decision="denied",
            reason="bad\x00reason",
            principal_scopes={"scope\x00key": [{"project_id": "p1\x00hidden"}]},
            extra={"nested": ["value\x00suffix"]},
        )
    )

    persisted = json.dumps(
        {
            "category": row.category,
            "action": row.action,
            "reason": row.reason,
            "principal_scopes": row.principal_scopes,
            "extra": row.extra,
        }
    )
    assert "\\u0000" not in persisted
    assert "\\ufffd" in persisted
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_redacts_values_selected_by_credential_mapping_keys() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]

    row = await service.append(
        AuditEvent(
            category="security_event",
            action="credential-key-evidence",
            decision="observed",
            extra={
                "password": "plain-password-value",
                "nested": {"api_key": "plain-api-key-value"},
            },
        )
    )

    encoded = json.dumps(row.extra, sort_keys=True)
    assert encoded.count("[REDACTED]") == 2
    assert encoded.count("[redacted-credential-key]") == 2
    assert "plain-password-value" not in encoded
    assert "plain-api-key-value" not in encoded
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_redacts_generic_nested_credential_strings() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]

    row = await service.append(
        AuditEvent(
            category="security_event",
            action="generic-value-evidence",
            decision="observed",
            principal_scopes={
                "scopes": [{"project_id": "password=project-secret"}],
            },
            extra={
                "message": "Authorization: Bearer nested-bearer-secret; retry",
            },
        )
    )

    encoded = json.dumps(
        {"principal_scopes": row.principal_scopes, "extra": row.extra},
        sort_keys=True,
    )
    assert encoded.count("[REDACTED]") == 2
    assert "project-secret" not in encoded
    assert "nested-bearer-secret" not in encoded
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_redacts_signed_urls_and_userinfo() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]
    signed_url = (
        "https://url-user:url-password@example.com/report?"
        "X-Amz-Signature=signed-secret&X-Amz-Credential=credential-secret&part=1"
    )

    row = await service.append(
        AuditEvent(
            category="security_event",
            action="signed-url-evidence",
            decision="observed",
            extra={"download_url": signed_url},
        )
    )

    encoded = json.dumps(row.extra, sort_keys=True)
    assert "[REDACTED]" in encoded
    for secret_value in (
        "url-user",
        "url-password",
        "signed-secret",
        "credential-secret",
    ):
        assert secret_value not in encoded
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_redacts_every_scalar_text_field_before_hashing() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]
    secret_value = "scalar-secret-value"

    row = await service.append(
        AuditEvent(
            category=f"password={secret_value}",
            action=f"Authorization: Bearer {secret_value}",
            decision=f"token={secret_value}",
            reason=f"client_secret={secret_value}",
            principal_id=f"password={secret_value}",
            principal_type=f"password={secret_value}",
            principal_role=f"password={secret_value}",
            request_method=f"token={secret_value}",
            request_path=f"/denied?sig={secret_value}",
            request_id=f"api_key={secret_value}",
            ip_address=f"password={secret_value}",
            user_agent=f"Basic {secret_value}",
            resource_type=f"password={secret_value}",
            resource_id=f"password={secret_value}",
        )
    )

    scalar_fields = (
        "category",
        "action",
        "decision",
        "reason",
        "principal_id",
        "principal_type",
        "principal_role",
        "request_method",
        "request_path",
        "request_id",
        "ip_address",
        "user_agent",
        "resource_type",
        "resource_id",
    )
    encoded = json.dumps(
        {name: getattr(row, name) for name in scalar_fields},
        sort_keys=True,
    )
    assert secret_value not in encoded
    assert "[REDACTED]" in encoded
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_bounds_cycles_depth_fanout_and_non_json_values() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]
    circular: dict[str, Any] = {}
    circular["self"] = circular
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(100):
        nested: dict[str, Any] = {}
        cursor["nested"] = nested
        cursor = nested

    row = await service.append(
        AuditEvent(
            category="security_event",
            action="bounded-evidence",
            decision="observed",
            extra={
                "circular": circular,
                "deep": deep,
                "fanout": list(range(300)),
                "unsupported": object(),
                "not_finite": float("nan"),
                "huge_integer": 1 << 1_000,
            },
        )
    )

    encoded = json.dumps(row.extra, allow_nan=False, sort_keys=True)
    assert "circular-reference" in encoded
    assert "depth-limit" in encoded
    assert "item-limit" in encoded
    assert "unsupported-json-value:object" in encoded
    assert "non-finite-float" in encoded
    assert "integer-out-of-range" in encoded
    assert (await service.verify_chain()).ok is True


@pytest.mark.asyncio
async def test_audit_service_replaces_oversized_json_with_bounded_digest_marker() -> None:
    service = AuditService(FakeAuditSession())  # type: ignore[arg-type]
    extra = {str(index): "x" * 4_096 for index in range(100)}

    row = await service.append(
        AuditEvent(category="security_event", action="oversized", decision="observed", extra=extra)
    )

    marker = row.extra["_apex_evidence_truncated"]
    assert marker["reason"] == "encoded-byte-limit"
    assert marker["original_bytes"] > 256 * 1_024
    assert len(marker["sha256"]) == 64
    assert len(json.dumps(row.extra).encode()) < 16_384
    assert (await service.verify_chain()).ok is True


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
async def test_slow_audit_export_releases_each_page_and_stops_at_initial_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_size = 2
    monkeypatch.setattr(audit_module, "AUDIT_EXPORT_PAGE_SIZE", page_size)
    session = WatermarkedPagedAuditSession(
        [_audit_row(chain_seq) for chain_seq in range(1, 6)],
        page_size=page_size,
    )
    lines = AuditService(cast(AsyncSession, session)).iter_jsonl()

    first = json.loads(await anext(lines))

    assert first["chain_seq"] == 1
    assert session.transaction_active is False
    assert session.rollback_calls == 2  # watermark and first materialized page

    # A slow client keeps only serialized strings; no transaction/connection is
    # held between chunks, and a concurrent append above the watermark is omitted.
    await asyncio.sleep(0)
    assert session.transaction_active is False
    remaining = [json.loads(line)["chain_seq"] async for line in lines]

    assert [first["chain_seq"], *remaining] == [1, 2, 3, 4, 5]
    assert session.page_calls == 3
    assert session.rollback_calls == 4
    assert session.transaction_active is False


@pytest.mark.asyncio
async def test_paged_audit_export_aborts_if_retention_removes_an_unread_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "AUDIT_EXPORT_PAGE_SIZE", 2)
    session = WatermarkedPagedAuditSession(
        [_audit_row(chain_seq) for chain_seq in range(1, 6)],
        page_size=2,
        prune_through_chain_seq_after_first_page=3,
    )
    lines = AuditService(cast(AsyncSession, session)).iter_jsonl()

    assert json.loads(await anext(lines))["chain_seq"] == 1
    assert json.loads(await anext(lines))["chain_seq"] == 2
    with pytest.raises(AuditReadConsistencyError, match="expected sequence 3, found 4"):
        await anext(lines)

    assert session.transaction_active is False


@pytest.mark.asyncio
async def test_paged_audit_verification_preserves_hash_chain_across_page_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "AUDIT_EXPORT_PAGE_SIZE", 2)
    seed = FakeAuditSession()
    seed_service = AuditService(seed)  # type: ignore[arg-type]
    for chain_seq in range(1, 6):
        await seed_service.append(
            AuditEvent(
                category="authz",
                action=f"event-{chain_seq}",
                decision="allowed",
            )
        )
    session = WatermarkedPagedAuditSession(
        cast(list[AuditLog], seed.rows),
        page_size=2,
    )

    verification = await AuditService(cast(AsyncSession, session)).verify_chain()

    assert verification.ok is True
    assert verification.checked == 5
    assert session.page_calls == 3
    assert session.transaction_active is False


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
    base = datetime.now(UTC) - timedelta(days=2)
    rows = [
        await service.append(
            AuditEvent(
                category="authz",
                action=str(index),
                decision="allowed",
                at=base + timedelta(seconds=index),
            )
        )
        for index in range(3)
    ]
    for index, row in enumerate(rows, start=1):
        row.id = f"r{index}"

    summary = await service.retention_summary(before=rows[-1].at + timedelta(seconds=1))
    assert summary.candidates == 2
    assert summary.preserved_anchor_id == "r3"

    deleted = await service.prune_before(before=rows[-1].at + timedelta(seconds=1))
    assert deleted == 2
    assert session.executed


@pytest.mark.asyncio
async def test_audit_retention_stops_at_first_new_row_when_clocks_move_backwards() -> None:
    session = FakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    rows = [
        await service.append(
            AuditEvent(
                category="authz",
                action=action,
                decision="allowed",
                at=at,
            )
        )
        for action, at in (
            ("old-1", now - timedelta(days=3)),
            ("new-boundary", now - timedelta(hours=1)),
            ("old-clock-rollback", now - timedelta(days=4)),
        )
    ]
    for index, row in enumerate(rows, start=1):
        row.id = f"r{index}"

    cutoff = now - timedelta(days=1)
    anchored = await service.retention_summary(before=cutoff)
    unanchored = await service.retention_summary(before=cutoff, retain_anchor=False)

    assert anchored.candidates == 0
    assert anchored.preserved_anchor_id == "r1"
    assert unanchored.candidates == 1
    assert await service.prune_before(before=cutoff, retain_anchor=False) == 1


def test_audit_retention_sql_deletes_only_contiguous_chain_prefix() -> None:
    engine = create_engine("sqlite://")
    now = datetime.now(UTC)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
            Base.metadata.tables["apex.audit_log"].create(connection)
        with Session(engine) as session:
            session.add_all(
                [
                    AuditLog(
                        id=f"r{chain_seq}",
                        chain_seq=chain_seq,
                        at=at,
                        category="authz",
                        action=f"event-{chain_seq}",
                        decision="allowed",
                        principal_scopes={},
                        extra={},
                        event_hash=str(chain_seq) * 64,
                    )
                    for chain_seq, at in (
                        (1, now - timedelta(days=3)),
                        (2, now - timedelta(hours=1)),
                        (3, now - timedelta(days=4)),
                    )
                ]
            )
            session.commit()
            service = AuditService(cast(AsyncSession, SqliteAuditFacade(session)))
            cutoff = now - timedelta(days=1)

            summary = asyncio.run(service.retention_summary(before=cutoff, retain_anchor=False))
            deleted = asyncio.run(service.prune_before(before=cutoff, retain_anchor=False))

            assert summary.candidates == 1
            assert deleted == 1
            assert list(session.scalars(select(AuditLog.id).order_by(AuditLog.chain_seq))) == [
                "r2",
                "r3",
            ]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_audit_prune_acquires_writer_lock_before_deleting() -> None:
    class PostgresFakeAuditSession(FakeAuditSession):
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    session = PostgresFakeAuditSession()
    service = AuditService(session)  # type: ignore[arg-type]
    await service.append(
        AuditEvent(
            category="authz",
            action="old",
            decision="allowed",
            at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    session.executed.clear()

    await service.prune_before(before=datetime.now(UTC) - timedelta(days=1), retain_anchor=False)

    assert "pg_advisory_xact_lock" in str(session.executed[0])


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
            "route": SimpleNamespace(path="/threads", operation_id=None),
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


def test_request_audit_event_uses_route_template_not_parameter_value() -> None:
    canary = "bare-secret-token-value"
    event = request_audit_event(
        {
            "method": "GET",
            "path": f"/v1/admin/consumers/{canary}",
            "route": SimpleNamespace(
                path="/v1/admin/consumers/{consumer_id}",
                operation_id="getConsumer",
            ),
        },
        status_code=403,
    )

    assert event.action == "getConsumer"
    assert event.request_path == "/v1/admin/consumers/{consumer_id}"
    assert canary not in repr(event)


def test_request_audit_event_quarantines_unmatched_concrete_path() -> None:
    canary = "bare-secret-token-value"
    event = request_audit_event(
        {
            "method": "GET",
            "path": f"/{canary}",
        },
        status_code=401,
    )

    assert event.action == "<unmatched-route>"
    assert event.request_path == "<unmatched-route>"
    assert canary not in repr(event)
