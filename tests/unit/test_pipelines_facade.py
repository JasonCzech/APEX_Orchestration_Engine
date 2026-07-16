"""Facade mapping + /pipelines read routes against a fake loopback client."""

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph_sdk.errors import NotFoundError

from apex.app.dependencies import get_current_identity
from apex.app.errors import register_exception_handlers
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.gates import build_prompt_review_payload
from apex.routers.pipelines import get_pipeline_read_service, router
from apex.services import pipeline_read as pipeline_read_module
from apex.services.pipeline_public import (
    MAX_PUBLIC_GATE_BYTES,
    MAX_PUBLIC_PIPELINE_STATE_BYTES,
    public_gate,
    public_pipeline_state,
)
from apex.services.pipeline_read import (
    MAX_PIPELINE_QUERY_CHARS,
    MAX_PIPELINE_TEXT_SCAN_RECORDS,
    MAX_PUBLIC_PENDING_GATES,
    MAX_PUBLIC_PENDING_GATES_BYTES,
    MAX_PUBLIC_PENDING_GATES_NODES,
    PIPELINE_SUMMARY_EXTRACT,
    PIPELINE_SUMMARY_SELECT,
    PIPELINE_TEXT_SCAN_PAGE_SIZE,
    PipelineReadService,
    build_phase_strip,
    map_thread_summary,
    pending_gates_from_state,
    pending_gates_from_thread,
)

JsonDict = dict[str, Any]
_MISSING = object()


@pytest.mark.parametrize(
    "malformed",
    [
        "password=malformed-config-canary",
        ["password=malformed-config-canary"],
    ],
)
def test_public_pipeline_state_omits_non_mapping_legacy_run_config(malformed: object) -> None:
    projected = public_pipeline_state({"run_config": malformed})

    assert "run_config" not in projected
    assert "malformed-config-canary" not in json.dumps(projected)


def test_public_pipeline_state_safely_handles_cyclic_legacy_run_config() -> None:
    malformed: JsonDict = {"password": "cyclic-config-canary"}
    malformed["cycle"] = malformed

    projected = public_pipeline_state({"run_config": malformed})

    assert "cyclic-config-canary" not in json.dumps(projected)


def test_public_pipeline_state_bounds_nonpublic_run_config_before_model_validation() -> None:
    oversized = {
        "title": "safe public title",
        "prompt_overrides": {"story_analysis": "x" * 5_000_001},
    }

    projected = public_pipeline_state({"title": "visible", "run_config": oversized})

    assert projected == {"title": "visible", "current_phase": None}


def test_public_projections_quarantine_unhashable_and_huge_legacy_scalars() -> None:
    projected = public_pipeline_state(
        {
            "title": "visible",
            "phases_plan": [["not-a-phase"]],
            "current_phase": ["not-a-phase"],
            "phase_results": {
                "execution": {
                    "status": ["succeeded"],
                    "attempt": 1 << 10_000,
                    "duration_s": 1 << 10_000,
                    "resolved_prompt_source": {"origin": ["catalog"]},
                    "approvals": [{"id": "a-1", "gate": ["prompt_review"], "action": "approve"}],
                    "tool_calls": [{"id": "t-1", "tool": "fetch", "status": ["ok"]}],
                }
            },
        }
    )
    gate = public_gate(
        {
            "interrupt_id": "interrupt-1",
            "kind": ["prompt_review"],
            "phase": ["execution"],
            "payload": {"actions": [["approve"]]},
        },
        include_payload=True,
    )

    assert projected == {
        "title": "visible",
        "phase_results": {"execution": {"phase": "execution"}},
    }
    assert gate is None


@pytest.mark.parametrize(
    "kind",
    [
        "engine_provision_retry",
        "engine_cleanup_retry",
        "engine_collection_retry",
        "engine_collection_settle_retry",
    ],
)
def test_public_gate_projects_engine_recovery_interrupts(kind: str) -> None:
    gate = public_gate(
        {
            "interrupt_id": "recovery-interrupt-1",
            "kind": kind,
            "phase": "execution",
            "payload": {
                "schema_version": 1,
                "kind": kind,
                "phase": "execution",
                "attempt": 3,
                "thread_id": "internal-thread-id",
                "actions": ["retry"],
                "error": "provider temporarily unavailable",
                "message": "Resume the exact durable provider attempt.",
                "connection_id": "must-not-project",
            },
        },
        include_payload=True,
    )

    assert gate == {
        "interrupt_id": "recovery-interrupt-1",
        "kind": kind,
        "phase": "execution",
        "payload": {
            "schema_version": 1,
            "kind": kind,
            "phase": "execution",
            "actions": ["retry"],
            "attempt": 3,
            "thread_id": "internal-thread-id",
            "message": "Resume the exact durable provider attempt.",
            "error": "provider temporarily unavailable",
        },
    }


def test_public_gate_rejects_engine_recovery_interrupt_on_another_phase() -> None:
    assert (
        public_gate(
            {
                "interrupt_id": "recovery-interrupt-1",
                "kind": "engine_cleanup_retry",
                "phase": "reporting",
                "payload": {},
            },
            include_payload=True,
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        pytest.param("schema_version", True, id="schema-version-bool"),
        pytest.param("schema_version", 2, id="schema-version-unknown"),
        pytest.param("kind", "phase_review", id="payload-kind-mismatch"),
        pytest.param("phase", "reporting", id="payload-phase-mismatch"),
        pytest.param("actions", _MISSING, id="actions-missing"),
        pytest.param("actions", [], id="actions-empty"),
        pytest.param("actions", ["approve"], id="actions-wrong"),
        pytest.param("actions", ["retry", "retry"], id="actions-duplicate"),
        pytest.param("attempt", True, id="attempt-bool"),
        pytest.param("attempt", 0, id="attempt-low"),
        pytest.param("attempt", 1_000_001, id="attempt-high"),
        pytest.param("thread_id", _MISSING, id="thread-id-missing"),
        pytest.param("thread_id", "", id="thread-id-empty"),
        pytest.param("thread_id", "x" * 256, id="thread-id-long"),
        pytest.param("message", _MISSING, id="message-missing"),
        pytest.param("message", "x" * 4_097, id="message-long"),
        pytest.param("error", 42, id="error-non-string"),
        pytest.param("error", "x" * 4_097, id="error-long"),
    ],
)
def test_public_gate_rejects_malformed_engine_recovery_contract(
    field: str,
    malformed: object,
) -> None:
    payload: JsonDict = {
        "schema_version": 1,
        "kind": "engine_cleanup_retry",
        "phase": "execution",
        "attempt": 3,
        "thread_id": "thread-1",
        "actions": ["retry"],
        "message": "Resume the exact durable provider attempt.",
        "error": "provider temporarily unavailable",
    }
    if malformed is _MISSING:
        payload.pop(field)
    else:
        payload[field] = malformed

    assert (
        public_gate(
            {
                "interrupt_id": "recovery-interrupt-1",
                "kind": "engine_cleanup_retry",
                "phase": "execution",
                "payload": payload,
            },
            include_payload=True,
        )
        is None
    )


def test_public_gate_allows_engine_recovery_error_to_be_absent() -> None:
    gate = public_gate(
        {
            "interrupt_id": "recovery-interrupt-1",
            "kind": "engine_cleanup_retry",
            "phase": "execution",
            "payload": {
                "schema_version": 1,
                "kind": "engine_cleanup_retry",
                "phase": "execution",
                "attempt": 3,
                "thread_id": "thread-1",
                "actions": ["retry"],
                "message": "Resume the exact durable provider attempt.",
            },
        },
        include_payload=True,
    )

    assert gate is not None
    assert "error" not in gate["payload"]


def test_public_gate_rejects_hostile_recovery_scalars_without_comparing_them() -> None:
    class HostileEquality:
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("untyped recovery fields must not be compared")

    hostile = HostileEquality()
    for field, malformed in (
        ("kind", hostile),
        ("phase", hostile),
        ("actions", [hostile]),
    ):
        payload: JsonDict = {
            "schema_version": 1,
            "kind": "engine_cleanup_retry",
            "phase": "execution",
            "attempt": 3,
            "thread_id": "thread-1",
            "actions": ["retry"],
            "message": "Resume the exact durable provider attempt.",
        }
        payload[field] = malformed

        assert (
            public_gate(
                {
                    "interrupt_id": "recovery-interrupt-1",
                    "kind": "engine_cleanup_retry",
                    "phase": "execution",
                    "payload": payload,
                },
                include_payload=True,
            )
            is None
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "kind": "prompt_review",
            "phase": "execution",
            "actions": ["approve", "modify", "skip_phase", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "phase_review",
            "phase": "execution",
            "actions": ["approve", "revise", "discuss", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "prompt_review",
            "phase": "execution",
            "prompt": {
                "system": None,
                "user": " ",
                "application": None,
                "source": {"origin": "catalog", "ref": None},
            },
            "additional_context": "",
            "context_packets": [],
            "tools": [],
            "editable": True,
            "actions": ["approve", "modify", "skip_phase", "abort"],
        },
        {
            "schema_version": 1,
            "kind": "phase_review",
            "phase": "execution",
            "summary": None,
            "result_preview": {"summary": None, "reasoning_digest": None},
            "artifacts": [],
            "warnings": [],
            "dialogue_tail": [],
            "actions": ["approve", "revise", "discuss", "abort"],
        },
    ],
)
def test_public_gate_rejects_incomplete_or_empty_human_review_contract(
    payload: JsonDict,
) -> None:
    assert (
        public_gate(
            {
                "interrupt_id": "review-interrupt-1",
                "kind": payload["kind"],
                "phase": payload["phase"],
                "payload": payload,
            },
            include_payload=True,
        )
        is None
    )


def test_public_gate_preserves_complete_nullable_review_fields() -> None:
    payload: JsonDict = {
        "schema_version": 1,
        "kind": "prompt_review",
        "phase": "execution",
        "prompt": {
            "system": "system",
            "user": "user",
            "application": None,
            "source": {"origin": "catalog", "ref": None},
        },
        "additional_context": "",
        "context_packets": [
            {"id": "packet-1", "source": "jira", "title": "Story", "summary": None}
        ],
        "tools": [],
        "editable": True,
        "actions": ["approve", "modify", "skip_phase", "abort"],
    }

    gate = public_gate(
        {
            "interrupt_id": "review-interrupt-1",
            "kind": "prompt_review",
            "phase": "execution",
            "payload": payload,
        },
        include_payload=True,
    )

    assert gate is not None
    assert gate["payload"]["prompt"]["application"] is None
    assert gate["payload"]["prompt"]["source"]["ref"] is None
    assert gate["payload"]["context_packets"][0]["summary"] is None

    phase_payload: JsonDict = {
        "schema_version": 1,
        "kind": "phase_review",
        "phase": "execution",
        "summary": None,
        "result_preview": {"summary": None, "reasoning_digest": None},
        "artifacts": [],
        "warnings": ["Review the provider warning."],
        "dialogue_tail": [],
        "actions": ["approve", "revise", "discuss", "abort"],
    }
    phase_gate = public_gate(
        {
            "interrupt_id": "phase-review-interrupt-1",
            "kind": "phase_review",
            "phase": "execution",
            "payload": phase_payload,
        },
        include_payload=True,
    )

    assert phase_gate is not None
    assert phase_gate["payload"]["summary"] is None
    assert phase_gate["payload"]["result_preview"] == {
        "summary": None,
        "reasoning_digest": None,
    }


def test_public_gate_keeps_generated_explicitly_blank_prompt_override() -> None:
    payload = build_prompt_review_payload(
        Phase.EXECUTION,
        {"system": "", "user": "", "application": None},
        {"origin": "run_override", "ref": None},
        [],
        [],
    )

    gate = public_gate(
        {
            "interrupt_id": "blank-prompt-review-1",
            "kind": "prompt_review",
            "phase": "execution",
            "payload": payload,
        },
        include_payload=True,
    )

    assert gate is not None
    assert gate["payload"]["prompt"] == {
        "system": "",
        "user": "",
        "application": None,
        "source": {"origin": "run_override", "ref": None},
    }


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

    async def update_state(self, thread_id: str, values: JsonDict, **kwargs: Any) -> JsonDict:
        state = await self.get_state(thread_id)
        current = state.setdefault("values", {})
        for key, value in values.items():
            if key in ("prompt_reviews", "application_reviews") and isinstance(value, dict):
                merged = dict(current.get(key) or {})
                merged.update(value)
                current[key] = merged
            else:
                current[key] = value
        self.update_calls.append({"thread_id": thread_id, "values": values, **kwargs})
        return {"checkpoint": {"thread_id": thread_id}}


class FakeRuns:
    def __init__(self) -> None:
        self.create_calls: list[JsonDict] = []
        self.active_statuses: set[str] = set()
        self.list_calls: list[JsonDict] = []

    async def list(self, thread_id: str, *, status: str | None = None, **kwargs: Any) -> list:
        self.list_calls.append({"thread_id": thread_id, "status": status, **kwargs})
        if status in self.active_statuses:
            return [{"run_id": "active-run", "status": status}]
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


async def test_active_run_probe_uses_single_row_projection() -> None:
    client = FakeClient(FakeThreads([]))
    client.runs.active_statuses.add("running")

    assert await PipelineReadService(client)._has_active_run("thread-1") is True
    assert client.runs.list_calls == [
        {
            "thread_id": "thread-1",
            "status": "running",
            "limit": 1,
            "select": ["run_id", "status"],
        }
    ]


async def test_pipeline_list_rejects_native_page_above_requested_cap() -> None:
    class OversizedThreads(FakeThreads):
        async def search(self, **kwargs: Any) -> list[JsonDict]:
            return [dict(THREAD_IDLE), dict(THREAD_INTERRUPTED)]

    service = PipelineReadService(FakeClient(OversizedThreads([])))

    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        await service.list_pipelines(limit=1)


async def test_graph_only_abort_paginates_beyond_sdk_default_run_page() -> None:
    class PaginatedRuns:
        def __init__(self) -> None:
            self.records = {
                "running": [f"running-{index}" for index in range(17)],
                "pending": [f"pending-{index}" for index in range(14)],
            }
            self.list_calls: list[JsonDict] = []
            self.cancelled: list[str] = []

        async def list(
            self,
            _thread_id: str,
            *,
            status: str,
            limit: int,
            offset: int,
            **_kwargs: Any,
        ) -> list[JsonDict]:
            self.list_calls.append({"status": status, "limit": limit, "offset": offset})
            # Model a server that caps pages below the client's requested size.
            effective_limit = min(limit, 10)
            return [
                {"run_id": run_id, "status": status}
                for run_id in self.records[status][offset : offset + effective_limit]
            ]

        async def cancel(self, _thread_id: str, run_id: str) -> None:
            self.cancelled.append(run_id)
            for records in self.records.values():
                if run_id in records:
                    records.remove(run_id)

    client = FakeClient(FakeThreads([]))
    runs = PaginatedRuns()
    client.runs = cast(Any, runs)
    expected = [*runs.records["running"], *runs.records["pending"]]

    cancelled = await PipelineReadService(client).abort_pipeline("thread-1")

    assert cancelled == expected
    assert runs.cancelled == expected
    assert any(call["offset"] >= 10 for call in runs.list_calls)


async def test_abort_rejects_unsafe_native_run_id_before_cancellation() -> None:
    class UnsafeRuns:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def list(self, *_args: Any, status: str, **_kwargs: Any) -> list[JsonDict]:
            if status == "running":
                return [{"run_id": "password=abort-native-id-secret-canary"}]
            return []

        async def cancel(self, _thread_id: str, run_id: str) -> None:
            self.cancelled.append(run_id)

    client = FakeClient(FakeThreads([]))
    runs = UnsafeRuns()
    client.runs = cast(Any, runs)

    with pytest.raises(RuntimeError, match="invalid identifier") as excinfo:
        await PipelineReadService(client).abort_pipeline("thread-1")

    assert "abort-native-id-secret-canary" not in str(excinfo.value)
    assert runs.cancelled == []


GATE_PAYLOAD = {
    "schema_version": 1,
    "kind": "prompt_review",
    "phase": "test_planning",
    "prompt": {
        "system": "You are the planning agent.",
        "user": "Review the test plan.",
        "application": None,
        "source": {"origin": "catalog", "ref": None},
    },
    "additional_context": "",
    "context_packets": [],
    "tools": ["work_tracking.query"],
    "editable": True,
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


def operator_identity(projects: tuple[str, ...] = ()) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="op",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id=project) for project in projects],
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


def _pending_interrupts(start: int, count: int, *, payload: JsonDict | None = None) -> list:
    gate_payload = payload or GATE_PAYLOAD
    return [{"id": f"int-{index}", "value": gate_payload} for index in range(start, start + count)]


def test_pending_gates_from_state_caps_aggregate_task_interrupts() -> None:
    state = {
        "tasks": [
            {"interrupts": _pending_interrupts(0, MAX_PUBLIC_PENDING_GATES)},
            {
                "interrupts": _pending_interrupts(
                    MAX_PUBLIC_PENDING_GATES,
                    MAX_PUBLIC_PENDING_GATES,
                )
            },
        ],
        "interrupts": [],
    }

    gates = pending_gates_from_state(state)

    assert len(gates) == MAX_PUBLIC_PENDING_GATES
    assert [gate["interrupt_id"] for gate in gates] == [
        f"int-{index}" for index in range(MAX_PUBLIC_PENDING_GATES)
    ]


def test_pending_gates_from_thread_caps_aggregate_interrupt_mapping() -> None:
    first_count = 75
    thread = {
        "interrupts": {
            "task-a": _pending_interrupts(0, first_count),
            "task-b": _pending_interrupts(first_count, first_count),
        }
    }

    gates = pending_gates_from_thread(thread)

    assert len(gates) == MAX_PUBLIC_PENDING_GATES
    assert gates[-1]["interrupt_id"] == f"int-{MAX_PUBLIC_PENDING_GATES - 1}"


def test_pending_gate_helpers_reject_container_subclasses_without_hooks() -> None:
    class HostileDict(dict):
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("hostile mapping hook ran")

    class HostileList(list):
        def __len__(self) -> int:
            raise AssertionError("hostile sequence hook ran")

        def __iter__(self) -> Any:
            raise AssertionError("hostile sequence hook ran")

    assert pending_gates_from_thread(cast(Any, HostileDict())) == []
    assert pending_gates_from_state(cast(Any, HostileDict())) == []
    assert pending_gates_from_thread({"interrupts": cast(Any, HostileDict())}) == []
    assert pending_gates_from_state({"tasks": cast(Any, HostileList()), "interrupts": []}) == []
    assert (
        pending_gates_from_state(
            {
                "tasks": [
                    {
                        "interrupts": [
                            cast(
                                Any,
                                HostileDict({"id": "int-hostile", "value": GATE_PAYLOAD}),
                            )
                        ]
                    }
                ],
                "interrupts": [],
            }
        )
        == []
    )


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


def test_map_thread_summary_supports_projected_extracted_values() -> None:
    projected = {
        "thread_id": "t-3",
        "status": "idle",
        "metadata": {"project_id": "p1"},
        "extracted": {
            "title": "Projected title",
            "current_phase": "execution",
            "phase_results": {"execution": {"status": "running", "attempt": 2}},
            "engine_handle": {"engine": "apex-load", "external_run_id": "run-9"},
        },
        "interrupts": {},
    }

    summary = map_thread_summary(projected)

    assert summary["title"] == "Projected title"
    assert summary["current_phase"] == "execution"
    assert summary["engine"] == {"engine": "apex-load", "external_run_id": "run-9"}
    execution = next(row for row in summary["phase_strip"] if row["phase"] == "execution")
    assert execution == {"phase": "execution", "status": "running", "attempt": 2}


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


def test_list_pipelines_rejects_huge_offset_before_loopback() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED])
    app = make_app(FakeClient(fake), operator_identity())

    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"offset": 10_001})

    assert response.status_code == 422
    assert fake.search_calls == []


def test_list_pipelines_passes_project_filter_to_metadata_search() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity(("p2",)))
    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"project": "p2", "status": "idle"})
    assert response.status_code == 200
    assert [item["thread_id"] for item in response.json()["items"]] == ["t-2"]
    assert fake.search_calls[0]["metadata"] == {"project_id": "p2"}
    assert fake.search_calls[0]["status"] == "idle"
    assert fake.search_calls[0]["sort_by"] == "updated_at"
    assert fake.search_calls[0]["select"] == list(PIPELINE_SUMMARY_SELECT)
    assert fake.search_calls[0]["extract"] == PIPELINE_SUMMARY_EXTRACT


def test_list_pipelines_rejects_out_of_scope_project_before_loopback() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity(("p1",)))
    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"project": "p2"})
    assert response.status_code == 403
    assert fake.search_calls == []


def test_list_pipelines_q_filters_current_page_by_title() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED, THREAD_IDLE])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"q": "checkout"})
    assert [item["thread_id"] for item in response.json()["items"]] == ["t-1"]


def test_list_pipelines_rejects_oversized_q_before_loopback() -> None:
    fake = FakeThreads([THREAD_INTERRUPTED])
    app = make_app(FakeClient(fake), operator_identity())

    with TestClient(app) as client:
        response = client.get(
            "/v1/pipelines",
            params={"q": "x" * (MAX_PIPELINE_QUERY_CHARS + 1)},
        )

    assert response.status_code == 422
    assert fake.search_calls == []


def test_list_pipelines_q_scan_has_a_hard_aggregate_record_cap() -> None:
    threads = [
        {
            "thread_id": f"thread-{index}",
            "status": "idle",
            "metadata": {"title": "unrelated"},
            "values": {},
            "interrupts": {},
        }
        for index in range(MAX_PIPELINE_TEXT_SCAN_RECORDS + PIPELINE_TEXT_SCAN_PAGE_SIZE)
    ]
    fake = FakeThreads(threads)
    app = make_app(FakeClient(fake), operator_identity())

    with TestClient(app) as client:
        response = client.get("/v1/pipelines", params={"q": "no-match"})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert len(fake.search_calls) == (
        MAX_PIPELINE_TEXT_SCAN_RECORDS // PIPELINE_TEXT_SCAN_PAGE_SIZE
    )
    assert sum(call["limit"] for call in fake.search_calls) == MAX_PIPELINE_TEXT_SCAN_RECORDS
    assert all(call["select"] == list(PIPELINE_SUMMARY_SELECT) for call in fake.search_calls)
    assert all(call["extract"] == PIPELINE_SUMMARY_EXTRACT for call in fake.search_calls)


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


def test_get_pipeline_caps_aggregate_gate_payload_response() -> None:
    payload = {**GATE_PAYLOAD, "additional_context": "C" * 50_000}
    state = {
        "values": THREAD_INTERRUPTED["values"],
        "tasks": [
            {"interrupts": _pending_interrupts(0, MAX_PUBLIC_PENDING_GATES, payload=payload)},
            {
                "interrupts": _pending_interrupts(
                    MAX_PUBLIC_PENDING_GATES,
                    MAX_PUBLIC_PENDING_GATES,
                    payload=payload,
                )
            },
        ],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    body = response.json()
    gates = body["interrupts"]
    assert 1 <= len(gates) < MAX_PUBLIC_PENDING_GATES
    assert [gate["interrupt_id"] for gate in gates] == [
        f"int-{index}" for index in range(len(gates))
    ]
    encoded_gates = [json.dumps(gate, separators=(",", ":")).encode() for gate in gates]
    assert all(len(gate) <= MAX_PUBLIC_GATE_BYTES for gate in encoded_gates)
    assert sum(map(len, encoded_gates)) + max(len(gates) - 1, 0) + 2 <= (
        MAX_PUBLIC_PENDING_GATES_BYTES
    )
    assert len(response.content) <= (
        MAX_PUBLIC_PENDING_GATES_BYTES + MAX_PUBLIC_PIPELINE_STATE_BYTES + 100_000
    )


def test_pending_gate_projection_enforces_aggregate_node_budget_without_large_preflight_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        **GATE_PAYLOAD,
        "context_packets": [
            {"id": str(index), "source": "doc", "title": "title", "summary": "summary"}
            for index in range(64)
        ],
        "tools": [f"tool-{index}" for index in range(64)],
    }
    measured_candidates: list[str] = []
    original_cost = pipeline_read_module._bounded_json_cost

    def tracked_cost(value: JsonDict, *, max_bytes: int, max_nodes: int):  # noqa: ANN202
        assert type(value) is dict
        assert type(value.get("interrupt_id")) is str
        assert "interrupts" not in value
        measured_candidates.append(value["interrupt_id"])
        return original_cost(value, max_bytes=max_bytes, max_nodes=max_nodes)

    monkeypatch.setattr(pipeline_read_module, "_bounded_json_cost", tracked_cost)
    state = {
        "values": THREAD_INTERRUPTED["values"],
        "tasks": [
            {"interrupts": _pending_interrupts(0, MAX_PUBLIC_PENDING_GATES, payload=payload)}
        ],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    gates = response.json()["interrupts"]
    assert 1 <= len(gates) < MAX_PUBLIC_PENDING_GATES
    assert measured_candidates == [f"int-{index}" for index in range(len(gates) + 1)]
    # The aggregate JSON tree stays below the node cap; preflight only ever
    # encodes one bounded gate dictionary, never the full provider collection.
    stack: list[Any] = [gates]
    node_cost = 0
    while stack:
        current = stack.pop()
        node_cost += 1
        if type(current) is dict:
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
    assert node_cost <= MAX_PUBLIC_PENDING_GATES_NODES
    assert len(json.dumps(gates, separators=(",", ":")).encode()) <= (
        MAX_PUBLIC_PENDING_GATES_BYTES
    )


async def test_get_pipeline_rejects_provider_container_subclasses_without_hooks() -> None:
    class HostileDict(dict):
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("hostile provider mapping hook ran")

    hostile_thread = HostileDict(THREAD_INTERRUPTED)
    client = FakeClient(
        FakeThreads(
            cast(Any, [hostile_thread]),
            states={"t-1": {"values": {}, "tasks": [], "interrupts": []}},
        )
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await PipelineReadService(client).get_pipeline("t-1")


def test_get_pipeline_engine_retry_gate_matches_shared_consumer_fixture() -> None:
    payload = {
        "schema_version": 1,
        "kind": "engine_cleanup_retry",
        "phase": "execution",
        "attempt": 2,
        "thread_id": "t-1",
        "actions": ["retry"],
        "error": "provider abort unavailable",
        "message": "Resume the exact durable provider attempt.",
        "connection_id": "must-not-project",
    }
    state = {
        "values": THREAD_INTERRUPTED["values"],
        "tasks": [
            {
                "interrupts": [
                    {
                        "id": "engine-retry-interrupt-1",
                        "value": payload,
                    }
                ]
            }
        ],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    expected = json.loads(
        (
            Path(__file__).parents[2]
            / "packages/pipeline-events/tests/fixtures/public-engine-retry-gate.json"
        ).read_text()
    )

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    body = response.json()
    assert body["interrupts"] == [expected]
    assert body["pending_gate"] == {key: expected[key] for key in ("interrupt_id", "kind", "phase")}


def test_get_pipeline_projects_checkpoint_and_gate_without_operational_secrets() -> None:
    canary = "PIPELINE_PUBLIC_STATE_SECRET_CANARY"
    bearer_canary = "PIPELINE_CONFIG_BEARER_CANARY"
    signed_canary = "PIPELINE_CONFIG_SIGNED_QUERY_CANARY"
    values = {
        "title": "Checkout regression",
        "request": canary,
        "run_config": {
            "assistant_id": f"Bearer {bearer_canary}",
            "project_id": "p1",
            "app_id": "a1",
            "environment_id": f"https://env.test/?X-Amz-Signature={signed_canary}",
            "environment_target": f"https://{canary}.example.test",
            "environment_target_version": 7,
            "connections": {"execution_engine": canary},
            "engine": f"Bearer {bearer_canary}",
            "load_test": {
                "vusers": 10,
                "title": f"https://load.test/?X-Amz-Signature={signed_canary}",
                "slas": {f"Bearer {bearer_canary}": 1.0},
                "test_id": canary,
            },
        },
        "phase_results": {
            "execution": {
                "status": "succeeded",
                "attempt": 1,
                "summary": f"token={canary}",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "tool": "provider.lookup",
                        "status": "error",
                        "duration_ms": 3,
                        "at": "2026-06-01T00:00:00+00:00",
                        "args_preview": {"api_key": canary},
                        "error": canary,
                    }
                ],
                "engine_options": {"provider_token": canary},
                "engine_handle": {
                    "engine": "sim",
                    "idempotency_key": "idem-1",
                    "extras": {"provider_token": canary},
                },
                "test_summary": {
                    "engine": "sim",
                    "passed": False,
                    "kpis": {"error_rate": 1.0},
                    "sla_breaches": [],
                    "notes": f"token={canary}",
                },
            }
        },
        "artifacts": [
            {
                "id": "artifact-1",
                "kind": "report",
                "name": "report.json",
                "uri": "memory://reports/report.json",
                "media_type": "application/json",
                "key": canary,
                "artifact_connection_id": canary,
            }
        ],
        "context_packets": [
            {
                "id": "ctx-1",
                "source": "jira",
                "title": "Story",
                "summary": "bounded",
                "text": canary,
                "ref": canary,
            }
        ],
        "application_reviews": {
            f"X-Amz-Signature={signed_canary}": {
                "content": "safe application prompt",
                "source": {"origin": "run_override"},
                "updated_at": "2026-06-01T00:00:00+00:00",
                "updated_by": "operator",
            }
        },
        "engine_handle": {
            "engine": "sim",
            "external_run_id": "run-1",
            "idempotency_key": "idem-1",
            "connection_id": canary,
            "extras": {"provider_token": canary},
        },
        "unknown_server_state": {"authorization": canary},
    }
    payload = {
        "schema_version": 1,
        "kind": "prompt_review",
        "phase": "test_planning",
        "prompt": {
            "system": f"token={canary}",
            "user": "Review the plan",
            "application": None,
            "source": {"origin": "catalog", "ref": "phase/test_planning@v1"},
        },
        "additional_context": "bounded",
        "context_packets": [],
        "tools": ["work_tracking.query"],
        "editable": True,
        "actions": ["approve", "modify", "skip_phase", "abort"],
        "provider_token": canary,
    }
    state = {
        "values": values,
        "tasks": [{"interrupts": [{"id": "int-secret", "value": payload}]}],
        "interrupts": [],
    }
    thread = {**THREAD_INTERRUPTED, "values": values}
    fake = FakeThreads([thread], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    assert canary not in response.text
    assert bearer_canary not in response.text
    assert signed_canary not in response.text
    body = response.json()
    public_state = body["values"]
    assert "request" not in public_state
    assert "unknown_server_state" not in public_state
    assert public_state["engine_handle"] == {"engine": "sim", "external_run_id": "run-1"}
    # Credential-bearing legacy config now fails closed as a whole. If a future
    # projector recovers safe display fields, server-only fields must remain absent.
    if (run_config := public_state.get("run_config")) is not None:
        assert set(run_config).isdisjoint(
            {
                "assistant_id",
                "project_id",
                "app_id",
                "environment_id",
                "connections",
                "environment_target",
                "environment_target_version",
                "prompt_overrides",
                "pre_execution_context",
            }
        )
        assert set(run_config.get("load_test") or {}).issubset(
            {"title", "vusers", "ramp_s", "duration_s", "slas"}
        )
    execution = public_state["phase_results"]["execution"]
    assert execution["summary"] == "token=[REDACTED]"
    assert execution["tool_calls"] == [
        {
            "id": "call-1",
            "tool": "provider.lookup",
            "status": "error",
            "duration_ms": 3,
            "at": "2026-06-01T00:00:00+00:00",
        }
    ]
    assert "engine_options" not in execution
    assert "engine_handle" not in execution
    assert public_state["artifacts"][0].keys().isdisjoint({"key", "artifact_connection_id"})
    assert public_state["context_packets"][0].keys().isdisjoint({"text", "ref"})
    # A passive checkpoint summary may be safely redacted, but an actionable
    # review must be a faithful projection of what the graph will execute.
    # Quarantine this legacy gate instead of offering approval over hidden text.
    assert body["interrupts"] == []
    assert body["pending_gate"] is None


def test_get_pipeline_quarantines_gate_that_cannot_fit_complete_public_contract() -> None:
    large_prompt = "P" * 100_000
    values = {
        "title": "Large legacy checkpoint",
        "phases_plan": [phase.value for phase in PHASE_ORDER],
        "phase_results": {
            phase.value: {
                "status": "succeeded",
                "attempt": 1,
                "resolved_prompt": {
                    "system": large_prompt,
                    "user": large_prompt,
                    "application": large_prompt,
                },
            }
            for phase in PHASE_ORDER
        },
        "prompt_reviews": {
            phase.value: {
                "system": large_prompt,
                "phase_prompt": large_prompt,
                "application": large_prompt,
                "additional_context": "",
                "source": {"origin": "catalog"},
                "updated_at": "2026-06-01T00:00:00+00:00",
                "updated_by": "system",
            }
            for phase in PHASE_ORDER
        },
    }
    gate = {
        **GATE_PAYLOAD,
        "prompt": {
            "system": large_prompt,
            "user": large_prompt,
            "application": large_prompt,
            "source": {"origin": "catalog"},
        },
        "additional_context": "C" * 50_000,
    }
    state = {
        "values": values,
        "tasks": [{"interrupts": [{"id": "int-large", "value": gate}]}],
        "interrupts": [],
    }
    thread = {**THREAD_INTERRUPTED, "values": values}

    with TestClient(
        make_app(FakeClient(FakeThreads([thread], states={"t-1": state})), operator_identity())
    ) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    body = response.json()
    encoded_state = json.dumps(body["values"], separators=(",", ":")).encode()
    assert len(encoded_state) <= MAX_PUBLIC_PIPELINE_STATE_BYTES
    assert "prompt_reviews" not in body["values"]
    assert body["values"]["phase_results"]["execution"] == {
        "phase": "execution",
        "status": "succeeded",
        "attempt": 1,
    }
    assert body["interrupts"] == []
    assert body["pending_gate"] is None


def test_get_pipeline_quarantines_malformed_legacy_checkpoint_and_interrupts() -> None:
    canary = "MALFORMED_LEGACY_CHECKPOINT_CANARY"
    values = {
        "title": canary * 1_000,
        "phase_results": {"not-a-phase": {"summary": canary}},
        "engine_handle": {"engine": canary * 100, "external_run_id": canary},
        "artifacts": [{"id": canary * 100, "uri": f"s3://bucket/key?token={canary}"}],
    }
    state = {
        "values": values,
        "tasks": [
            {
                "interrupts": [
                    {
                        "id": canary * 100,
                        "value": {
                            "kind": "prompt_review",
                            "phase": "test_planning",
                            "provider_token": canary,
                        },
                    }
                ]
            }
        ],
    }
    thread = {**THREAD_INTERRUPTED, "values": values}
    fake = FakeThreads([thread], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1")

    assert response.status_code == 200
    assert canary not in response.text
    assert response.json()["interrupts"] == []
    assert response.json()["values"].get("engine_handle") is None


def test_prompt_review_read_redacts_malformed_checkpoint_diagnostics() -> None:
    canary = "PROMPT_REVIEW_CHECKPOINT_SECRET"
    state = {
        "values": {
            "prompt_reviews": {
                "story_analysis": {
                    "system": f"token={canary}",
                    "phase_prompt": "P",
                    "application": None,
                    "additional_context": "",
                    "source": {"origin": "run_override"},
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "updated_by": "operator",
                    "provider_token": canary,
                }
            }
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.get("/v1/pipelines/t-1/phases/story_analysis/prompt-review")

    assert response.status_code == 200
    assert response.json()["system"] == "token=[REDACTED]"
    assert canary not in response.text


def test_get_pipeline_unknown_thread_is_404_problem() -> None:
    fake = FakeThreads([])
    app = make_app(FakeClient(fake), operator_identity())
    with TestClient(app) as client:
        response = client.get("/v1/pipelines/missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_pipeline_not_found_does_not_reflect_signed_query_shaped_id() -> None:
    canary = "X-Amz-Signature=secret-canary"
    with TestClient(make_app(FakeClient(FakeThreads([])), operator_identity())) as client:
        response = client.get(f"/v1/pipelines/{canary}")

    assert response.status_code == 404
    assert response.json()["title"] == "pipeline thread not found"
    assert canary not in response.text


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


def test_get_terminal_phase_prompt_review_uses_executed_prompt_not_mutable_draft() -> None:
    state = {
        "values": {
            "phase_results": {
                "story_analysis": {
                    "status": "succeeded",
                    "resolved_prompt": {
                        "system": "Executed system",
                        "user": "Executed user",
                        "application": "Executed application",
                    },
                    "resolved_prompt_source": {
                        "origin": "catalog",
                        "ref": "phase/story@v7",
                    },
                }
            },
            "prompt_reviews": {
                "story_analysis": _seeded_review("Later draft", "Later application")
            },
            "application_reviews": {
                "a1": {
                    "content": "Later app-wide override",
                    "source": {"origin": "run_override"},
                    "updated_at": "2026-06-02T00:00:00+00:00",
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
        response = client.get("/v1/pipelines/t-1/phases/story_analysis/prompt-review")

    assert response.status_code == 200
    body = response.json()
    assert body["system"] == "Executed system"
    assert body["phase_prompt"] == "Executed user"
    assert body["application"] == "Executed application"
    assert body["source"]["ref"] == "phase/story@v7"


def test_patch_phase_prompt_review_updates_state_without_run() -> None:
    state = {
        "values": {},
        "tasks": [],
        "interrupts": [],
        "checkpoint": {"checkpoint_id": "cp-1"},
    }
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
    assert fake.update_calls[0]["as_node"] == "plan_resolver"
    assert fake.update_calls[0]["checkpoint"] == {"checkpoint_id": "cp-1"}
    assert client_obj.runs.create_calls == []


def test_patch_phase_prompt_review_rejects_credentials_before_checkpoint_write() -> None:
    state = {
        "values": {},
        "tasks": [],
        "interrupts": [],
        "checkpoint": {"checkpoint_id": "cp-1"},
    }
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
                "additional_context": "Authorization: Bearer prompt-secret-canary",
            },
        )

    assert response.status_code == 422
    assert "prompt-secret-canary" not in response.text
    assert fake.update_calls == []
    assert client_obj.runs.create_calls == []


@pytest.mark.parametrize(
    "checkpoint",
    [None, {"checkpoint_id": "x", "provider_blob": "x" * 100_001}],
)
def test_patch_phase_prompt_review_requires_a_bounded_checkpoint_cas(
    checkpoint: JsonDict | None,
) -> None:
    state: JsonDict = {
        "values": {},
        "tasks": [],
        "interrupts": [],
    }
    if checkpoint is not None:
        state["checkpoint"] = checkpoint
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )

    assert response.status_code == 409
    assert fake.update_calls == []


def test_patch_phase_prompt_review_rejects_noncanonical_provider_draft() -> None:
    canary = "legacy-application-secret-canary"
    review = _seeded_review("S", f"Authorization: Bearer {canary}")
    state = {
        "values": {"prompt_reviews": {"story_analysis": review}},
        "tasks": [],
        "interrupts": [],
        "checkpoint": {"checkpoint_id": "cp-unsafe-draft"},
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})

    with TestClient(make_app(FakeClient(fake), operator_identity())) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/story_analysis/prompt-review",
            json={
                "system": "S2",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )

    assert response.status_code == 409
    assert canary not in response.text
    assert fake.update_calls == []


@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "aborted"])
def test_patch_phase_prompt_review_rejects_started_or_terminal_phase(status: str) -> None:
    state = {
        "values": {"phase_results": {"story_analysis": {"status": status, "attempt": 1}}},
        "tasks": [],
        "interrupts": [],
        "checkpoint": {"checkpoint_id": "cp-started"},
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())

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

    assert response.status_code == 409
    assert fake.update_calls == []


def test_patch_awaiting_prompt_review_requires_matching_pending_gate() -> None:
    state = {
        "values": {
            "phase_results": {"story_analysis": {"status": "awaiting_prompt_review", "attempt": 1}}
        },
        "tasks": [],
        "interrupts": [],
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    app = make_app(FakeClient(fake), operator_identity())

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

    assert response.status_code == 409
    assert fake.update_calls == []


def test_patch_awaiting_prompt_review_rejects_queued_resume_run() -> None:
    state = {
        "values": {
            "phase_results": {"test_planning": {"status": "awaiting_prompt_review", "attempt": 2}}
        },
        "tasks": [{"interrupts": [{"id": "int-1", "value": GATE_PAYLOAD}]}],
        "checkpoint": {"checkpoint_id": "still-old-gate"},
    }
    fake = FakeThreads([THREAD_INTERRUPTED], states={"t-1": state})
    client_obj = FakeClient(fake)
    client_obj.runs.active_statuses.add("pending")
    app = make_app(client_obj, operator_identity())

    with TestClient(app) as client:
        response = client.patch(
            "/v1/pipelines/t-1/phases/test_planning/prompt-review",
            json={
                "system": "S",
                "phase_prompt": "P",
                "application": None,
                "additional_context": "",
            },
        )

    assert response.status_code == 409
    assert fake.update_calls == []


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
        "checkpoint": {"checkpoint_id": "cp-application-wide"},
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
        "checkpoint": {"checkpoint_id": "cp-application-unchanged"},
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
        "checkpoint": {"checkpoint_id": "cp-application-preserved"},
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
