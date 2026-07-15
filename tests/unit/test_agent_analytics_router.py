"""GET /v1/analytics/agents: params, scoping, and cost visibility."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.routers.analytics import get_agent_analytics_repository, router
from apex.services.agent_analytics import _as_float
from apex.settings import ApexSettings, get_settings

ADMIN = ConsumerIdentity(
    consumer_id="admin-1", name="root", consumer_type=ConsumerType.INTERNAL, role=Role.ADMIN
)
OPERATOR = ConsumerIdentity(
    consumer_id="ops-1", name="ops", consumer_type=ConsumerType.DASHBOARD, role=Role.OPERATOR
)
ALICE = ConsumerIdentity(
    consumer_id="view-alice",
    name="alice",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.VIEWER,
    scopes=[ScopeRef(project_id="proj-a", app_id="app-a"), ScopeRef(project_id="proj-b")],
)

CANNED = {
    "totals": {
        "events": 4,
        "errors": 1,
        "input_tokens": 1200,
        "output_tokens": 360,
        "total_tokens": 1560,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 40,
        "reasoning_tokens": 80,
        "cost_usd": 0.012345,
        "avg_latency_ms": 1250.0,
        "p95_latency_ms": 2100.0,
        "runs": 2,
        "agents": 3,
        "models": 2,
    },
    "breakdown": [
        {
            "key": "claude-3-5-sonnet-latest",
            "thread_id": None,
            "events": 3,
            "errors": 1,
            "input_tokens": 1000,
            "output_tokens": 300,
            "total_tokens": 1300,
            "cache_read_tokens": 160,
            "cache_creation_tokens": 40,
            "reasoning_tokens": 60,
            "cost_usd": 0.01,
            "avg_latency_ms": 1200.0,
            "p95_latency_ms": 2000.0,
            "runs": 2,
        }
    ],
    "series": [
        {
            "bucket_start": datetime(2026, 6, 10, tzinfo=UTC),
            "key": "claude-3-5-sonnet-latest",
            "events": 3,
            "errors": 1,
            "input_tokens": 1000,
            "output_tokens": 300,
            "total_tokens": 1300,
            "cache_read_tokens": 160,
            "cache_creation_tokens": 40,
            "reasoning_tokens": 60,
            "cost_usd": 0.01,
            "avg_latency_ms": 1200.0,
            "p95_latency_ms": 2000.0,
            "runs": 2,
        }
    ],
    "page": {"limit": 20, "offset": 0, "total": 1},
}


def test_non_finite_legacy_aggregates_normalize_to_missing() -> None:
    for value in (
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ):
        assert _as_float(value) is None

    assert _as_float(Decimal("1.25")) == 1.25


class FakeAgentAnalyticsRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def aggregate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "totals": dict(CANNED["totals"]),
            "breakdown": [dict(row) for row in CANNED["breakdown"]],
            "series": [dict(row) for row in CANNED["series"]],
            "page": dict(CANNED["page"]),
        }


def make_client(
    repo: FakeAgentAnalyticsRepository,
    identity: ConsumerIdentity | None,
    *,
    cost_enabled: bool = False,
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_agent_analytics_repository] = lambda: repo
    app.dependency_overrides[get_settings] = lambda: ApexSettings(
        analytics_cost_visible=cost_enabled
    )
    if identity is not None:
        app.dependency_overrides[get_current_identity] = lambda: identity
    return TestClient(app)


def test_response_shape_defaults_and_cost_hidden() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get("/v1/analytics/agents")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"window", "totals", "breakdown", "series", "page", "cost_visible"}
    assert body["window"]["bucket"] == "day"
    assert body["window"]["group_by"] == "model"
    assert body["cost_visible"] is False
    assert body["totals"]["cost_usd"] is None
    assert body["breakdown"][0]["cost_usd"] is None
    assert body["series"][0]["cost_usd"] is None
    call = repo.calls[0]
    assert call["window_to"] - call["window_from"] == timedelta(days=7)
    assert call["group_by"] == "model"
    assert call["sort"] == "total_tokens"
    assert call["order"] == "desc"
    assert call["limit"] == 20
    assert call["offset"] == 0


def test_explicit_params_and_csv_filters_pass_through() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ADMIN, cost_enabled=True) as client:
        response = client.get(
            "/v1/analytics/agents",
            params={
                "from": "2026-06-01T00:00:00Z",
                "to": "2026-06-02T00:00:00Z",
                "bucket": "hour",
                "group_by": "stage",
                "model": "m1,m2",
                "stage": ["story_analysis", "reporting"],
                "agent": "story_analysis.worker",
                "test": "run-1",
                "status": "error",
                "sort": "p95_latency_ms",
                "order": "asc",
                "limit": 50,
                "offset": 10,
            },
        )
    assert response.status_code == 200
    call = repo.calls[0]
    assert call["window_from"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert call["window_to"] == datetime(2026, 6, 2, tzinfo=UTC)
    assert call["bucket"] == "hour"
    assert call["group_by"] == "stage"
    assert call["models"] == ("m1", "m2")
    assert call["stages"] == ("story_analysis", "reporting")
    assert call["agents"] == ("story_analysis.worker",)
    assert call["test"] == "run-1"
    assert call["status"] == "error"
    assert call["sort"] == "p95_latency_ms"
    assert call["order"] == "asc"
    assert call["limit"] == 50
    assert call["offset"] == 10
    assert response.json()["cost_visible"] is True
    assert response.json()["totals"]["cost_usd"] == 0.012345


def test_filter_fanout_and_large_offset_are_rejected_before_aggregation() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        too_many = client.get(
            "/v1/analytics/agents",
            params={"model": ",".join(f"m{index}" for index in range(51))},
        )
        huge_offset = client.get("/v1/analytics/agents", params={"offset": 10_001})

    assert too_many.status_code == 422
    assert huge_offset.status_code == 422
    assert repo.calls == []


def test_hourly_window_cap_is_rejected_before_aggregation() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/agents",
            params={
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-02-02T00:00:00Z",
                "bucket": "hour",
            },
        )

    assert response.status_code == 422
    assert repo.calls == []


def test_default_window_underflow_is_422_before_aggregation() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ADMIN) as client:
        response = client.get(
            "/v1/analytics/agents",
            params={"to": "0001-01-01T00:00:00Z"},
        )

    assert response.status_code == 422
    assert "default seven-day" in response.json()["title"]
    assert repo.calls == []


def test_cost_flag_does_not_show_cost_to_non_admin() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, OPERATOR, cost_enabled=True) as client:
        response = client.get("/v1/analytics/agents")
    assert response.status_code == 200
    assert response.json()["cost_visible"] is False
    assert response.json()["totals"]["cost_usd"] is None


def test_scoped_consumer_gets_visibility_filter() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        assert client.get("/v1/analytics/agents").status_code == 200
    assert repo.calls[0]["visible_scopes"] == tuple(ALICE.scopes)


def test_project_filter_inside_scope_is_passed_through() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        assert client.get("/v1/analytics/agents", params={"project": "proj-a"}).status_code == 200
    assert repo.calls[0]["project_id"] == "proj-a"


def test_project_filter_outside_scope_is_403() -> None:
    repo = FakeAgentAnalyticsRepository()
    with make_client(repo, ALICE) as client:
        response = client.get("/v1/analytics/agents", params={"project": "proj-z"})
    assert response.status_code == 403
    assert repo.calls == []


def test_requires_authentication() -> None:
    with make_client(FakeAgentAnalyticsRepository(), None) as client:
        response = client.get("/v1/analytics/agents")
    assert response.status_code == 401
