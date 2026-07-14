"""Compliance endpoints require a platform-wide, not merely role-admin, identity."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.compliance import router

SCOPED_ADMIN = ConsumerIdentity(
    consumer_id="scoped-admin",
    name="scoped-admin",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.ADMIN,
    scopes=[ScopeRef(project_id="p1")],
)


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
