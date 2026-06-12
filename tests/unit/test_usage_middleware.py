"""UsageTrackingMiddleware on the real /v1 app, with the DB writer faked.

The middleware schedules the write fire-and-forget on the TestClient's portal
loop, so assertions poll briefly for the captured event instead of awaiting it.
"""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apex.app.http import app
from apex.services import usage

DEV_KEY = "usage-mw-dev-key"


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the best-effort DB writer with an in-memory capture."""
    captured: list[dict[str, Any]] = []

    async def fake_record(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(usage, "record_usage_event", fake_record)
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    return captured


def wait_for_events(captured: list[dict[str, Any]], count: int, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and len(captured) < count:
        time.sleep(0.01)
    assert len(captured) >= count, f"expected {count} usage events, got {len(captured)}"


def test_records_operation_id_status_and_duration(events: list[dict[str, Any]]) -> None:
    with TestClient(app) as client:
        response = client.get("/v1/system/info", headers={"x-api-key": DEV_KEY})
        assert response.status_code == 200
        wait_for_events(events, 1)
    event = events[0]
    assert event["action"] == "getSystemInfo"
    assert event["surface"] == "v1"
    assert event["status"] == "ok"
    assert event["consumer_name"] == "dev"  # lazily re-resolved from the api key header
    assert isinstance(event["duration_ms"], int) and event["duration_ms"] >= 0
    assert event["extra"]["status_code"] == 200
    assert event["extra"]["path"] == "/v1/system/info"
    assert event["project_id"] is None


def test_no_event_for_openapi_docs_or_unmatched_paths(events: list[dict[str, Any]]) -> None:
    with TestClient(app) as client:
        assert client.get("/v1/openapi.json").status_code == 200
        assert client.get("/v1/nope").status_code == 404
        # A matched operation afterwards proves the pipeline works; if the two
        # requests above had emitted, they would land before this one.
        assert client.get("/v1/system/info", headers={"x-api-key": DEV_KEY}).status_code == 200
        wait_for_events(events, 1)
        time.sleep(0.05)  # grace period for any stray events
    assert [e["action"] for e in events] == ["getSystemInfo"]


def test_401_is_recorded_as_error_with_anonymous_consumer(
    events: list[dict[str, Any]],
) -> None:
    with TestClient(app) as client:
        assert client.get("/v1/system/info").status_code == 401
        wait_for_events(events, 1)
    event = events[0]
    assert event["action"] == "getSystemInfo"  # the route matched; the dependency rejected
    assert event["status"] == "error"
    assert event["consumer_name"] == "anonymous"
    assert event["extra"]["status_code"] == 401


def test_unresolvable_key_records_fingerprint(events: list[dict[str, Any]]) -> None:
    # Unknown key + hermetic (unreachable) DB: resolution fails, so the event
    # carries a sha256 fingerprint instead of a consumer name.
    with TestClient(app) as client:
        assert (
            client.get("/v1/system/info", headers={"x-api-key": "who-is-this"}).status_code == 401
        )
        wait_for_events(events, 1)
    assert events[0]["consumer_name"].startswith("key:")


def test_project_query_param_attributes_event(events: list[dict[str, Any]]) -> None:
    with TestClient(app) as client:
        client.get("/v1/system/info", params={"project": "proj-a"}, headers={"x-api-key": DEV_KEY})
        wait_for_events(events, 1)
    assert events[0]["project_id"] == "proj-a"
