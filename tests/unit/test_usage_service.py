"""Best-effort usage writers: DB failures are swallowed; phase bridge maps fields."""

from typing import Any

import pytest

from apex.services import usage

# ── Swallow-and-log writers (hermetic settings point at an unreachable DB) ───


async def test_record_usage_event_swallows_db_errors() -> None:
    # conftest's hermetic_settings points APEX_DATABASE__URI at an unreachable
    # port — the write must fail silently, never raise.
    await usage.record_usage_event(
        consumer_name="dev", surface="v1", action="getSystemInfo", status="ok", duration_ms=3
    )


def test_record_usage_event_sync_swallows_db_errors() -> None:
    usage.record_usage_event_sync(
        consumer_name="graph", surface="graph", action="phase:execution:failed", status="error"
    )


def test_record_phase_usage_sync_swallows_everything() -> None:
    usage.record_phase_usage_sync("execution", "succeeded", None)  # no config at all
    usage.record_phase_usage_sync("execution", "succeeded", {"configurable": None})


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


# ── Lazy consumer attribution for the middleware path ────────────────────────


async def test_resolve_consumer_name_dev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "usage-dev-key")
    from apex.settings import get_settings

    get_settings.cache_clear()
    assert await usage._resolve_consumer_name("usage-dev-key") == "dev"


async def test_resolve_consumer_name_falls_back_to_fingerprint() -> None:
    # Unknown key + unreachable DB -> resolver returns None -> fingerprint.
    name = await usage._resolve_consumer_name("not-a-real-key")
    assert name.startswith("key:")
    assert len(name) == len("key:") + 12


async def test_resolve_consumer_name_no_key_is_anonymous() -> None:
    assert await usage._resolve_consumer_name(None) == "anonymous"
