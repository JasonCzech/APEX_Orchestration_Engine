"""APEX Load engine adapter against respx wire fixtures.

Fixture shapes follow Project_Stormrunner/API_AND_DSL_REFERENCE.md and the
definitive Go handlers in Project_Stormrunner/pkg/api (cited per fixture):

- list tests:    handleListTests        -> {"tests": [...], "count": N}
- get test:      handleGetTest          -> ManagedTest (status, live_metrics, ...)
- create script: handleCreateScript     -> 201 stored dsl.Script ({"id": ...})
- create test:   handleCreateTest       -> 201 ManagedTest
- start:         handleStartTest        -> 200 {"status": "started", ...};
                 400 startableTestStatusError when not startable
- abort:         handleAbortTest        -> 200 {"status": "aborted", "test_id"}
- sla-status:    handleGetTestSLAStatus -> SLABreachReport
- archive/report: handleGetRunArchiveReport -> RunArchiveReport
- validate:      handleValidateDraftScript / handleValidateScript
                 -> scriptValidationResponse {"valid", "issues", ...}
"""

import asyncio
import json
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from apex.adapters.apex_load.engine import (
    ApexLoadExecutionEngine,
    format_go_duration,
    parse_go_duration_s,
)
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import LoadTestSpec, SecretValue
from apex.domain.pipeline import EngineHandle
from apex.graphs.pipeline import execution_phase, phase_subgraph
from apex.graphs.pipeline.state import PipelineState
from apex.ports.artifact_store import StoredArtifact, engine_artifact_key
from apex.ports.execution_engine import EngineRunPhase

BASE = "https://apexload.acme.test"
API_KEY = "apexload-service-key"
KEY = "thread-42-execution-a1"
TEST_NAME = f"apex-orch:{KEY}"
TEST_ID = "test-789"


def make_adapter(**option_overrides: object) -> ApexLoadExecutionEngine:
    options: dict[str, object] = {"base_url": BASE, "project_id": "proj-123"}
    options.update(option_overrides)
    options = {k: v for k, v in options.items() if v is not None}
    conn = ConnectionConfig(
        id="apexload-acme",
        kind=PortKind.EXECUTION_ENGINE,
        provider="apex_load",
        name="Acme APEX Load",
        options=options,
    )
    return ApexLoadExecutionEngine(conn, SecretValue(value=API_KEY))


def make_spec(**overrides: Any) -> LoadTestSpec:
    base: dict[str, Any] = {
        "idempotency_key": KEY,
        "title": "Checkout load test",
        "script_refs": [],
        "vusers": 50,
        "ramp_s": 30.0,
        "duration_s": 300.0,
        "slas": {"p95_ms": 500.0, "error_rate": 0.01, "tps_avg": 80.0},
        "target_environment": "https://shop.acme.test",
    }
    base.update(overrides)
    return LoadTestSpec.model_validate(base)


def make_handle(run_id: str = TEST_ID) -> EngineHandle:
    return EngineHandle(
        engine="apex_load",
        connection_id="apexload-acme",
        external_run_id=run_id,
        idempotency_key=KEY,
        extras={"test_name": TEST_NAME, "project_id": "proj-123"},
    )


def assert_all_calls_authed() -> None:
    """Every mocked exchange must carry the service-account API key header."""
    assert respx.calls, "expected at least one mocked call"
    for call in respx.calls:
        assert call.request.headers["X-APEXLoad-API-Key"] == API_KEY


# ── wire fixtures ─────────────────────────────────────────────────────────────


def managed_test_json(
    test_id: str = TEST_ID,
    name: str = TEST_NAME,
    status: str = "QUEUED",
    **extra: Any,
) -> dict[str, Any]:
    """ManagedTest as serialized by pkg/api/runner.go (handleGetTest/handleCreateTest)."""
    test: dict[str, Any] = {
        "id": test_id,
        "name": name,
        "status": status,
        "project_id": "proj-123",
        "groups": [
            {
                "name": "group-1",
                "script_id": "script-456",
                "script_name": f"{name} script",
                "protocol": "http",
                "vusers": 50,
                "ramp_up": "30s",
            }
        ],
        "total_vusers": 50,
        "duration_ns": 300_000_000_000,  # 300s
        "duration": "5m0s",
        "created_at": "2026-06-11T10:30:00Z",
    }
    test.update(extra)
    return test


def live_metrics_json(**extra: Any) -> dict[str, Any]:
    """LiveMetrics (pkg/api/runner.go): error_pct is a PERCENTAGE, p95 is p95_ms."""
    metrics: dict[str, Any] = {
        "timestamp": "2026-06-11T10:31:45Z",
        "active_vusers": 42,
        "tps": 94.4,
        "avg_response_ms": 156.3,
        "error_pct": 2.5,
        "total_transactions": 4250,
        "total_errors": 12,
        "p50_ms": 142.0,
        "p90_ms": 401.2,
        "p95_ms": 482.5,
        "p99_ms": 892.1,
    }
    metrics.update(extra)
    return metrics


def archive_report_json(**overview_extra: Any) -> dict[str, Any]:
    """RunArchiveReport (pkg/api/archive.go buildRunArchiveReport / reference
    "archive reports via overview...")."""
    overview: dict[str, Any] = {
        "transactions": 28_000,
        "errors": 280,
        "error_pct": 1.0,
        "avg_response_ms": 150.2,
        "min_response_ms": 21.0,
        "max_response_ms": 1900.4,
        "peak_tps": 120.0,
        "peak_active_vusers": 50,
        "resource_samples": 60,
        "interval_summaries": 3,
    }
    overview.update(overview_extra)
    return {
        "schema_version": "1",
        "manifest": {
            "schema_version": "1",
            "test_id": TEST_ID,
            "test_name": TEST_NAME,
            "status": "COMPLETE",
            "transaction_count": 28_000,
            "sla_breached": False,
        },
        "overview": overview,
        "summary_timeline": [
            {
                "timestamp": "2026-06-11T10:31:00Z",
                "active_vusers": 25,
                "tps": 80.0,
                "error_pct": 0.8,
                "p95_ms": 420.0,
                "total_transactions": 9000,
                "total_errors": 70,
            },
            {
                "timestamp": "2026-06-11T10:33:00Z",
                "active_vusers": 50,
                "tps": 110.0,
                "error_pct": 1.1,
                "p95_ms": 470.0,
                "total_transactions": 19000,
                "total_errors": 200,
            },
            {
                "timestamp": "2026-06-11T10:35:00Z",
                "active_vusers": 50,
                "tps": 104.0,
                "error_pct": 1.0,
                "p95_ms": 460.0,
                "total_transactions": 28000,
                "total_errors": 280,
            },
        ],
        "by_action": {
            "load_root": {
                "transactions": 28_000,
                "errors": 280,
                "error_pct": 1.0,
                "avg_response_ms": 150.2,
                "p95_ms": 455.0,
            }
        },
        "resource_peaks": {"cpu_process_pct": 41.0, "mem_process_mb": 512.0},
    }


def sla_status_json(
    status: str = "COMPLETE", breached: bool = False, details: list[str] | None = None
) -> dict[str, Any]:
    """SLABreachReport (pkg/api/handlers.go handleGetTestSLAStatus)."""
    body: dict[str, Any] = {"test_id": TEST_ID, "status": status, "sla_breached": breached}
    if details:
        body["details"] = details
    return body


def validation_response_json(valid: bool = True, issues: list[str] | None = None) -> dict[str, Any]:
    """scriptValidationResponse (pkg/api/handlers.go buildScriptValidationResponse)."""
    return {
        "script_id": "",
        "valid": valid,
        "issues": issues or [],
        "warnings": [],
        "authoring_health": {"status": "ok"},
    }


class RecordingStore:
    """ArtifactStorePort double that records puts."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        self.puts.append((key, data, content_type))
        return StoredArtifact(key=key, uri=f"s3://apex-artifacts/{key}", size=len(data))

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact:
        payload = bytearray()
        async for chunk in data:
            if len(payload) + len(chunk) > max_bytes:
                raise ValueError(f"artifact exceeds maximum size of {max_bytes} bytes")
            payload.extend(chunk)
        return await self.put(key, bytes(payload), content_type=content_type)

    async def get(self, key: str) -> bytes:
        raise KeyError(key)

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        async def _missing() -> AsyncIterator[bytes]:
            if False:  # pragma: no cover - preserves the async-generator shape
                yield b""
            raise KeyError(key)

        return _missing()

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        return f"{BASE}/{key}?ttl={ttl_s}"


# ── go duration helpers ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("300s", 300.0),
        ("5m", 300.0),
        ("5m0s", 300.0),
        ("1m30s", 90.0),
        ("2.5s", 2.5),
        ("1h", 3600.0),
        ("250ms", 0.25),
        ("", None),
        ("5 minutes", None),
        (300, None),
        (None, None),
    ],
)
def test_parse_go_duration(raw: Any, expected: float | None) -> None:
    assert parse_go_duration_s(raw) == expected


def test_format_go_duration() -> None:
    assert format_go_duration(300.0) == "300s"
    assert format_go_duration(2.5) == "2.5s"


# ── constructor ───────────────────────────────────────────────────────────────


def test_constructor_requires_base_url_and_secret() -> None:
    conn = ConnectionConfig(
        id="apexload-x", kind=PortKind.EXECUTION_ENGINE, provider="apex_load", name="x"
    )
    with pytest.raises(ValueError, match="base_url"):
        ApexLoadExecutionEngine(conn, SecretValue(value="k"))
    conn = ConnectionConfig(
        id="apexload-x",
        kind=PortKind.EXECUTION_ENGINE,
        provider="apex_load",
        name="x",
        options={"base_url": BASE},
    )
    with pytest.raises(ValueError, match="API key"):
        ApexLoadExecutionEngine(conn, None)


# ── validate (contract: validation before provision) ─────────────────────────


@respx.mock
async def test_validate_default_workload_uses_draft_endpoint() -> None:
    route = respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(200, json=validation_response_json())
    )
    report = await make_adapter().validate(make_spec())
    assert report.ok and report.issues == []
    payload = json.loads(route.calls.last.request.content)
    assert payload["project_id"] == "proj-123"
    script = payload["script"]
    assert script["config"]["base_url"] == "https://shop.acme.test"
    assert script["protocol"] == "http"
    assert script["actions"][0]["method"] == "GET"
    assert_all_calls_authed()


@respx.mock
async def test_generated_graph_spec_uses_apex_default_workload_contract() -> None:
    generated = phase_subgraph._script_scenario_load_test_spec(
        {"title": "Checkout"},  # type: ignore[arg-type]
        {"configurable": {"thread_id": "thread-42"}},
        1,
    )
    state: PipelineState = {
        "title": "Checkout",
        "phase_results": {"script_scenario": {"load_test_spec": generated}},
    }
    spec, options = execution_phase._build_spec(
        state,
        {"configurable": {"thread_id": "thread-42", "engine": "apex_load"}},
        1,
        "apex_load",
        target_environment="https://approved.example.test",
    )
    route = respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(200, json=validation_response_json())
    )

    report = await make_adapter().validate(spec)

    assert options == {}
    assert spec.script_refs == []
    assert report.ok
    payload = json.loads(route.calls.last.request.content)
    assert payload["script"]["config"]["base_url"] == "https://approved.example.test"


async def test_validate_local_structural_issues_skip_remote() -> None:
    # Public construction rejects these values; bypass validation to retain the
    # adapter's defense-in-depth contract for callers holding an old/checkpointed model.
    spec = make_spec().model_copy(update={"vusers": 0, "duration_s": 0, "ramp_s": -1})
    report = await make_adapter().validate(spec)  # no respx mock: must not call out
    assert not report.ok
    assert len(report.issues) == 3


async def test_validate_inline_ref_bad_json_is_an_issue() -> None:
    report = await make_adapter().validate(make_spec(script_refs=["{not json"]))
    assert not report.ok
    assert any("script_refs[0]" in issue for issue in report.issues)


async def test_validate_default_workload_requires_target_environment() -> None:
    report = await make_adapter().validate(make_spec(target_environment=None))
    assert not report.ok
    assert any("target_environment" in issue for issue in report.issues)


async def test_validate_rejects_named_script_ref_path_traversal_without_remote_call() -> None:
    report = await make_adapter().validate(make_spec(script_refs=["../../tests/private/start"]))
    assert not report.ok
    assert any("safe APEX Load script id" in issue for issue in report.issues)


@respx.mock
async def test_validate_propagates_remote_issues() -> None:
    respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(
            200,
            json=validation_response_json(valid=False, issues=["actions[0]: target is required"]),
        )
    )
    report = await make_adapter().validate(make_spec())
    assert not report.ok
    assert report.issues == ["actions[0]: target is required"]
    assert_all_calls_authed()


@respx.mock
async def test_validate_named_ref_validates_existing_script() -> None:
    respx.post(f"{BASE}/api/v1/scripts/script-456/validate").mock(
        return_value=httpx.Response(200, json=validation_response_json())
    )
    missing = respx.post(f"{BASE}/api/v1/scripts/script-gone/validate").mock(
        return_value=httpx.Response(404, json={"error": "script not found"})
    )
    report = await make_adapter().validate(make_spec(script_refs=["script-456", "script-gone"]))
    assert missing.called
    assert not report.ok
    assert any("script-gone" in issue for issue in report.issues)
    assert_all_calls_authed()


# ── provision (contract req. 1: get-or-create by idempotency key) ─────────────


@respx.mock
async def test_provision_creates_script_and_test_when_absent() -> None:
    list_route = respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    script_route = respx.post(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(
            201, json={"id": "script-456", "name": f"{TEST_NAME} script", "protocol": "http"}
        )
    )
    create_route = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json(status="QUEUED"))
    )
    adapter = make_adapter()
    handle = await adapter.provision(make_spec())

    assert list_route.calls.last.request.url.params["project_id"] == "proj-123"
    script_payload = json.loads(script_route.calls.last.request.content)
    assert script_payload["project_id"] == "proj-123"
    assert script_payload["script"]["config"]["base_url"] == "https://shop.acme.test"

    body = json.loads(create_route.calls.last.request.content)
    assert create_route.calls.last.request.headers["Idempotency-Key"] == KEY
    assert body["name"] == TEST_NAME  # the remote name carries the idempotency key
    assert body["project_id"] == "proj-123"
    assert body["duration"] == "300s"
    assert body["groups"] == [
        {"name": "group-1", "script_id": "script-456", "vusers": 50, "ramp_up": "30s"}
    ]
    # spec.slas -> goal_config (error_rate fraction -> max_error_pct percent)
    assert body["goal_config"] == {
        "target_average_tps": 80.0,
        "p95_latency_ms": 500.0,
        "max_error_pct": 1.0,
    }

    assert handle.engine == "apex_load"
    assert handle.connection_id == "apexload-acme"
    assert handle.external_run_id == TEST_ID
    assert handle.idempotency_key == KEY
    assert handle.extras["test_name"] == TEST_NAME
    assert_all_calls_authed()


async def test_concurrent_provision_calls_create_one_remote_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fresh adapters cannot both pass the lookup before remote creation."""

    remote_test: dict[str, Any] | None = None
    create_count = 0

    async def find_test(_adapter: ApexLoadExecutionEngine, _name: str) -> dict[str, Any] | None:
        # Snapshot before yielding: without the keyed guard both callers observe
        # absence and each creates an expensive test.
        snapshot = remote_test
        await asyncio.sleep(0.02)
        return snapshot

    async def create_test(
        _adapter: ApexLoadExecutionEngine, _name: str, _spec: LoadTestSpec
    ) -> dict[str, Any]:
        nonlocal create_count, remote_test
        create_count += 1
        await asyncio.sleep(0.01)
        remote_test = managed_test_json()
        return remote_test

    monkeypatch.setattr(ApexLoadExecutionEngine, "_find_test_by_name", find_test)
    monkeypatch.setattr(ApexLoadExecutionEngine, "_create_test", create_test)

    first, second = await asyncio.gather(
        make_adapter().provision(make_spec()),
        make_adapter().provision(make_spec()),
    )

    assert create_count == 1
    assert first.external_run_id == second.external_run_id == TEST_ID


@respx.mock
async def test_provision_adopts_provider_winner_after_idempotency_conflict() -> None:
    """The provider reservation can win just before its test row is visible."""

    respx.get(f"{BASE}/api/v1/tests").mock(
        side_effect=[
            httpx.Response(200, json={"tests": [], "count": 0}),
            httpx.Response(200, json={"tests": [managed_test_json()], "count": 1}),
        ]
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(201, json={"id": "script-loser"})
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(409, json={"error": "duplicate idempotency key"})
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert create.calls.last.request.headers["Idempotency-Key"] == KEY
    assert_all_calls_authed()


@respx.mock
async def test_provision_adopts_test_when_create_response_is_lost() -> None:
    """A committed test create is recovered by its durable idempotent name."""

    respx.get(f"{BASE}/api/v1/tests").mock(
        side_effect=[
            httpx.Response(200, json={"tests": [], "count": 0}),
            httpx.Response(200, json={"tests": [managed_test_json()], "count": 1}),
        ]
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(201, json={"id": "script-committed"})
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(
        side_effect=httpx.ReadTimeout("response lost after remote commit")
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert create.call_count == 1
    assert create.calls.last.request.headers["Idempotency-Key"] == KEY
    assert_all_calls_authed()


@respx.mock
async def test_provision_adopts_script_when_upload_response_is_lost() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    committed: dict[str, str] = {}

    def lose_upload_response(request: httpx.Request) -> httpx.Response:
        committed["name"] = json.loads(request.content)["script"]["name"]
        raise httpx.ReadTimeout("response lost after script commit")

    def list_committed_script(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "scripts": [{"id": "script-committed", "name": committed["name"]}],
                "count": 1,
            },
        )

    upload = respx.post(f"{BASE}/api/v1/scripts").mock(side_effect=lose_upload_response)
    respx.get(f"{BASE}/api/v1/scripts").mock(side_effect=list_committed_script)
    create_test = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json())
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert upload.call_count == 1
    body = json.loads(create_test.calls.last.request.content)
    assert body["groups"][0]["script_id"] == "script-committed"
    assert_all_calls_authed()


@respx.mock
async def test_provision_is_get_or_create_across_a_fresh_instance() -> None:
    """Simulated process restart: a FRESH adapter re-provisioning the same key
    finds the test by name via GET /api/v1/tests and creates nothing."""
    existing = managed_test_json(status="RUNNING")
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(
            200,
            json={
                "tests": [managed_test_json(test_id="test-other", name="unrelated"), existing],
                "count": 2,
            },
        )
    )
    create_test = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json())
    )
    create_script = respx.post(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(201, json={"id": "script-456"})
    )

    fresh_adapter = make_adapter()  # no in-memory state carried over
    handle = await fresh_adapter.provision(make_spec())
    assert handle.external_run_id == TEST_ID
    assert not create_test.called, "re-provision must not create a second remote test"
    assert not create_script.called
    assert_all_calls_authed()


@respx.mock
async def test_provision_inline_and_named_refs_split_vusers() -> None:
    inline = json.dumps(
        {
            "name": "custom",
            "protocol": "http",
            "config": {"base_url": "https://x.test"},
            "actions": [{"name": "a", "type": "http", "method": "GET", "target": "/"}],
        }
    )
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    script_route = respx.post(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(201, json={"id": "script-up-1"})
    )
    create_route = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json())
    )
    spec = make_spec(script_refs=[inline, "script-existing"], vusers=51)
    await make_adapter().provision(spec)

    assert script_route.call_count == 1  # only the inline ref uploads
    body = json.loads(create_route.calls.last.request.content)
    assert [g["script_id"] for g in body["groups"]] == ["script-up-1", "script-existing"]
    assert [g["vusers"] for g in body["groups"]] == [26, 25]  # remainder to the first group
    assert_all_calls_authed()


async def test_validate_rejects_more_script_groups_than_requested_vusers() -> None:
    report = await make_adapter().validate(
        make_spec(script_refs=["script-a", "script-b"], vusers=1)
    )

    assert report.ok is False
    assert "must be >= script_refs count" in report.issues[0]


@respx.mock
async def test_provision_refuses_to_amplify_vusers_even_if_validate_was_skipped() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    with pytest.raises(ValueError, match="refusing to amplify"):
        await make_adapter().provision(make_spec(script_refs=["script-a", "script-b"], vusers=1))


@respx.mock
async def test_provision_default_workload_requires_target_environment() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    with pytest.raises(ValueError, match="target_environment"):
        await make_adapter().provision(make_spec(target_environment=None))


# ── start (contract req. 2: idempotent) ───────────────────────────────────────


@respx.mock
async def test_start_posts_start() -> None:
    route = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(
            200, json={"status": "started", "test_id": TEST_ID, "test": managed_test_json()}
        )
    )
    await make_adapter().start(make_handle())
    assert route.called
    assert_all_calls_authed()


@respx.mock
async def test_start_tolerates_already_started() -> None:
    # startableTestStatusError wording from pkg/api/runner.go
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "test is RUNNING, must be QUEUED, PENDING, RESERVED, "
                "SCHEDULED, or SHAKEOUT_REVIEW to start"
            },
        )
    )
    await make_adapter().start(make_handle())  # must not raise
    assert_all_calls_authed()


@respx.mock
async def test_start_raises_on_other_400() -> None:
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(400, json={"error": "no generators configured"})
    )
    with pytest.raises(ValueError, match="no generators configured"):
        await make_adapter().start(make_handle())


@respx.mock
async def test_start_missing_test_raises_keyerror() -> None:
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(404, json={"error": "test not found"})
    )
    with pytest.raises(KeyError):
        await make_adapter().start(make_handle())


# ── get_status (contract req. 3: cheap, poll-safe, faithful mapping) ──────────


@pytest.mark.parametrize(
    ("remote_status", "expected_phase"),
    [
        ("DRAFT", EngineRunPhase.PROVISIONING),
        ("QUEUED", EngineRunPhase.READY),
        ("PENDING", EngineRunPhase.READY),
        ("RESERVED", EngineRunPhase.READY),
        ("SCHEDULED", EngineRunPhase.READY),
        ("SHAKEOUT_REVIEW", EngineRunPhase.READY),
        ("INITIALIZING", EngineRunPhase.RUNNING),
        ("SHAKEOUT", EngineRunPhase.RUNNING),
        ("RUNNING", EngineRunPhase.RUNNING),
        ("PAUSED", EngineRunPhase.RUNNING),
        ("STOPPING", EngineRunPhase.STOPPING),
        ("COMPLETE", EngineRunPhase.COMPLETED),
        ("FAILED", EngineRunPhase.FAILED),
        ("ABORTED", EngineRunPhase.ABORTED),
        ("SOMETHING_NEW", EngineRunPhase.RUNNING),  # unmapped -> safe non-terminal
    ],
)
@respx.mock
async def test_get_status_maps_every_remote_state(
    remote_status: str, expected_phase: EngineRunPhase
) -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status=remote_status))
    )
    status = await make_adapter().get_status(make_handle())
    assert status.phase is expected_phase
    if expected_phase in (EngineRunPhase.COMPLETED, EngineRunPhase.FAILED, EngineRunPhase.ABORTED):
        assert status.progress_pct == 100.0
    assert_all_calls_authed()


@respx.mock
async def test_get_status_normalizes_live_stats_and_progress() -> None:
    started = (datetime.now(UTC) - timedelta(seconds=150)).isoformat()
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(
                status="RUNNING", started_at=started, live_metrics=live_metrics_json()
            ),
        )
    )
    status = await make_adapter().get_status(make_handle())
    assert status.phase is EngineRunPhase.RUNNING
    stats = status.live_stats
    assert stats is not None
    assert stats.vusers == 42.0
    assert stats.tps == 94.4
    assert stats.error_rate == pytest.approx(0.025)  # error_pct 2.5% -> fraction
    assert stats.p95_ms == 482.5
    # 150s into a 300s run: ~50%, clamped to [0, 99]
    assert 40.0 <= status.progress_pct <= 60.0
    assert_all_calls_authed()


@respx.mock
async def test_get_status_without_metrics_has_no_live_stats() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="QUEUED"))
    )
    status = await make_adapter().get_status(make_handle())
    assert status.live_stats is None
    assert status.progress_pct == 0.0
    assert_all_calls_authed()


@respx.mock
async def test_get_status_failed_carries_remote_error() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200, json=managed_test_json(status="FAILED", error="generator crashed")
        )
    )
    status = await make_adapter().get_status(make_handle())
    assert status.phase is EngineRunPhase.FAILED
    assert status.message is not None and "generator crashed" in status.message


@respx.mock
async def test_get_status_404_raises_keyerror() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(404, json={"error": "test not found"})
    )
    with pytest.raises(KeyError):
        await make_adapter().get_status(make_handle())


@respx.mock
async def test_error_mapping_auth_and_server_errors() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(401, json={"error": "authentication required"})
    )
    with pytest.raises(RuntimeError, match="credentials"):
        await make_adapter().get_status(make_handle())

    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await make_adapter().get_status(make_handle())

    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(RuntimeError, match="before a response arrived"):
        await make_adapter().get_status(make_handle())


@respx.mock
async def test_successful_non_json_status_response_is_contextual_error() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, text="<html>proxy login</html>")
    )

    with pytest.raises(RuntimeError, match=r"GET /api/v1/tests/test-789 returned invalid JSON"):
        await make_adapter().get_status(make_handle())


def test_unprovisioned_handle_is_rejected() -> None:
    handle = EngineHandle(engine="apex_load", idempotency_key=KEY)
    with pytest.raises(ValueError, match="provision"):
        make_adapter()._run_id(handle)  # noqa: SLF001 — shared guard for every method


# ── abort (contract req. 4: idempotent) ───────────────────────────────────────


@respx.mock
async def test_abort_posts_abort() -> None:
    route = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(200, json={"status": "aborted", "test_id": TEST_ID})
    )
    await make_adapter().abort(make_handle(), reason="poll timeout")
    assert route.called
    assert_all_calls_authed()


@respx.mock
async def test_abort_is_idempotent_on_terminal_or_missing_test() -> None:
    # Second abort of a finished run: the Go backend rejects with 400.
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(400, json={"error": "test backend does not support stop"})
    )
    await make_adapter().abort(make_handle(), reason="retry")  # must not raise

    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(404, json={"error": "test not found"})
    )
    await make_adapter().abort(make_handle(), reason="retry")  # must not raise
    assert_all_calls_authed()


# ── teardown (documented no-op) ───────────────────────────────────────────────


async def test_teardown_is_a_noop_and_never_raises() -> None:
    await make_adapter().teardown(make_handle())  # no respx mock: must not call out
    await make_adapter().teardown(EngineHandle(engine="apex_load", idempotency_key=KEY))


# ── collect_artifacts (contract req. 5) ───────────────────────────────────────


@respx.mock
async def test_collect_artifacts_streams_archive_report() -> None:
    report = archive_report_json()
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )
    store = RecordingStore()
    refs = await make_adapter().collect_artifacts(make_handle(), store)

    assert len(store.puts) == 1
    key, data, content_type = store.puts[0]
    expected_key = engine_artifact_key(KEY, "apex-load-report.json")
    assert key == expected_key
    assert content_type == "application/json"
    assert json.loads(data) == report

    assert len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "engine_report"
    assert ref["name"] == "apex-load-report.json"
    assert ref["uri"] == f"s3://apex-artifacts/{expected_key}"
    assert ref["key"] == expected_key
    assert ref["media_type"] == "application/json"
    assert TEST_ID in (ref["summary"] or "")
    assert_all_calls_authed()


@respx.mock
async def test_collect_artifacts_retries_transient_stream_status() -> None:
    report = archive_report_json()
    route = respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        side_effect=[
            httpx.Response(503, json={"error": "temporarily unavailable"}),
            httpx.Response(200, json=report),
        ]
    )

    store = RecordingStore()
    refs = await make_adapter().collect_artifacts(make_handle(), store)

    assert route.call_count == 2
    assert len(refs) == 1
    assert json.loads(store.puts[0][1]) == report


@respx.mock
async def test_collect_artifacts_caps_large_stream_error_preview() -> None:
    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                self.yielded += 1
                yield b"x" * 4096

        async def aclose(self) -> None:
            self.closed = True

    stream = CountingStream()
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(418, stream=stream)
    )

    with pytest.raises(RuntimeError, match="HTTP 418"):
        await make_adapter().collect_artifacts(make_handle(), RecordingStore())

    assert 0 < stream.yielded < 100
    assert stream.closed is True


@respx.mock
async def test_collect_artifact_enforces_configured_stream_limit() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, content=b"too-large")
    )
    store = RecordingStore()
    with pytest.raises(ValueError, match="maximum size of 4 bytes"):
        await make_adapter(max_report_bytes=4).collect_artifacts(make_handle(), store)
    assert store.puts == []


@respx.mock
async def test_collect_artifacts_tolerates_missing_archive() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(404, json={"error": "archive not found"})
    )
    store = RecordingStore()
    refs = await make_adapter().collect_artifacts(make_handle(), store)
    assert refs == []
    assert store.puts == []
    assert_all_calls_authed()


# ── fetch_summary (contract req. 5: normalized KPIs + SLA verdict) ────────────


@respx.mock
async def test_fetch_summary_passed_with_normalized_kpis() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=archive_report_json())
    )
    summary = await make_adapter().fetch_summary(make_handle())
    assert summary.engine == "apex_load"
    assert summary.passed is True
    assert summary.sla_breaches == []
    assert summary.kpis["tps_avg"] == pytest.approx(98.0)  # mean of timeline tps
    assert summary.kpis["p95_ms"] == pytest.approx(450.0)  # mean of timeline p95
    assert summary.kpis["error_rate"] == pytest.approx(0.01)  # 1.0% -> fraction
    assert summary.kpis["vusers_peak"] == 50.0
    assert_all_calls_authed()


@respx.mock
async def test_fetch_summary_sla_breach_fails_with_details() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(
            200,
            json=sla_status_json(
                breached=True,
                details=["P95 latency breached threshold", "Error percentage breached threshold"],
            ),
        )
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=archive_report_json(error_pct=4.0))
    )
    summary = await make_adapter().fetch_summary(make_handle())
    assert summary.passed is False
    assert summary.sla_breaches == [
        "P95 latency breached threshold",
        "Error percentage breached threshold",
    ]
    assert summary.kpis["error_rate"] == pytest.approx(0.04)
    assert_all_calls_authed()


@respx.mock
async def test_fetch_summary_failed_run_without_archive_uses_live_metrics() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json(status="FAILED"))
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(404, json={"error": "archive not found"})
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(
                status="FAILED", error="generator crashed", live_metrics=live_metrics_json()
            ),
        )
    )
    summary = await make_adapter().fetch_summary(make_handle())
    assert summary.passed is False  # not COMPLETE -> never passed
    assert summary.kpis["error_rate"] == pytest.approx(0.025)
    assert summary.kpis["vusers_peak"] == 50.0  # total_vusers
    assert summary.notes is not None and "live metrics" in summary.notes
    assert_all_calls_authed()


@respx.mock
async def test_fetch_summary_bounds_oversized_archive_and_uses_live_metrics() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, content=b'{"padding":"' + (b"x" * 1000) + b'"}')
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(status="COMPLETE", live_metrics=live_metrics_json()),
        )
    )

    summary = await make_adapter(max_report_bytes=64).fetch_summary(make_handle())

    assert summary.kpis["error_rate"] == pytest.approx(0.025)
    assert summary.notes is not None and "oversized" in summary.notes
    assert_all_calls_authed()


# ── registration ──────────────────────────────────────────────────────────────


def test_registered_with_adapter_registry() -> None:
    import apex.adapters  # noqa: F401 — side-effect import registers providers
    from apex.adapters.registry import AdapterRegistry

    assert "apex_load" in AdapterRegistry.providers_for(PortKind.EXECUTION_ENGINE)
