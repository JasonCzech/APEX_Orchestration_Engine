"""Compliance endpoints require a platform-wide, not merely role-admin, identity."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apex.routers.compliance as compliance_module
from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.db import get_session
from apex.routers.compliance import router

SCOPED_ADMIN = ConsumerIdentity(
    consumer_id="scoped-admin",
    name="scoped-admin",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.ADMIN,
    scopes=[ScopeRef(project_id="p1")],
)
PLATFORM_ADMIN = ConsumerIdentity(
    consumer_id="platform-admin",
    name="platform-admin",
    consumer_type=ConsumerType.INTERNAL,
    role=Role.ADMIN,
)


class EmptyAuditSession:
    async def scalars(self, statement):  # noqa: ANN001, ANN202
        return []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/admin/compliance/audit/chain"),
        ("get", "/v1/admin/compliance/audit/export.jsonl"),
        ("get", "/v1/admin/compliance/audit/export.cef"),
        ("get", "/v1/admin/compliance/audit/retention?before=2026-01-01T00:00:00Z"),
        ("delete", "/v1/admin/compliance/audit/retention?before=2026-01-01T00:00:00Z"),
    ],
)
def test_scoped_admin_cannot_access_global_compliance(method: str, path: str) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: SCOPED_ADMIN

    with TestClient(app) as client:
        response = client.request(method, path)

    assert response.status_code == 403
    assert response.json()["detail"] == "Compliance access requires platform admin"


@pytest.mark.parametrize("method", ["get", "delete"])
def test_audit_retention_rejects_future_cutoff(method: str) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: PLATFORM_ADMIN
    app.dependency_overrides[get_session] = lambda: object()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    with TestClient(app) as client:
        response = client.request(
            method,
            "/v1/admin/compliance/audit/retention",
            params={"before": future},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid audit retention cutoff"


def test_audit_export_admission_is_bounded_and_released_after_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = compliance_module._AuditExportLimiter(1)
    monkeypatch.setattr(compliance_module, "_audit_export_limiter", limiter)
    held = limiter.try_acquire()
    assert held is not None

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: PLATFORM_ADMIN
    app.dependency_overrides[get_session] = lambda: EmptyAuditSession()

    with TestClient(app) as client:
        rejected = client.get("/v1/admin/compliance/audit/export.jsonl")
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "5"

        held.release()
        streamed = client.get("/v1/admin/compliance/audit/export.jsonl")

    assert streamed.status_code == 200
    assert streamed.content == b""
    reacquired = limiter.try_acquire()
    assert reacquired is not None
    reacquired.release()


@pytest.mark.anyio
async def test_audit_export_stream_detaches_database_failure_context() -> None:
    secret = "audit-export-database-secret-canary"

    async def failing_lines():  # noqa: ANN202
        yield "first"
        raise RuntimeError(secret)

    stream = compliance_module._stable_audit_export(failing_lines())

    assert await anext(stream) == "first\n"
    with pytest.raises(compliance_module._AuditExportStreamError) as exc_info:
        await anext(stream)

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


@pytest.mark.anyio
async def test_audit_export_consumer_close_closes_provider_iterator() -> None:
    class TrackingIterator:
        def __init__(self) -> None:
            self.sent = False
            self.closed = False

        def __aiter__(self) -> "TrackingIterator":
            return self

        async def __anext__(self) -> str:
            if self.sent:
                await asyncio.Event().wait()
            self.sent = True
            return "first"

        async def aclose(self) -> None:
            self.closed = True

    lines = TrackingIterator()
    stream: Any = compliance_module._stable_audit_export(lines)
    assert await anext(stream) == "first\n"

    await stream.aclose()

    assert lines.closed is True


@pytest.mark.anyio
async def test_audit_export_cleanup_failure_never_replaces_stable_read_failure() -> None:
    read_secret = "audit-read-secret-canary"
    close_secret = "audit-close-secret-canary"

    class FailingIterator:
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def __aiter__(self) -> "FailingIterator":
            return self

        async def __anext__(self) -> str:
            self.reads += 1
            if self.reads == 1:
                return "first"
            raise RuntimeError(read_secret)

        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError(close_secret)

    lines = FailingIterator()
    stream = compliance_module._stable_audit_export(lines)
    assert await anext(stream) == "first\n"

    with pytest.raises(compliance_module._AuditExportStreamError) as exc_info:
        await anext(stream)

    assert lines.closed is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert read_secret not in repr(exc_info.value)
    assert close_secret not in repr(exc_info.value)
