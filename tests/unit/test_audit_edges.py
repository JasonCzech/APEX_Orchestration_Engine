"""Failure-boundary tests for audit verification, export, and retention."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import apex.services.audit as audit
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import AuditLog


def _row(
    *,
    chain_seq: int = 1,
    previous_hash: str | None = None,
    event_nonce: str | None = "nonce",
) -> AuditLog:
    return AuditLog(
        id=f"row-{chain_seq}",
        chain_seq=chain_seq,
        at=datetime(2026, 7, 1, tzinfo=UTC),
        category="authz",
        action="read",
        decision="allowed",
        principal_scopes={},
        extra={},
        event_nonce=event_nonce,
        previous_hash=previous_hash,
        event_hash="pending",
    )


class _RowsSession:
    def __init__(self, rows: list[AuditLog]) -> None:
        self.rows = rows
        self.commits = 0

    async def scalars(self, _statement: Any) -> list[AuditLog]:
        return self.rows

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize(
    "before",
    [datetime(2026, 7, 1), datetime.now(UTC) + timedelta(days=1)],
)
def test_retention_cutoff_rejects_naive_and_future_times(before: datetime) -> None:
    with pytest.raises(ValueError):
        audit.validate_retention_cutoff(before)


def test_event_from_identity_preserves_delegated_scope_and_anonymous_defaults() -> None:
    identity = ConsumerIdentity(
        consumer_id="consumer-1",
        name="operator",
        consumer_type=ConsumerType.HEADLESS,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project-a", app_id="app-a")],
    )

    event = audit.event_from_identity(
        identity=identity,
        category="authz",
        action="write",
        decision="allowed",
        extra={"safe": True},
    )
    anonymous = audit.event_from_identity(
        identity=None,
        category="authn",
        action="read",
        decision="unauthenticated",
    )

    assert event.principal_id == "consumer-1"
    assert event.principal_scopes == {"scopes": [{"project_id": "project-a", "app_id": "app-a"}]}
    assert event.extra == {"safe": True}
    assert anonymous.principal_id is None
    assert anonymous.principal_scopes == {}


@pytest.mark.asyncio
async def test_verify_truncated_chain_uses_first_row_as_anchor() -> None:
    anchor = "a" * 64
    row = _row(chain_seq=9, previous_hash=anchor)
    row.event_hash = audit._event_hash(audit._event_from_row(row), anchor)
    session = _RowsSession([row])
    service = audit.AuditService(cast(AsyncSession, session))

    result = await service.verify_chain(allow_truncated=True)

    assert result.ok is True
    assert result.checked == 1
    assert result.last_hash == row.event_hash


@pytest.mark.asyncio
async def test_verify_chain_reports_previous_hash_mismatch_before_event_hash() -> None:
    row = _row(previous_hash="unexpected")
    session = _RowsSession([row])

    result = await audit.AuditService(cast(AsyncSession, session)).verify_chain()

    assert result.ok is False
    assert result.checked == 1
    assert result.first_error is not None and "previous_hash mismatch" in result.first_error


class _BoundsSession:
    stream_scalars = object()
    new: tuple[()] = ()
    dirty: tuple[()] = ()
    deleted: tuple[()] = ()

    def __init__(self, bounds: tuple[int | None, int | None]) -> None:
        self.bounds = bounds
        self.rollbacks = 0

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(one=lambda: self.bounds)

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_paged_export_handles_empty_and_inconsistent_watermarks() -> None:
    empty = _BoundsSession((None, None))
    assert [line async for line in audit.AuditService(cast(AsyncSession, empty)).iter_jsonl()] == []
    assert empty.rollbacks == 1

    inconsistent = _BoundsSession((None, 4))
    with pytest.raises(audit.AuditReadConsistencyError, match="inconsistent chain bounds"):
        _ = [
            line async for line in audit.AuditService(cast(AsyncSession, inconsistent)).iter_jsonl()
        ]
    assert inconsistent.rollbacks == 1


@pytest.mark.asyncio
async def test_export_transaction_guards_pending_writes_and_cleanup_failures() -> None:
    pending = SimpleNamespace(new=(object(),), dirty=(), deleted=())
    with pytest.raises(RuntimeError, match="pending mutations"):
        await audit.AuditService(cast(AsyncSession, pending))._release_export_read_transaction()

    no_rollback = SimpleNamespace()
    await audit.AuditService(cast(AsyncSession, no_rollback))._rollback_export_read_transaction()

    class BrokenRollback:
        async def rollback(self) -> None:
            raise RuntimeError("dead connection")

    await audit.AuditService(
        cast(AsyncSession, BrokenRollback())
    )._rollback_export_read_transaction()


@pytest.mark.asyncio
async def test_fallback_prune_commits_empty_retention_window() -> None:
    session = _RowsSession([])
    deleted = await audit.AuditService(cast(AsyncSession, session)).prune_before(
        before=datetime.now(UTC) - timedelta(days=1)
    )
    assert deleted == 0
    assert session.commits == 1


def test_legacy_hash_and_cef_severity() -> None:
    event = audit.AuditEvent(
        category="authz",
        action="read",
        decision="allowed",
        at=datetime(2026, 7, 1, tzinfo=UTC),
        event_nonce="new-field",
    )
    first = audit._legacy_event_hash(event, None)
    second = audit._legacy_event_hash(
        audit.AuditEvent(category="authz", action="read", decision="allowed"), None
    )
    assert first == second

    row = _row()
    for decision, status, expected in [
        ("denied", None, 8),
        ("rate_limited", None, 6),
        ("allowed", None, 3),
        ("unknown", 503, 7),
        ("unknown", 404, 5),
        ("unknown", 200, 2),
    ]:
        row.decision = decision
        row.status_code = status
        assert audit._cef_severity(row) == expected


def test_audit_headers_are_relevant_bounded_and_hook_safe() -> None:
    calls: list[str] = []

    class HostileValue:
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile header hook ran")

        def decode(self, _encoding: str) -> str:
            calls.append("decode")
            raise AssertionError("hostile header hook ran")

    headers: list[Any] = [
        (HostileValue(), HostileValue()),
        (b"irrelevant", b"x" * 1_000_000),
        (b"X-Request-ID", b"req-1"),
        (b"user-agent", b"pytest"),
    ]

    assert audit._headers({"headers": headers}) == {
        "x-request-id": "req-1",
        "user-agent": "pytest",
    }
    assert calls == []


def test_audit_headers_fail_closed_on_oversize_duplicates_and_scan_overflow() -> None:
    headers = [
        (b"x-request-id", b"a"),
        (b"X-Request-ID", b"b"),
        (b"user-agent", b"x" * (audit._AUDIT_TEXT_LIMITS["user_agent"] + 1)),
        *[(b"irrelevant", b"value")] * audit._AUDIT_HEADER_SCAN_LIMIT,
        (b"user-agent", b"after-limit"),
    ]

    assert audit._headers({"headers": headers}) == {}
    assert audit._headers({"headers": object()}) == {}


def test_request_audit_event_does_not_coerce_or_invoke_raw_scope_fields() -> None:
    calls: list[str] = []

    class HostileValue:
        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("raw scope hook ran")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("raw scope hook ran")

    class HostileRoute:
        @property
        def path(self) -> str:
            calls.append("path")
            raise AssertionError("route descriptor ran")

        @property
        def operation_id(self) -> str:
            calls.append("operation_id")
            raise AssertionError("route descriptor ran")

    class HostileTuple(tuple[Any, ...]):
        def __getitem__(self, _index: Any) -> Any:
            calls.append("getitem")
            raise AssertionError("client tuple hook ran")

    class HostileExtra(dict[str, Any]):
        def __bool__(self) -> bool:
            calls.append("extra_bool")
            raise AssertionError("extra hook ran")

    event = audit.request_audit_event(
        {
            "method": HostileValue(),
            "client": HostileTuple((HostileValue(), 443)),
            "route": HostileRoute(),
            "state": HostileValue(),
        },
        status_code=403,
        extra=HostileExtra(hostile=HostileValue()),
    )

    assert event.request_method == ""
    assert event.ip_address is None
    assert event.request_path == "<unmatched-route>"
    assert event.principal_id is None
    assert event.extra == {}
    assert calls == []


def test_cef_scalars_are_hook_safe_bounded_and_control_free() -> None:
    calls: list[str] = []

    class HostileValue:
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile CEF hook ran")

    hostile = audit._cef_escape(HostileValue())
    assert calls == []
    assert "diagnostic unavailable" in hostile

    row = _row()
    row.category = "authz\t\x1b\x85"
    row.action = "read\r\n|forged"
    row.reason = "reason\t\x00\x7f\x90"
    rendered = audit._row_cef(row)

    assert "\\n" in rendered
    assert "\\r" in rendered
    assert "\\|forged" in rendered
    assert not any(
        ord(character) < 0x20 or ord(character) == 0x7F or 0x80 <= ord(character) <= 0x9F
        for character in rendered
    )
