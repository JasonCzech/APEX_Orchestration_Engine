from fastapi.testclient import TestClient

from apex.app.http import app


def test_system_info() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/system/info")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "APEX Orchestration Engine"
    assert set(body) == {"name", "version", "environment", "features"}


def test_unknown_route_returns_problem_details() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404
