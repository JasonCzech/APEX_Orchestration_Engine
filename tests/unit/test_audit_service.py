"""Audit service row construction and hash-chain behavior."""

from typing import Any

import pytest

from apex.services.audit import AuditEvent, AuditService, request_audit_event


class FakeAuditSession:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def scalar(self, statement: Any) -> str | None:
        return self.rows[-1].event_hash if self.rows else None

    def add(self, row: Any) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        return None

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
    assert len(first.event_hash) == 64
    assert second.previous_hash == first.event_hash
    assert second.event_hash != first.event_hash
    assert session.rows == [first, second]


def test_request_audit_event_extracts_decision_and_request_context() -> None:
    event = request_audit_event(
        {
            "method": "POST",
            "path": "/threads",
            "headers": [(b"x-request-id", b"req-1"), (b"user-agent", b"pytest")],
            "client": ("203.0.113.10", 12345),
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
