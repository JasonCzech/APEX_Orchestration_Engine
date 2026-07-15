"""Composition-root middleware contract regressions."""

from fastapi.testclient import TestClient

from apex.app.http import app


def test_cors_preflight_allows_authenticated_sse_resume_headers() -> None:
    # Constructing TestClient without entering it deliberately skips lifespan;
    # CORS preflight is answered entirely by the outer middleware.
    client = TestClient(app)

    response = client.options(
        "/threads/thread-1/runs/run-1/join",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "last-event-id,x-api-key",
        },
    )

    assert response.status_code == 200
    allowed = {
        header.strip().lower()
        for header in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"last-event-id", "x-api-key"}.issubset(allowed)
