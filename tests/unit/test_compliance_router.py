"""Compliance endpoints require a platform-wide, not merely role-admin, identity."""

from datetime import UTC, datetime, timedelta

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
