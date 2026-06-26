"""Auth/principal introspection routes."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.auth import router


def make_app(identity: ConsumerIdentity) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: identity
    return app


def test_auth_me_returns_current_api_consumer_shape() -> None:
    identity = ConsumerIdentity(
        consumer_id="consumer-1",
        name="Dashboard",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="p1", app_id="app-a")],
    )

    with TestClient(make_app(identity)) as client:
        response = client.get("/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "principal_kind": "api_consumer",
        "principal_id": "consumer-1",
        "name": "Dashboard",
        "consumer_type": "dashboard",
        "role": "operator",
        "scopes": [{"project_id": "p1", "app_id": "app-a"}],
        "is_unscoped": False,
        "org_id": None,
        "workspace_id": None,
        "session_expires_at": None,
        "mfa_required": False,
        "step_up_required": False,
        "capabilities": {"api_keys": False, "platform_admin": False},
    }
