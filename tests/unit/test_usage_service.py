"""Best-effort usage writers: DB failures are swallowed; phase bridge maps fields."""

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.persistence.models import AgentEvent, Base, UsageEvent
from apex.services import usage

# ── Swallow-and-log writers (hermetic settings point at an unreachable DB) ───


async def test_record_usage_event_swallows_db_errors() -> None:
    # conftest's hermetic_settings points APEX_DATABASE__URI at an unreachable
    # port — the write must fail silently, never raise.
    await usage.record_usage_event(
        consumer_name="dev", surface="v1", action="getSystemInfo", status="ok", duration_ms=3
    )


@pytest.mark.asyncio
async def test_record_agent_event_finish_reason_never_executes_scalar_truthiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileFinishReason:
        called = False

        def __bool__(self) -> bool:
            self.called = True
            raise AssertionError("finish-reason scalar truthiness must not execute")

    captured: list[dict[str, Any]] = []

    async def capture(_session_factory: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(usage, "_insert_agent_event", capture)
    hostile = HostileFinishReason()

    await usage.record_agent_event(
        thread_id="thread-1",
        project_id="project-1",
        app_id="app-1",
        phase="execution",
        agent_name="execution.worker",
        model="gpt-4o",
        provider="openai",
        attempt=1,
        status="ok",
        latency_ms=5,
        usage={"finish_reason": hostile, "stop_reason": "fallback-stop"},
    )

    assert hostile.called is False
    assert captured[0]["extra"]["finish_reason"] == "fallback-stop"


async def test_record_agent_event_never_invokes_hostile_scalar_hooks() -> None:
    calls: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile agent telemetry hook ran")

        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("hostile agent telemetry hook ran")

    await usage.record_agent_event(
        thread_id=HostileText("thread-secret"),
        project_id=None,
        app_id=None,
        phase=HostileText("execution"),
        agent_name=HostileText("execution.worker"),
        model=HostileText("provider-model"),
        provider=None,
        attempt=1,
        status="ok",
        latency_ms=1,
    )

    assert calls == []


def test_record_usage_event_sync_swallows_db_errors() -> None:
    usage.record_usage_event_sync(
        consumer_name="graph", surface="graph", action="phase:execution:failed", status="error"
    )


def test_record_phase_usage_sync_swallows_everything() -> None:
    usage.record_phase_usage_sync("execution", "succeeded", None)  # no config at all
    usage.record_phase_usage_sync("execution", "succeeded", {"configurable": None})


@pytest.mark.parametrize(
    "text_limits",
    [usage._USAGE_TEXT_LIMITS, usage._AGENT_TEXT_LIMITS],
)
def test_every_durable_event_scalar_is_credential_redacted(
    text_limits: dict[str, int],
) -> None:
    for field_name in text_limits:
        values: dict[str, Any] = {
            field_name: "Authorization: Bearer telemetry-secret",
            "extra": {},
        }

        sanitized = usage._sanitize_event_values(values, text_limits)

        assert "telemetry-secret" not in json.dumps(sanitized)


def test_durable_usage_extra_redacts_paths_keys_and_nested_auth_strings() -> None:
    values = {
        "action": "getThing",
        "duration_ms": 17,
        "extra": {
            "path": "/v1/things?sig=signed-secret&view=full",
            "password": "mapping-secret",
            "nested": {"finish_reason": "Basic encoded-secret"},
        },
    }

    sanitized = usage._sanitize_event_values(values, usage._USAGE_TEXT_LIMITS)
    encoded = json.dumps(sanitized, sort_keys=True)

    assert "signed-secret" not in encoded
    assert "mapping-secret" not in encoded
    assert "encoded-secret" not in encoded
    assert "[REDACTED]" in encoded
    assert "[redacted-credential-key]" in encoded
    assert sanitized["duration_ms"] == 17


def test_durable_agent_numeric_evidence_is_preserved() -> None:
    values = {
        "phase": "execution",
        "input_tokens": 41,
        "cost_usd": Decimal("0.123456"),
        "latency_ms": 88,
        "extra": {},
    }

    sanitized = usage._sanitize_event_values(values, usage._AGENT_TEXT_LIMITS)

    assert sanitized["input_tokens"] == 41
    assert sanitized["cost_usd"] == Decimal("0.123456")
    assert sanitized["latency_ms"] == 88


def test_event_scalar_sanitization_never_invokes_arbitrary_string_hooks() -> None:
    calls: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile telemetry string hook ran")

    sanitized = usage._sanitize_event_values(
        {"action": HostileText("provider-secret"), "extra": {}},
        usage._USAGE_TEXT_LIMITS,
    )

    assert calls == []
    assert "provider-secret" not in sanitized["action"]
    assert "unsupported-text" in sanitized["action"]


# ── Phase bridge field mapping (capture the inner sync writer) ───────────────


def _capture_sync_writer(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(usage, "record_usage_event_sync", fake)
    return captured


def test_phase_usage_succeeded_maps_to_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_sync_writer(monkeypatch)
    config = {
        "configurable": {
            "thread_id": "t-123",
            "project_id": "proj-a",
            "app_id": "app-a",
            "langgraph_auth_user": {"identity": "alice"},
        }
    }
    usage.record_phase_usage_sync("story_analysis", "succeeded", config)
    assert captured == [
        {
            "consumer_name": "alice",
            "surface": "graph",
            "action": "phase:story_analysis:succeeded",
            "status": "ok",
            "project_id": "proj-a",
            "app_id": "app-a",
            "thread_id": "t-123",
        }
    ]


@pytest.mark.parametrize(
    ("phase_status", "expected"),
    [("succeeded", "ok"), ("skipped", "ok"), ("failed", "error"), ("aborted", "error")],
)
def test_phase_usage_status_mapping(
    monkeypatch: pytest.MonkeyPatch, phase_status: str, expected: str
) -> None:
    captured = _capture_sync_writer(monkeypatch)
    usage.record_phase_usage_sync("execution", phase_status, {"configurable": {}})
    assert captured[0]["status"] == expected
    assert captured[0]["action"] == f"phase:execution:{phase_status}"


def test_phase_usage_defaults_consumer_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_sync_writer(monkeypatch)
    usage.record_phase_usage_sync("execution", "succeeded", {"configurable": {"thread_id": "t"}})
    assert captured[0]["consumer_name"] == "graph"
    assert captured[0]["project_id"] is None
    assert captured[0]["app_id"] is None


def test_phase_usage_rejects_hostile_checkpoint_scalars_without_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_sync_writer(monkeypatch)
    calls: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("hostile checkpoint string hook ran")

        def replace(self, *_args: object, **_kwargs: object) -> str:
            calls.append("replace")
            raise AssertionError("hostile checkpoint string hook ran")

    usage.record_phase_usage_sync(
        "execution",
        "succeeded",
        {
            "configurable": {
                "thread_id": HostileText("thread-secret"),
                "project_id": HostileText("project-secret"),
                "langgraph_auth_user": {"identity": HostileText("identity-secret")},
            }
        },
        attempt=1,
    )
    usage.record_phase_usage_sync(
        HostileText("execution"),
        "succeeded",
        {"configurable": {}},
    )

    assert calls == []
    assert captured == [
        {
            "consumer_name": "graph",
            "surface": "graph",
            "action": "phase:execution:succeeded",
            "status": "ok",
            "project_id": None,
            "app_id": None,
            "thread_id": None,
        }
    ]


def test_phase_usage_replay_key_is_stable_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_sync_writer(monkeypatch)
    config = {"configurable": {"thread_id": "t-1", "project_id": "p1"}}

    usage.record_phase_usage_sync("execution", "succeeded", config, attempt=2)
    usage.record_phase_usage_sync("execution", "succeeded", config, attempt=2)
    usage.record_phase_usage_sync("execution", "succeeded", config, attempt=3)

    assert captured[0]["event_key"] == captured[1]["event_key"]
    assert captured[0]["event_key"] != captured[2]["event_key"]


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (
            UsageEvent,
            {
                "event_key": "phase:stable",
                "consumer_name": "graph",
                "surface": "graph",
                "action": "phase:execution:succeeded",
                "status": "ok",
                "extra": {},
            },
        ),
        (
            AgentEvent,
            {
                "event_key": "agent:stable",
                "phase": "execution",
                "agent_name": "execution.worker",
                "status": "ok",
                "extra": {},
            },
        ),
    ],
)
def test_sqlite_native_insert_ignores_replayed_event_key(
    model: type[UsageEvent] | type[AgentEvent], values: dict[str, Any]
) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        table_name = "usage_events" if model is UsageEvent else "agent_events"
        Base.metadata.tables[f"apex.{table_name}"].create(connection)
        statement = usage._idempotent_insert(model, values, "sqlite")
        assert statement is not None
        connection.execute(statement)
        connection.execute(statement)
        count = connection.scalar(select(func.count()).select_from(model))
    engine.dispose()

    assert count == 1


# ── Lazy consumer attribution for the middleware path ────────────────────────


def test_request_consumer_name_uses_resolved_identity() -> None:
    identity = ConsumerIdentity(
        consumer_id="consumer-1",
        name="alice",
        consumer_type=ConsumerType.HEADLESS,
        role=Role.VIEWER,
    )
    scope = {
        "state": {"identity": identity},
        "headers": [(b"x-api-key", b"must-not-be-retained")],
    }

    assert usage._request_consumer_name(scope) == "alice"


def test_request_consumer_name_fingerprints_unresolved_key() -> None:
    name = usage._request_consumer_name(
        {"state": {}, "headers": [(b"x-api-key", b"not-a-real-key")]}
    )
    assert name.startswith("key:")
    assert len(name) == len("key:") + 12


def test_request_consumer_name_without_key_is_anonymous() -> None:
    assert usage._request_consumer_name({"state": {}, "headers": []}) == "anonymous"
