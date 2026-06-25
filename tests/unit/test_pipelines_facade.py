"""Facade mapping + /pipelines read routes against a fake loopback client."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.domain.pipeline import PHASE_ORDER
from apex.routers.pipelines import get_pipeline_read_service, router
from apex.services.pipeline_read import (
    PipelineReadService,
    build_phase_strip,
    map_thread_summary,
    pending_gates_from_state,
    pending_gates_from_thread,
)

JsonDict = dict[str, Any]


def _not_found() -> NotFoundError:
    request = httpx.Request("GET", "http://loopback/threads/x")
    return NotFoundError("not found", response=httpx.Response(404, request=request), body=None)


class FakeThreads:
    def __init__(self, threads: list[JsonDict], states: dict[str, JsonDict] | None = None):
        self.threads = threads
        self.states = states or {}
        self.search_calls: list[JsonDict] = []
        self.update_calls: list[JsonDict] = []

    async def search(self, **kwargs: Any) -> list[JsonDict]:
        self.search_calls.append(kwargs)
        results = self.threads
        metadata = kwargs.get("metadata")
        if metadata:
            results = [
                t
                for t in results
                if all((t.get("metadata") or {}).get(k) == v for k, v in metadata.items())
            ]
        if kwargs.get("status"):
            results = [t for t in results if t.get("status") == kwargs["status"]]
        offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 10)
        return results[offset : offset + limit]

    async def get(self, thread_id: str) -> JsonDict:
        for thread in self.threads:
            if thread["thread_id"] == thread_id:
                return thread
        raise _not_found()

    async def get_state(self, thread_id: str) -> JsonDict:
        try:
            return self.states[thread_id]
        except KeyError:
            raise _not_found() from None

    async def update_state(self, thread_id: str, values: JsonDict, **_: Any) -> JsonDict:
        state = await self.get_state(thread_id)
        current = state.setdefault("values", {})
        for key, value in values.items():
            if key in ("prompt_reviews", "application_reviews") and isinstance(value, dict):
                merged = dict(current.get(key) or {})
                merged.update(value)
                current[key] = merged
            else:
                current[key] = value
        self.update_calls.append({"thread_id": thread_id, "values": values})
        return {"checkpoint": {"thread_id": thread_id}}


class FakeRuns:
    def __init__(self) -> None:
        self.create_calls: list[JsonDict] = []

    async def list(self, thread_id: str, *, status: str | None = None, **_: Any) -> list:
        return []

    async def create(self, *args: Any, **kwargs: Any) -> JsonDict:
        self.create_calls.append({"args": args, "kwargs": kwargs})
        return {"run_id": "run-1"}

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        return None


class FakeClient:
    def __init__(self, threads: FakeThreads):
        self.threads = threads
        self.runs = FakeRuns()


GATE_PAYLOAD = {
    "schema_version": 1,
    "kind": "prompt_review",
    "phase": "test_planning",
    "actions": ["approve", "modify", "skip_phase", "abort"],
}

THREAD_INTERRUPTED = {
    "thread_id": "t-1",
    "status": "interrupted",
    "created_at": "2026-06-01T00:00:00+00:00",
    "updated_at": "2026-06-02T00:00:00+00:00",
    "metadata": {"project_id": "p1", "app_id": "a1"},
    "values": {
        "title": "Checkout regression",
        "current_phase": "test_planning",
        "phase_results": {
            "story_analysis": {"status": "succeeded", "attempt": 1},
            "test_planning": {"status": "awaiting_prompt_review", "attempt": 2},
        },
    },
    "interrupts": {"task-1": [{"id": "int-1", "value": GATE_PAYLOAD}]},
}

THREAD_IDLE = {
    "thread_id": "t-2",
    "status": "idle",
    "created_at": "2026-06-03T00:00:00+00:00",
    "updated_at": "2026-06-03T01:00:00+00:00",
    "metadata": {"project_id": "p2", "title": "Metadata title"},
    "values": {},
    "interrupts": {},
}


def operator_identity() -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="op",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
    )


def make_app(client: FakeClient, identity: ConsumerIdentity | None = None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_pipeline_read_service] = lambda: PipelineReadService(client)
    if identity is not None:
        app.dependency_overrides[get_current_identity] = lambda: identity
    return app


# ── Pure mapping ─────────────────────────────────────────────────────────────


def test_phase_strip_canonical_order_and_none_filling() -> None:
    strip = build_phase_strip(THREAD_INTERRUPTED["values"])
    assert [entry["phase"] for entry in strip] == [p.value for p in PHASE_ORDER]
    by_phase = {entry["phase"]: entry for entry in strip}
    assert by_phase["story_analysis"] == {
        "phase": "story_analysis",
        "status": "succeeded",
        "attempt": 1,
    }
    assert by_phase["test_planning"]["status"] == "awaiting_prompt_review"
    assert by_phase["test_planning"]["attempt"] == 2
    assert by_phase["execution"] == {"phase": "execution", "status": "none", "attempt": None}


def test_phase_strip_handles_missing_values() -> None:
    strip = build_phase_strip(None)
    assert len(strip) == len(PHASE_ORDER)
    assert all(entry["status"] == "none" for entry in strip)


def test_pending_gate_from_thread_interrupts_mapping() -> None:
    gates = pending_gates_from_thread(THREAD_INTERRUPTED)
    assert gates == [
        {
            "interrupt_id": "int-1",
            "kind": "prompt_review",
            "phase": "test_planning",
            "payload": GATE_PAYLOAD,
        }
    ]
    assert pending_gates_from_thread(THREAD_IDLE) == []


def test_pending_gates_from_state_prefers_tasks_then_top_level() -> None:
    state = {"tasks": [{"interrupts": [{"id": "int-9", "value": GATE_PAYLOAD}]}], "interrupts": []}
    assert pending_gates_from_state(state)[0]["interrupt_id"] == "int-9"
    flat = {"tasks": [], "interrupts": [{"id": "int-8", "value": GATE_PAYLOAD}]}
    assert pending_gates_from_state(flat)[0]["interrupt_id"] == "int-8"
    assert pending_gates_from_state({}) == []


def test_map_thread_summary_shapes_pipeline_row() -> None:
    summary = map_thread_summary(THREAD_INTERRUPTED)
    assert summary["thread_id"] == "t-1"
    assert summary["title"] == "Checkout regression"
    assert summary["project_id"] == "p1"
    assert summary["app_id"] == "a1"
    assert summary["thread_status"] == "interrupted"
    assert summary["current_phase"] == "test_planning"
    assert summary["pending_gate"] == {
        "interrupt_id": "int-1",
        "kind": "prompt_review",
        "phase": "test_planning",
    }


def test_map_thread_summary_falls_back_to_metadata_title() -> None:
    summary = map_thread_summary(THREAD_IDLE)
    assert summary["title"] == "Metadata title"
    assert summary["pending_gate"] is None


# ── Routes ───────────────────────────────────────────────────────────────────


def test_list_pipelines_returns_mapped_items() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines")
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 20 and body["offset"] == 0
    assert [item["thread_id"] for item in body["items"]] == ["t-1", "t-2"]
    item = body["items"][0]
    assert item["pending_gate"]["interrupt_id"] == "int-1"
    assert len(item["phase_strip"]) == len(PHASE_ORDER)


def test_list_pipelines_passes_project_filter_to_metadata_search() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"project": "p2", "status": "idle"})
    assert response.status_code == 200
    assert [item["thread_id"] for item in response.json()["items"]] == ["t-2"]
    assert fake.search_calls[0]["metadata"] == {"project_id": "p2"}
    assert fake.search_calls[0]["status"] == "idle"
    assert fake.search_calls[0]["sort_by"] == "updated_at"


def test_list_pipelines_q_filters_current_page_by_title() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"q": "checkout"})
    assert [item["thread_id"] for item in response.json()["items"]] == ["t-1"]


def test_get_pipeline_returns_state_values_and_interrupts() -> None:
    state = {
        "values": THREAD_INTERRUPTED["values"],
        "tasks": [{"interrupts": [{"id": "int-1", "value": GATE_PAYLOAD}]}],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/t-1")
    assert response.status_code == 200
    body = response.json()
    assert body["values"]["title"] == "Checkout regression"
    assert body["interrupts"][0]["interrupt_id"] == "int-1"
    assert body["interrupts"][0]["payload"]["actions"] == GATE_PAYLOAD["actions"]
    assert body["pending_gate"]["interrupt_id"] == "int-1"


def test_get_pipeline_unknown_thread_is_404_problem() -> None:
    fake = FakeThreads([])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_get_phase_prompt_review_uses_state_draft() -> None:
    state = {
        "values": {
            "prompt_reviews": {
                "story_analysis": {
                    "system": "S",
                    "phase_prompt": "P",
                    "application": None,
                    "additional_context": "C",
                    "source": {"origin": "run_override"},
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "updated_by": "op",
                }
            }
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/t-1/phases/story_analysis/prompt-review")
    assert response.status_code == 200
    assert response.json()["phase_prompt"] == "P"
    assert response.json()["additional_context"] == "C"


def test_get_phase_prompt_review_falls_back_to_resolved_prompt() -> None:
    state = {
        "values": {
            "phase_results": {
                "story_analysis": {
                    "resolved_prompt": {"system": "S", "user": "U", "application": "A"},
                    "resolved_prompt_source": {"origin": "catalog", "ref": "phase/story@v1"},
                    "started_at": "2026-06-01T00:00:00+00:00",
                }
            }
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/t-1/phases/story_analysis/prompt-review")
    assert response.status_code == 200
    body = response.json()
    assert body["system"] == "S"
    assert body["phase_prompt"] == "U"
    assert body["source"]["ref"] == "phase/story@v1"


def test_patch_phase_prompt_review_updates_state_without_run() -> None:
    state = {"values": {}, "tasks": [], "interrupts": []}
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    client_obj = FakeClient(fake)
    app = make_app(client_obj, operator_identity())
    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "C",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["source"]["origin"] == "run_override"
    assert body["updated_by"] == "op"
    assert fake.update_calls[0]["values"]["prompt_reviews"]["story_analysis"]["phase_prompt"] == "P"
    assert client_obj.runs.create_calls == []


def test_patch_phase_prompt_review_invalid_phase_is_422() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": {"values": {}}})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/not_a_phase/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )
    assert response.status_code == 422


def test_patch_phase_prompt_review_requires_operator() -> None:
    viewer = ConsumerIdentity(
        consumer_id="v1",
        name="viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
    )
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": {"values": {}}})
    app = make_app(FakeClient(fake), viewer)
    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )
    assert response.status_code == 403


def _seeded_review(system: str, application: str | None) -> JsonDict:
    return {
        "system": system,
        "phase_prompt": "P",
        "application": application,
        "additional_context": "",
        "source": {"origin": "catalog"},
        "updated_at": "2026-06-01T00:00:00+00:00",
        "updated_by": "system",
    }


def test_patch_application_prompt_is_app_wide() -> None:
    state = {
        "values": {
            "prompt_reviews": {
                "story_analysis": _seeded_review("S1", "App catalog"),
                "test_planning": _seeded_review("S2", "App catalog"),
            }
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})  # metadata app_id="a1"
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        patched = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S1",
                "phase_prompt": "P",
                "application": "Edited app prompt",
                "additional_context": "",
            },
        )
        assert patched.status_code == 200
        assert patched.json()["application"] == "Edited app prompt"
        # The app-wide override is visible from a DIFFERENT phase.
        other = client.get("/v1/pipelines/t-1/phases/test_planning/prompt-review")
    assert other.status_code == 200
    assert other.json()["application"] == "Edited app prompt"
    # Stored once under application_reviews[app_id], not duplicated per-phase.
    stored = fake.states["t-1"]["values"]["application_reviews"]["a1"]
    assert stored["content"] == "Edited app prompt"


def test_patch_application_unchanged_does_not_write_override() -> None:
    state = {
        "values": {"prompt_reviews": {"story_analysis": _seeded_review("S1", "App catalog")}},
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "Edited system",
                "phase_prompt": "P",
                "application": "App catalog",  # unchanged
                "additional_context": "",
            },
        )
    assert response.status_code == 200
    # A system-only edit must not freeze the app prompt as a run override.
    assert "application_reviews" not in fake.states["t-1"]["values"]


def test_patch_null_application_is_truthful_and_preserves_override() -> None:
    state = {
        "values": {
            "prompt_reviews": {"story_analysis": _seeded_review("S1", "Edited app")},
            "application_reviews": {
                "a1": {
                    "content": "Edited app",
                    "source": {"origin": "run_override"},
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "updated_by": "op",
                }
            },
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        # A null application (e.g. a system-only edit) must not make the response claim
        # null while the app-wide override silently persists. The response reflects the
        # effective value, and a later GET agrees.
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S1 edited",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )
        assert response.status_code == 200
        assert response.json()["application"] == "Edited app"
        later = client.get("/v1/pipelines/t-1/phases/test_planning/prompt-review")
    assert later.json()["application"] == "Edited app"
    assert fake.states["t-1"]["values"]["application_reviews"]["a1"]["content"] == "Edited app"


def test_patch_phase_prompt_review_unknown_thread_is_404() -> None:
    fake = FakeThreads([], states={})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/missing/phases/story_analysis/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )
    assert response.status_code == 404


def test_get_phase_prompt_review_unknown_thread_is_404() -> None:
    fake = FakeThreads([], states={})
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/missing/phases/story_analysis/prompt-review")
    assert response.status_code == 404


def test_list_pipelines_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", "k")  # auth on; no key sent -> 401
    app = make_app(FakeClient(FakeThreads([])), identity=None)
    with TestClient(app) as client:
        response = client.get("/v1/pipelines")
    assert response.status_code == 401
