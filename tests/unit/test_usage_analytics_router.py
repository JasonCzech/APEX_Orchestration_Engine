"""GET /v1/analytics/usage: shapes, windowing, bucket validation, and scoping."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.analytics import get_usage_analytics_repository, router

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
ALICE = ConsumerIdentity(  # one app scope plus one project-wide scope
    consumer_id="view-alice",
    name="alice",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.VIEWER,
    scopes=[ScopeRef(project_id="proj-a", app_id="app-a"), ScopeRef(project_id="proj-b")],
)

CANNED = {
    "totals": {"events": 12, "errors": 2, "by_surface": {"v1": 9, "graph": 3}},
    "buckets": [
        {"bucket_start": datetime(2026, 6, 10, tzinfo=UTC), "events": 7, "errors": 1},
        {"bucket_start": datetime(2026, 6, 11, tzinfo=UTC), "events": 5, "errors": 1},
    ],
    "top_actions": [
        {"action": "getSystemInfo", "count": 6},
        {"action": "phase:execution:succeeded", "count": 3},
    ],
    "runs": {"phases_succeeded": 3, "phases_failed": 0},
}


class FakeUsageAnalyticsRepository:
    """Records the aggregate() kwargs and returns a canned aggregate."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def aggregate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return CANNED


def make_client(
    repo: FakeUsageAnalyticsRepository, identity: ConsumerIdentity | None
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_usage_analytics_repository] = lambda: repo
    if identity is not None:
        app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


def test_response_shape_and_defaults() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get("/v1/analytics/usage")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"window", "totals", "buckets", "top_actions", "runs"}
    assert set(body["window"]) == {"from", "to", "bucket"}
    assert body["window"]["bucket"] == "day"
    assert body["totals"] == {"events": 12, "errors": 2, "by_surface": {"v1": 9, "graph": 3}}
    assert [b["events"] for b in body["buckets"]] == [7, 5]
    assert body["top_actions"][0] == {"action": "getSystemInfo", "count": 6}
    assert body["runs"] == {"phases_succeeded": 3, "phases_failed": 0}
    # Default window: last 7 days, bucketed by day, unscoped admin -> no filters.
    call = repo.calls[0]
    assert call["window_to"] - call["window_from"] == timedelta(days=7)
    assert call["bucket"] == "day"
    assert call["project_id"] is None
    assert call["visible_scopes"] is None


def test_explicit_window_and_hour_bucket_pass_through() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/usage",
            params={
                "from": "2026-06-01T00:00:00Z",
                "to": "2026-06-02T00:00:00Z",
                "bucket": "hour",
            },
        )
    assert response.status_code == 200
    call = repo.calls[0]
    assert call["window_from"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert call["window_to"] == datetime(2026, 6, 2, tzinfo=UTC)
    assert call["bucket"] == "hour"
    assert response.json()["window"]["bucket"] == "hour"


def test_naive_datetimes_are_taken_as_utc() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/usage",
            params={"from": "2026-06-01T00:00:00", "to": "2026-06-02T00:00:00"},
        )
    assert response.status_code == 200
    assert repo.calls[0]["window_from"].tzinfo is not None


def test_invalid_bucket_is_422() -> None:
    with make_client(FakeUsageAnalyticsRepository(), ADMIN) as client:
        response = client.get("/v1/analytics/usage", params={"bucket": "week"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_from_after_to_is_422() -> None:
    with make_client(FakeUsageAnalyticsRepository(), ADMIN) as client:
        response = client.get(
            "/v1/analytics/usage",
            params={"from": "2026-06-03T00:00:00Z", "to": "2026-06-02T00:00:00Z"},
        )
    assert response.status_code == 422


def test_default_window_underflow_is_422_before_aggregation() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/usage",
            params={"to": "0001-01-01T00:00:00Z"},
        )

    assert response.status_code == 422
    assert "default seven-day" in response.json()["title"]
    assert repo.calls == []


def test_oversized_analytics_window_is_rejected_before_aggregation() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/usage",
            params={
                "from": "2025-01-01T00:00:00Z",
                "to": "2026-07-01T00:00:00Z",
                "bucket": "day",
            },
        )

    assert response.status_code == 422
    assert repo.calls == []


def test_scoped_consumer_gets_visibility_filter() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        assert client.get("/v1/analytics/usage").status_code == 200
    assert repo.calls[0]["visible_scopes"] == tuple(ALICE.scopes)


def test_project_filter_inside_scope_is_passed_through() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        assert client.get("/v1/analytics/usage", params={"project": "proj-a"}).status_code == 200
    assert repo.calls[0]["project_id"] == "proj-a"


def test_project_filter_outside_scope_is_403() -> None:
    repo = FakeUsageAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        response = client.get("/v1/analytics/usage", params={"project": "proj-z"})
    assert response.status_code == 403
    assert repo.calls == []  # rejected before any aggregation


def test_requires_authentication() -> None:
    # No identity override: the real dependency runs against hermetic settings
    # (auth on, no dev key, unreachable DB) and rejects.
    with make_client(FakeUsageAnalyticsRepository(), None) as client:
        response = client.get("/v1/analytics/usage")
    assert response.status_code == 401
