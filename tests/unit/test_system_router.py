import pytest
from fastapi.testclient import TestClient

from apex.app import http as http_app
from apex.app.http import app

DEV_KEY = "system-info-dev-key"


def test_system_readiness_is_public_and_dependency_aware() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 204
    assert response.content == b""


def test_system_readiness_hides_dependency_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_app: object) -> None:
        raise RuntimeError("postgresql://admin:secret@database.internal/apex")

    monkeypatch.setattr(http_app, "check_runtime_readiness", unavailable)
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert "secret" not in response.text
    assert response.json()["title"] == "Service is not ready"


def test_system_info_requires_api_key() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/system/info")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_system_info_with_dev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    with TestClient(app) as client:
        response = client.get("/v1/system/info", headers={"x-api-key": DEV_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "APEX Orchestration Engine"
    assert set(body) == {"name", "version", "environment", "features", "limits", "consumer"}
    assert body["limits"]["max_context_packets"] > 0
    assert body["consumer"] == {"name": "dev", "role": "admin", "scopes": []}


def test_system_info_bearer_header_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    with TestClient(app) as client:
        response = client.get("/v1/system/info", headers={"Authorization": f"Bearer {DEV_KEY}"})
    assert response.status_code == 200


def test_system_info_auth_disabled_yields_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__ENABLED", "false")
    with TestClient(app) as client:
        response = client.get("/v1/system/info")
    assert response.status_code == 200
    assert response.json()["consumer"]["name"] == "anonymous"


def test_unknown_route_returns_problem_details() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404
