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
from structlog.testing import capture_logs

import apex.adapters.apex_load.engine as apex_load_engine
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
from apex.ports.execution_engine import EngineProviderRunNotFoundError, EngineRunPhase

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


def test_constructor_rejects_report_limit_above_server_ceiling() -> None:
    with pytest.raises(ValueError, match="max_report_bytes"):
        make_adapter(max_report_bytes=apex_load_engine._HARD_MAX_REPORT_BYTES + 1)


@pytest.mark.parametrize(
    ("option", "value", "match"),
    [
        ("base_url", True, "base_url"),
        ("project_id", 7, "project_id"),
        ("project_id", "../../other-project", "project_id"),
        ("max_report_bytes", True, "integer"),
        ("max_report_bytes", "1024", "integer"),
    ],
)
def test_constructor_rejects_coercible_or_unsafe_options(
    option: str, value: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        make_adapter(**{option: value})


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


def mock_owned_test(*, status: str = "RUNNING") -> respx.Route:
    return respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status=status))
    )


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


def created_script_response(script_id: str) -> Any:
    """Echo the provider-owned script identity from one create request."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        script = payload["script"]
        return httpx.Response(
            201,
            json={
                "id": script_id,
                "name": script["name"],
                "protocol": script.get("protocol"),
                **({"project_id": payload["project_id"]} if "project_id" in payload else {}),
            },
        )

    return respond


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


def test_parse_go_duration_rejects_unbounded_values() -> None:
    assert parse_go_duration_s("1" * 129 + "s") is None
    assert parse_go_duration_s("999999999999999999999999999999999999999h") is None


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


@pytest.mark.parametrize(
    "api_key",
    ["unsafe-api-key\r\nInjected: value", "k" * 16_385, "non-ascii-\N{SNOWMAN}"],
)
def test_constructor_rejects_unsafe_or_oversized_api_key_without_reflection(
    api_key: str,
) -> None:
    conn = ConnectionConfig(
        id="apexload-credential-boundary",
        kind=PortKind.EXECUTION_ENGINE,
        provider="apex_load",
        name="APEX Load",
        options={"base_url": BASE},
    )

    with pytest.raises(ValueError) as error:
        ApexLoadExecutionEngine(conn, SecretValue(value=api_key))

    assert api_key not in str(error.value)


@pytest.mark.parametrize(
    "idempotency_key",
    ["unsafe\r\nInjected: value", "non-ascii-\N{SNOWMAN}"],
)
async def test_spec_rejects_non_header_safe_idempotency_key_before_provider_io(
    idempotency_key: str,
) -> None:
    spec = make_spec(idempotency_key=idempotency_key)

    report = await make_adapter().validate(spec)

    assert report.ok is False
    assert report.issues == ["load test specification failed structural validation"]
    assert idempotency_key not in str(report)


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
    assert report.issues == ["load test specification failed structural validation"]


async def test_provision_revalidates_a_model_copy_before_provider_io() -> None:
    spec = make_spec().model_copy(update={"duration_s": float("nan")})

    with pytest.raises(ValueError, match="structural validation"):
        await make_adapter().provision(spec)


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
async def test_validate_redacts_provider_credentials_before_domain_output() -> None:
    canary = "ghp_" + ("A" * 24)
    respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(
            200,
            json=validation_response_json(
                valid=False,
                issues=[f"provider diagnostic included {canary}"],
            ),
        )
    )

    report = await make_adapter().validate(make_spec())

    assert report.ok is False
    assert canary not in repr(report)
    assert "[REDACTED]" in report.issues[0]


@respx.mock
async def test_validate_rejects_non_list_remote_issues() -> None:
    respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(200, json={"valid": False, "issues": "unbounded text"})
    )

    with pytest.raises(RuntimeError, match="field 'issues' must be a list"):
        await make_adapter().validate(make_spec())


@respx.mock
async def test_validate_requires_an_exact_provider_boolean() -> None:
    respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(200, json={"valid": "false", "issues": []})
    )

    with pytest.raises(RuntimeError, match="must be a boolean"):
        await make_adapter().validate(make_spec())


@respx.mock
async def test_validate_rejects_remote_issue_amplification() -> None:
    respx.post(f"{BASE}/api/v1/scripts/validate").mock(
        return_value=httpx.Response(
            200,
            json={"valid": False, "issues": [f"issue-{index}" for index in range(129)]},
        )
    )

    with pytest.raises(RuntimeError, match="128-message limit"):
        await make_adapter().validate(make_spec())


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
        side_effect=created_script_response("script-456")
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
    respx.post(f"{BASE}/api/v1/scripts").mock(side_effect=created_script_response("script-loser"))
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
        side_effect=created_script_response("script-committed")
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(
        side_effect=httpx.ReadTimeout("response lost after remote commit")
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert create.call_count == 1
    assert create.calls.last.request.headers["Idempotency-Key"] == KEY
    assert_all_calls_authed()


@pytest.mark.parametrize("status_code", [408, 429, 502, 503])
@respx.mock
async def test_provision_reconciles_test_after_ambiguous_transient_response(
    status_code: int,
) -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        side_effect=[
            httpx.Response(200, json={"tests": [], "count": 0}),
            httpx.Response(200, json={"tests": [managed_test_json()], "count": 1}),
        ]
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        side_effect=created_script_response("script-committed")
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(status_code, json={"error": "proxy failure after commit"})
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert create.called
    assert_all_calls_authed()


@pytest.mark.parametrize("response_kind", ["malformed_json", "missing_id"])
@respx.mock
async def test_provision_reconciles_ambiguous_successful_create_response(
    response_kind: str,
) -> None:
    """A committed 2xx with an unusable body must still adopt the named test."""

    respx.get(f"{BASE}/api/v1/tests").mock(
        side_effect=[
            httpx.Response(200, json={"tests": [], "count": 0}),
            httpx.Response(200, json={"tests": [managed_test_json()], "count": 1}),
        ]
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        side_effect=created_script_response("script-committed")
    )
    create_response = (
        httpx.Response(201, text="<html>proxy replaced the body</html>")
        if response_kind == "malformed_json"
        else httpx.Response(201, json={})
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(return_value=create_response)

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert create.call_count == 1
    assert create.calls.last.request.headers["Idempotency-Key"] == KEY
    assert_all_calls_authed()


@pytest.mark.parametrize(
    "wrong_identity",
    [
        {"id": "test-wrong", "name": "unrelated-test", "project_id": "proj-123"},
        {"id": "test-wrong", "name": TEST_NAME, "project_id": "proj-other"},
    ],
)
@respx.mock
async def test_provision_reconciles_wrong_target_create_acknowledgement(
    wrong_identity: dict[str, str],
) -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        side_effect=[
            httpx.Response(200, json={"tests": [], "count": 0}),
            httpx.Response(200, json={"tests": [managed_test_json()], "count": 1}),
        ]
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        side_effect=created_script_response("script-committed")
    )
    create = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json(**wrong_identity))
    )

    handle = await make_adapter().provision(make_spec())

    assert create.call_count == 1
    assert handle.external_run_id == TEST_ID


@respx.mock
async def test_provision_rejects_same_named_test_from_wrong_project() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(
            200,
            json={
                "tests": [managed_test_json(project_id="proj-other")],
                "count": 1,
            },
        )
    )

    with pytest.raises(RuntimeError, match="unexpected project"):
        await make_adapter().provision(make_spec())

    assert len(respx.calls) == 1


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


@pytest.mark.parametrize("mismatch", ["name", "project"])
@respx.mock
async def test_provision_reconciles_wrong_target_script_acknowledgement(
    mismatch: str,
) -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    committed: dict[str, str] = {}

    def wrong_acknowledgement(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        committed["name"] = payload["script"]["name"]
        return httpx.Response(
            201,
            json={
                "id": "script-wrong",
                "name": "unrelated-script" if mismatch == "name" else committed["name"],
                "project_id": "proj-other" if mismatch == "project" else "proj-123",
            },
        )

    respx.post(f"{BASE}/api/v1/scripts").mock(side_effect=wrong_acknowledgement)
    respx.get(f"{BASE}/api/v1/scripts").mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "scripts": [
                    {
                        "id": "script-committed",
                        "name": committed["name"],
                        "project_id": "proj-123",
                    }
                ],
                "count": 1,
            },
        )
    )
    create_test = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json())
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    body = json.loads(create_test.calls.last.request.content)
    assert body["groups"][0]["script_id"] == "script-committed"


@respx.mock
async def test_provision_reconciles_script_after_ambiguous_transient_response() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    committed: dict[str, str] = {}

    def ambiguous_upload(request: httpx.Request) -> httpx.Response:
        committed["name"] = json.loads(request.content)["script"]["name"]
        return httpx.Response(503, json={"error": "proxy failure after commit"})

    respx.post(f"{BASE}/api/v1/scripts").mock(side_effect=ambiguous_upload)
    respx.get(f"{BASE}/api/v1/scripts").mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "scripts": [{"id": "script-committed", "name": committed["name"]}],
                "count": 1,
            },
        )
    )
    create_test = respx.post(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(201, json=managed_test_json())
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == TEST_ID
    assert json.loads(create_test.calls.last.request.content)["groups"][0]["script_id"] == (
        "script-committed"
    )
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
        side_effect=created_script_response("script-456")
    )

    fresh_adapter = make_adapter()  # no in-memory state carried over
    handle = await fresh_adapter.provision(make_spec())
    assert handle.external_run_id == TEST_ID
    assert not create_test.called, "re-provision must not create a second remote test"
    assert not create_script.called
    assert_all_calls_authed()


@respx.mock
async def test_provision_rejects_duplicate_idempotency_named_tests() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(
            200,
            json={
                "tests": [
                    managed_test_json(test_id="test-first"),
                    managed_test_json(test_id="test-second"),
                ],
                "count": 2,
            },
        )
    )

    with pytest.raises(RuntimeError, match="multiple tests"):
        await make_adapter().provision(make_spec())


@respx.mock
async def test_script_reconciliation_rejects_duplicate_content_bound_names() -> None:
    name = f"{TEST_NAME} script-deadbeefdeadbeef"
    respx.get(f"{BASE}/api/v1/scripts").mock(
        return_value=httpx.Response(
            200,
            json={
                "scripts": [
                    {"id": "script-first", "name": name},
                    {"id": "script-second", "name": name},
                ],
                "count": 2,
            },
        )
    )

    with pytest.raises(RuntimeError, match="multiple scripts"):
        await make_adapter()._find_script_by_name(name)


@respx.mock
async def test_provision_rejects_an_unsafe_provider_test_id() -> None:
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(
            200,
            json={
                "tests": [managed_test_json(test_id="../../other-test/start")],
                "count": 1,
            },
        )
    )

    with pytest.raises(RuntimeError, match="safe non-empty identifier"):
        await make_adapter().provision(make_spec())

    assert len(respx.calls) == 1


@respx.mock
async def test_provision_rejects_provider_token_id_before_handle_or_log() -> None:
    canary = "ghp_" + ("B" * 24)
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(
            200,
            json={"tests": [managed_test_json(test_id=canary)], "count": 1},
        )
    )

    with capture_logs() as logs, pytest.raises(RuntimeError, match="safe non-empty identifier"):
        await make_adapter().provision(make_spec())

    assert canary not in repr(logs)


@respx.mock
async def test_reconciled_script_token_id_is_rejected_before_log() -> None:
    canary = "ghp_" + ("C" * 24)
    committed: dict[str, str] = {}
    respx.get(f"{BASE}/api/v1/tests").mock(
        return_value=httpx.Response(200, json={"tests": [], "count": 0})
    )
    respx.post(f"{BASE}/api/v1/scripts").mock(
        side_effect=lambda request: (
            committed.update(name=json.loads(request.content)["script"]["name"])
            or httpx.Response(503, json={"error": "commit acknowledgement lost"})
        )
    )
    respx.get(f"{BASE}/api/v1/scripts").mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={"scripts": [{"id": canary, "name": committed["name"]}], "count": 1},
        )
    )

    with capture_logs() as logs, pytest.raises(RuntimeError, match="safe non-empty identifier"):
        await make_adapter().provision(make_spec())

    assert canary not in repr(logs)


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
        side_effect=created_script_response("script-up-1")
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
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json())
    )
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
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="RUNNING"))
    )
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
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json())
    )
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(400, json={"error": "no generators configured"})
    )
    with pytest.raises(ValueError, match="no generators configured"):
        await make_adapter().start(make_handle())


@respx.mock
async def test_start_missing_test_raises_keyerror() -> None:
    start = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(200, json={"status": "started", "test_id": TEST_ID})
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(404, json={"error": "test not found"})
    )
    with pytest.raises(KeyError):
        await make_adapter().start(make_handle())
    assert not start.called


@pytest.mark.parametrize("mismatch", ["id", "name", "project"])
@respx.mock
async def test_start_rejects_wrong_target_preflight_without_starting(mismatch: str) -> None:
    test = managed_test_json()
    if mismatch == "id":
        test["id"] = "test-other"
    elif mismatch == "name":
        test["name"] = "apex-orch:other-run"
    else:
        test["project_id"] = "proj-other"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(return_value=httpx.Response(200, json=test))
    start = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(200, json={"status": "started", "test_id": TEST_ID})
    )

    with pytest.raises(RuntimeError, match="unexpected (?:test|project)"):
        await make_adapter().start(make_handle())

    assert not start.called


@respx.mock
async def test_start_rejects_wrong_target_acknowledgement() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json())
    )
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/start").mock(
        return_value=httpx.Response(200, json={"status": "started", "test_id": "test-other"})
    )

    with pytest.raises(RuntimeError, match="unexpected test id"):
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


@pytest.mark.parametrize(
    "identity_override",
    [
        {"id": "test-other"},
        {"name": "apex-orch:another-key"},
        {"project_id": "proj-other"},
    ],
)
@respx.mock
async def test_get_status_rejects_wrong_target_test_response(
    identity_override: dict[str, str],
) -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(status="RUNNING", **identity_override),
        )
    )

    with pytest.raises(RuntimeError, match="unexpected test|unexpected project"):
        await make_adapter().get_status(make_handle())


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_vusers", True),
        ("tps", -1),
        ("error_pct", 101),
        ("p95_ms", "482.5"),
    ],
)
@respx.mock
async def test_get_status_rejects_malformed_live_metrics(field: str, value: Any) -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(
                status="RUNNING",
                live_metrics=live_metrics_json(**{field: value}),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="live_metrics"):
        await make_adapter().get_status(make_handle())


@pytest.mark.parametrize("status", [True, 7, "RUNNING\x00FAILED", "x" * 65])
@respx.mock
async def test_get_status_rejects_malformed_provider_status(status: Any) -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status=status))
    )

    with pytest.raises(RuntimeError, match="field 'status'"):
        await make_adapter().get_status(make_handle())


@respx.mock
async def test_get_status_rejects_provider_token_status_before_public_message() -> None:
    canary = "sk_test_" + ("A" * 20)
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status=canary))
    )

    with pytest.raises(RuntimeError, match="unsafe material") as raised:
        await make_adapter().get_status(make_handle())

    assert canary not in str(raised.value)


@respx.mock
async def test_get_status_rejects_boolean_duration_metadata() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(status="RUNNING", duration_ns=True),
        )
    )

    with pytest.raises(RuntimeError, match="duration_ns"):
        await make_adapter().get_status(make_handle())


@respx.mock
async def test_get_status_invalid_timestamp_does_not_retain_provider_value() -> None:
    canary = "bare-provider-timestamp-secret-canary"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(
            200,
            json=managed_test_json(status="RUNNING", started_at=canary),
        )
    )

    with pytest.raises(RuntimeError, match="ISO-8601 timestamp") as excinfo:
        await make_adapter().get_status(make_handle())

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


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
async def test_get_status_404_raises_definitive_provider_not_found() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(404, json={"error": "test not found"})
    )
    with pytest.raises(EngineProviderRunNotFoundError):
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


def test_unsafe_persisted_handle_id_is_rejected_before_network_io() -> None:
    with pytest.raises(ValueError, match="invalid external_run_id"):
        make_adapter()._run_id(make_handle("../../other-test/start"))  # noqa: SLF001


# ── abort (contract req. 4: idempotent) ───────────────────────────────────────


@respx.mock
async def test_abort_posts_abort() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="RUNNING"))
    )
    route = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(200, json={"status": "aborted", "test_id": TEST_ID})
    )
    await make_adapter().abort(make_handle(), reason="poll timeout")
    assert route.called
    assert_all_calls_authed()


@respx.mock
async def test_abort_logs_only_non_content_reason_metadata() -> None:
    secret_reason = "opaque-api-key-value-that-redaction-cannot-recognize"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="RUNNING"))
    )
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(200, json={"status": "aborted", "test_id": TEST_ID})
    )

    with capture_logs() as logs:
        await make_adapter().abort(make_handle(), reason=secret_reason)

    assert secret_reason not in repr(logs)
    event = next(log for log in logs if log.get("event") == "apex_load.abort")
    assert event["reason_present"] is True
    assert event["reason_length"] == len(secret_reason)


@respx.mock
async def test_abort_is_idempotent_on_terminal_or_missing_test() -> None:
    # Second abort of a finished run: the Go backend rejects with 400.
    get = respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="FINISHED"))
    )
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(400, json={"error": "test backend does not support stop"})
    )
    await make_adapter().abort(make_handle(), reason="retry")  # must not raise

    get.mock(return_value=httpx.Response(404, json={"error": "test not found"}))
    await make_adapter().abort(make_handle(), reason="retry")  # must not raise
    assert_all_calls_authed()


@pytest.mark.parametrize("mismatch", ["id", "name", "project"])
@respx.mock
async def test_abort_rejects_wrong_target_preflight_without_aborting(mismatch: str) -> None:
    test = managed_test_json(status="RUNNING")
    if mismatch == "id":
        test["id"] = "test-other"
    elif mismatch == "name":
        test["name"] = "apex-orch:other-run"
    else:
        test["project_id"] = "proj-other"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(return_value=httpx.Response(200, json=test))
    abort = respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(200, json={"status": "aborted", "test_id": TEST_ID})
    )

    with pytest.raises(RuntimeError, match="unexpected (?:test|project)"):
        await make_adapter().abort(make_handle(), reason="poll timeout")

    assert not abort.called


@respx.mock
async def test_abort_rejects_wrong_target_acknowledgement() -> None:
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="RUNNING"))
    )
    respx.post(f"{BASE}/api/v1/tests/{TEST_ID}/abort").mock(
        return_value=httpx.Response(200, json={"status": "aborted", "test_id": "test-other"})
    )

    with pytest.raises(RuntimeError, match="unexpected test id"):
        await make_adapter().abort(make_handle(), reason="poll timeout")


# ── teardown (documented no-op) ───────────────────────────────────────────────


async def test_teardown_is_a_noop_and_never_raises() -> None:
    await make_adapter().teardown(make_handle())  # no respx mock: must not call out
    await make_adapter().teardown(EngineHandle(engine="apex_load", idempotency_key=KEY))


# ── collect_artifacts (contract req. 5) ───────────────────────────────────────


@respx.mock
async def test_collect_artifacts_streams_archive_report() -> None:
    mock_owned_test()
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
    mock_owned_test()
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

    mock_owned_test()
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
    mock_owned_test()
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, content=b"too-large")
    )
    store = RecordingStore()
    with pytest.raises(ValueError, match="maximum size of 4 bytes"):
        await make_adapter(max_report_bytes=4).collect_artifacts(make_handle(), store)
    assert store.puts == []


@respx.mock
async def test_collect_artifacts_tolerates_missing_archive() -> None:
    mock_owned_test()
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(404, json={"error": "archive not found"})
    )
    store = RecordingStore()
    refs = await make_adapter().collect_artifacts(make_handle(), store)
    assert refs == []
    assert store.puts == []
    assert_all_calls_authed()


@respx.mock
async def test_collect_artifacts_rejects_wrong_target_before_archive_download() -> None:
    wrong = managed_test_json(status="FINISHED", project_id="proj-other")
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(return_value=httpx.Response(200, json=wrong))
    archive = respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=archive_report_json())
    )

    with pytest.raises(RuntimeError, match="unexpected project"):
        await make_adapter().collect_artifacts(make_handle(), RecordingStore())

    assert not archive.called


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
async def test_fetch_summary_rejects_wrong_target_sla_response_before_archive() -> None:
    payload = sla_status_json()
    payload["test_id"] = "test-other"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=payload)
    )
    archive = respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=archive_report_json())
    )

    with pytest.raises(RuntimeError, match="unexpected test id"):
        await make_adapter().fetch_summary(make_handle())

    assert not archive.called


@respx.mock
async def test_fetch_summary_rejects_wrong_target_archive_manifest() -> None:
    report = archive_report_json()
    report["manifest"]["test_id"] = "test-other"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )

    with pytest.raises(RuntimeError, match="unexpected test id"):
        await make_adapter().fetch_summary(make_handle())


@respx.mock
async def test_fetch_summary_supports_legacy_archive_after_independent_identity_check() -> None:
    report = archive_report_json()
    del report["manifest"]
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )
    identity = respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(
        return_value=httpx.Response(200, json=managed_test_json(status="COMPLETE"))
    )

    summary = await make_adapter().fetch_summary(make_handle())

    assert summary.kpis == {
        "tps_avg": 98.0,
        "p95_ms": 450.0,
        "error_rate": 0.01,
        "vusers_peak": 50.0,
    }
    assert "legacy archive identity verified" in (summary.notes or "")
    assert identity.call_count == 1


@respx.mock
async def test_fetch_summary_rejects_legacy_archive_when_test_identity_is_wrong() -> None:
    report = archive_report_json()
    del report["manifest"]
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )
    wrong = managed_test_json(status="COMPLETE", name="apex-orch:other-run")
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}").mock(return_value=httpx.Response(200, json=wrong))

    with pytest.raises(RuntimeError, match="unexpected test name"):
        await make_adapter().fetch_summary(make_handle())


@respx.mock
async def test_fetch_summary_rejects_explicitly_malformed_archive_manifest() -> None:
    report = archive_report_json()
    report["manifest"] = None
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )

    with pytest.raises(RuntimeError, match="no valid manifest"):
        await make_adapter().fetch_summary(make_handle())


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
async def test_fetch_summary_rejects_non_list_sla_details_before_archive_fetch() -> None:
    sla = sla_status_json(breached=True)
    sla["details"] = "P95 latency breached threshold"
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla)
    )

    with pytest.raises(RuntimeError, match="field 'details' must be a list"):
        await make_adapter().fetch_summary(make_handle())

    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"sla_breached": "false"},
        {"sla_breached": 0},
        {"status": {"value": "COMPLETE"}},
    ],
)
@respx.mock
async def test_fetch_summary_rejects_malformed_sla_scalars_before_archive_fetch(
    mutation: dict[str, Any],
) -> None:
    payload = sla_status_json()
    payload.update(mutation)
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(RuntimeError):
        await make_adapter().fetch_summary(make_handle())

    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("summary_timeline", "not-a-list", "summary_timeline"),
        ("summary_timeline", [True], "must be an object"),
        ("summary_timeline", [{"tps": True}], "field 'tps'"),
        ("overview", {"error_pct": 101}, "error_pct"),
        ("by_action", {"load_root": {"transactions": True}}, "transactions"),
    ],
)
@respx.mock
async def test_fetch_summary_rejects_malformed_archive_semantics(
    field: str, value: Any, match: str
) -> None:
    report = archive_report_json()
    report[field] = value
    if field == "by_action":
        report["summary_timeline"] = []
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )

    with pytest.raises(RuntimeError, match=match):
        await make_adapter().fetch_summary(make_handle())


@respx.mock
async def test_fetch_summary_does_not_reflect_malformed_provider_action_name() -> None:
    canary = "bare-provider-action-secret-canary"
    report = archive_report_json()
    report["summary_timeline"] = []
    report["by_action"] = {canary: "not-an-object"}
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/sla-status").mock(
        return_value=httpx.Response(200, json=sla_status_json())
    )
    respx.get(f"{BASE}/api/v1/tests/{TEST_ID}/archive/report").mock(
        return_value=httpx.Response(200, json=report)
    )

    with pytest.raises(RuntimeError, match="action entry") as excinfo:
        await make_adapter().fetch_summary(make_handle())

    assert canary not in str(excinfo.value)


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
