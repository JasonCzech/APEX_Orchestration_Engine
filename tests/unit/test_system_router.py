import pytest
from fastapi.testclient import TestClient

from apex.app.http import app

DEV_KEY = "system-info-dev-key"


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
    assert set(body) == {"name", "version", "environment", "features", "consumer"}
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
