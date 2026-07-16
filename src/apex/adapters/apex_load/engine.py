"""APEX Load execution-engine adapter (provider "apex_load", PortKind.EXECUTION_ENGINE).

Wire surface: APEX Load REST API v1 (Project_Stormrunner API_AND_DSL_REFERENCE.md;
the Go HTTP handlers in pkg/api are definitive where the reference is stale).
Connection options: {"base_url": "https://apexload.internal:8080",
"project_id"?: "proj-123"}; the secret is a service-account API key sent as the
X-APEXLoad-API-Key header (the server also accepts "Authorization: Bearer").

Wire-format decisions (documented mappings / deviations):

- Get-or-create (contract req. 1): provision() names the remote test
  "apex-orch:<idempotency_key>" and looks that name up via GET /api/v1/tests
  (project-scoped when project_id is set) inside a keyed creation guard before
  creating anything. POST /api/v1/tests also carries Idempotency-Key, so the
  provider atomically rejects concurrent creates from separate callers; the
  loser adopts the winning named test after 409. Re-provisioning after a crash
  — even from a fresh process — finds the existing run. The remote test name is
  the durable key registry; nothing is kept client-side.
- spec.script_refs convention: an entry starting with "{" is inline APEX Load
  JSON DSL (uploaded via POST /api/v1/scripts at provision time, wrapper shape
  {"project_id"?, "script": {...}} per pkg/api/handlers.go handleCreateScript);
  any other entry is an EXISTING APEX Load script id used as-is. An empty list
  generates a default single-GET HTTP workload against spec.target_environment.
- spec.vusers splits evenly across one group per script (remainder to the
  earliest groups). More scripts than users is rejected so effective load can
  never exceed the requested total; ramp_s/duration_s
  become Go duration strings ("30s").
- spec.slas: tps_avg -> goal_config.target_average_tps, p95_ms ->
  goal_config.p95_latency_ms, error_rate (0..1 fraction) ->
  goal_config.max_error_pct (percent). Other keys are ignored (documented
  deviation): APEX Load evaluates SLAs server-side via goal_config +
  GET /tests/{id}/sla-status, which keeps the adapter stateless.
- Status mapping (ManagedTest.Status values from pkg/api/runner.go):
  DRAFT -> provisioning; QUEUED/PENDING/RESERVED/SCHEDULED/SHAKEOUT_REVIEW ->
  ready; INITIALIZING/SHAKEOUT/RUNNING/PAUSED -> running; STOPPING -> stopping;
  COMPLETE -> completed; FAILED -> failed; ABORTED -> aborted. Unknown statuses
  map to running (non-terminal is the safe default for a poll loop) with the
  raw status preserved in the message. live_metrics.error_pct is a PERCENTAGE
  and is normalized to a 0..1 fraction.
- start() tolerates the remote's already-started rejection (HTTP 400 "test is
  RUNNING, must be QUEUED, PENDING, RESERVED, SCHEDULED, or SHAKEOUT_REVIEW to
  start"); abort() tolerates 400/404 (already terminal or gone). Both are
  idempotent (contract reqs. 2 and 4).
- teardown() is a documented no-op: APEX Load tests are immutable archives.
- collect_artifacts() streams GET /tests/{id}/archive/report into the artifact
  store (kind "engine_report", 60s download timeout). Aborted-before-start runs
  never archive, so a 404 yields zero artifacts rather than an error.
- fetch_summary(): passed/breaches come from GET /tests/{id}/sla-status; KPIs
  come from the archive report (tps_avg/p95_ms averaged over summary_timeline,
  falling back to overview.peak_tps and a transaction-weighted by_action p95),
  with a live-metrics fallback when the archive is missing.

The httpx.AsyncClient is lazy and per-instance, but rebuilt if the running
event loop changes (resolver-cached adapter instances are reused across the
short-lived loops that graph nodes spin up).
"""

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from apex.adapters.http_resilience import (
    CircuitBreaker,
    CircuitOpenError,
    close_response_definitively,
    parse_json_bytes,
    parse_json_response,
    read_bounded_response,
    read_stream_error_preview,
    require_identity_content_encoding,
    resilient_stream_request,
    retry_policy,
)
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.options import require_bounded_credential
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.adapters.remote_idempotency import remote_create_guard
from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
from apex.domain.integrations import (
    LoadTestSpec,
    SecretValue,
    TestResultSummary,
    ValidationReport,
)
from apex.domain.pipeline import ArtifactRef, EngineHandle
from apex.ports.artifact_store import ArtifactStorePort, engine_artifact_key
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineProviderRunNotFoundError,
    EngineRunPhase,
    EngineRunStatus,
    LiveStats,
)

logger = structlog.get_logger(__name__)

PROVIDER = "apex_load"
TEST_NAME_PREFIX = "apex-orch:"
_TIMEOUT_S = 15.0
_DOWNLOAD_TIMEOUT_S = 60.0
_DOWNLOAD_TOTAL_TIMEOUT_S = 10 * 60.0
_COLLECTION_TOTAL_TIMEOUT_S = 10 * 60.0
_DEFAULT_MAX_REPORT_BYTES = 256 * 1024 * 1024
_HARD_MAX_REPORT_BYTES = 256 * 1024 * 1024
_MAX_SUMMARY_REPORT_BYTES = 16 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
_TEST_LIST_PAGE_SIZE = 200
_MAX_TEST_RECONCILIATION_ROWS = 5_000
_MAX_SCRIPT_RECONCILIATION_ROWS = 5_000
_MAX_PROVIDER_MESSAGES = 128
_MAX_PROVIDER_MESSAGE_CHARS = 2_048
_MAX_PROVIDER_STATUS_CHARS = 64
_MAX_PROVIDER_TIMELINE_POINTS = 100_000
_MAX_PROVIDER_ACTIONS = 10_000
_MAX_PROVIDER_COUNT = 1_000_000_000
_MAX_PROVIDER_DURATION_S = 366 * 24 * 60 * 60
_CONFLICT_RECHECK_ATTEMPTS = 6
_CONFLICT_RECHECK_DELAY_S = 0.05

# ManagedTest.Status values -> EngineRunPhase (see module docstring).
_PHASE_BY_STATUS: dict[str, EngineRunPhase] = {
    "DRAFT": EngineRunPhase.PROVISIONING,
    "QUEUED": EngineRunPhase.READY,
    "PENDING": EngineRunPhase.READY,
    "RESERVED": EngineRunPhase.READY,
    "SCHEDULED": EngineRunPhase.READY,
    "SHAKEOUT_REVIEW": EngineRunPhase.READY,
    "INITIALIZING": EngineRunPhase.RUNNING,
    "SHAKEOUT": EngineRunPhase.RUNNING,
    "RUNNING": EngineRunPhase.RUNNING,
    "PAUSED": EngineRunPhase.RUNNING,
    "STOPPING": EngineRunPhase.STOPPING,
    "COMPLETE": EngineRunPhase.COMPLETED,
    "FAILED": EngineRunPhase.FAILED,
    "ABORTED": EngineRunPhase.ABORTED,
}

# startableTestStatusError (pkg/api/runner.go): the already-started rejection.
_ALREADY_STARTED_MARKER = "must be QUEUED"


class _RemoteConflictError(RuntimeError):
    """A provider-side 409 that may have an observable winning resource."""


class _AmbiguousRequestError(RuntimeError):
    """A create request that may have committed despite the observed failure."""


class _TransportRequestError(_AmbiguousRequestError):
    """A request whose response was lost, leaving create outcome ambiguous."""


class _AmbiguousResponseError(_AmbiguousRequestError):
    """A received transient response that can follow an upstream create commit."""


# ── Go duration helpers ───────────────────────────────────────────────────────

_GO_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_GO_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_NAMED_SCRIPT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


def parse_go_duration_s(value: Any) -> float | None:
    """ "5m30s" -> 330.0; None for anything that is not a Go duration string."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    text = value.strip()
    matches = list(_GO_DURATION_TOKEN.finditer(text))
    if not matches or "".join(match.group(0) for match in matches) != text:
        return None
    duration = sum(float(match.group(1)) * _GO_UNIT_S[match.group(2)] for match in matches)
    if not math.isfinite(duration) or not 0 <= duration <= _MAX_PROVIDER_DURATION_S:
        return None
    return duration


def format_go_duration(seconds: float) -> str:
    """Seconds -> Go duration string ("300s", "2.5s"); ParseDuration-compatible."""
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int | float)
        or not math.isfinite(float(seconds))
        or not 0 <= float(seconds) <= _MAX_PROVIDER_DURATION_S
    ):
        raise ValueError("duration seconds must be a finite non-negative number")
    return f"{seconds:g}s"


# ── spec -> wire helpers ──────────────────────────────────────────────────────


def test_name_for(idempotency_key: str) -> str:
    return f"{TEST_NAME_PREFIX}{idempotency_key}"


def _is_inline_ref(ref: str) -> bool:
    return ref.lstrip().startswith("{")


def _is_safe_named_ref(ref: str) -> bool:
    return _NAMED_SCRIPT_REF.fullmatch(ref) is not None and not contains_credential_material(ref)


async def _read_bounded_json_object(
    response: httpx.Response, context: str, *, max_bytes: int
) -> dict[str, Any]:
    payload = bytearray()
    try:
        async for chunk in response.aiter_raw():
            if len(payload) + len(chunk) > max_bytes:
                raise ValueError(f"{context} exceeds maximum size of {max_bytes} bytes")
            payload.extend(chunk)
    finally:
        await close_response_definitively(response)
    data: Any = None
    invalid_json = False
    try:
        buffered = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(payload),
        )
        data = parse_json_response(buffered, context=context)
    except RuntimeError:
        invalid_json = True
    if invalid_json:
        raise ValueError(f"{context} returned invalid JSON")
    if not isinstance(data, dict):
        raise ValueError(f"{context} returned a non-object JSON payload")
    return data


def _default_script(spec: LoadTestSpec) -> dict[str, Any]:
    """Generated default workload: a single GET / against spec.target_environment."""
    return {
        "name": f"{test_name_for(spec.idempotency_key)} script",
        "protocol": "http",
        "config": {"protocol": "http", "base_url": spec.target_environment or ""},
        "actions": [
            {
                "name": "load_root",
                "type": "http",
                "method": "GET",
                "target": "/",
                "assertions": [{"type": "status", "expected": 200}],
            }
        ],
    }


def _prepare_inline_script(
    parsed: dict[str, Any], spec: LoadTestSpec, index: int
) -> dict[str, Any]:
    script = dict(parsed)
    # Canonical names bind recovery to the idempotency scope instead of trusting
    # an arbitrary caller-supplied name that may identify unrelated content.
    script["name"] = f"{test_name_for(spec.idempotency_key)} script {index + 1}"
    script.setdefault("protocol", "http")
    return script


def _content_bound_script(script: dict[str, Any], project_id: str | None) -> dict[str, Any]:
    """Bind reconciliation identity to canonical content and project scope."""
    canonical = dict(script)
    content = {key: value for key, value in canonical.items() if key != "name"}
    digest = hashlib.sha256(
        json.dumps(
            {"project_id": project_id, "script": content},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:16]
    base = str(canonical.get("name") or "apex-script")
    canonical["name"] = f"{base[: 255 - len(digest) - 1]}-{digest}"
    return canonical


def _split_vusers(total: int, group_count: int) -> list[int]:
    """Even split without ever exceeding the caller's requested total."""
    if group_count < 1:
        raise ValueError("APEX Load requires at least one script group")
    if total < group_count:
        raise ValueError(
            f"vusers ({total}) must be >= script group count ({group_count}); "
            "refusing to amplify the requested load"
        )
    base, remainder = divmod(total, group_count)
    return [base + (1 if index < remainder else 0) for index in range(group_count)]


def _goal_config_from_slas(slas: dict[str, float]) -> dict[str, float]:
    """tps_avg/p95_ms/error_rate -> APEX Load goal_config; other keys ignored."""
    goal: dict[str, float] = {}
    if "tps_avg" in slas:
        goal["target_average_tps"] = float(slas["tps_avg"])
    if "p95_ms" in slas:
        goal["p95_latency_ms"] = float(slas["p95_ms"])
    if "error_rate" in slas:
        goal["max_error_pct"] = float(slas["error_rate"]) * 100.0
    return goal


def _revalidate_spec(spec: LoadTestSpec) -> LoadTestSpec:
    """Defeat model_copy/mutation bypasses before load-bearing side effects."""

    if not isinstance(spec, LoadTestSpec):
        raise ValueError("apex_load requires a valid LoadTestSpec")
    validated: LoadTestSpec | None = None
    try:
        payload = spec.model_dump(mode="python", round_trip=True, warnings="error")
        validated = LoadTestSpec.model_validate(payload)
        require_bounded_credential(
            validated.idempotency_key,
            label="apex_load idempotency key",
            max_bytes=256,
            header_token=True,
        )
    except Exception:  # noqa: BLE001 - a corrupt model must fail before provider I/O
        validated = None
    if validated is None:
        raise ValueError("apex_load load test specification failed structural validation")
    return validated


# ── remote -> domain helpers ──────────────────────────────────────────────────


def _provider_number(
    value: Any,
    context: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1_000_000_000_000.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"apex load {context} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise RuntimeError(
            f"apex load {context} must be finite and between {minimum:g} and {maximum:g}"
        )
    return parsed


def _provider_number_field(
    data: dict[str, Any],
    field: str,
    context: str,
    *,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 1_000_000_000_000.0,
) -> float:
    if field not in data:
        return default
    return _provider_number(
        data[field],
        f"{context} field {field!r}",
        minimum=minimum,
        maximum=maximum,
    )


def _provider_integer(
    value: Any,
    context: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_PROVIDER_COUNT,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RuntimeError(
            f"apex load {context} must be an integer between {minimum} and {maximum}"
        )
    return value


def _provider_identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _is_safe_named_ref(value):
        raise RuntimeError(
            f"apex load {context} must be a safe non-empty identifier of at most 255 characters"
        )
    return value


def _provider_status(data: dict[str, Any], context: str) -> str:
    raw = data.get("status")
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > _MAX_PROVIDER_STATUS_CHARS
        or not re.fullmatch(r"[A-Za-z0-9_-]+", raw)
    ):
        raise RuntimeError(
            f"apex load {context} field 'status' must be a safe non-empty string of at most "
            f"{_MAX_PROVIDER_STATUS_CHARS} characters"
        )
    if contains_credential_material(raw):
        raise RuntimeError(f"apex load {context} field 'status' contains unsafe material")
    return raw.upper()


def _provider_boolean(data: dict[str, Any], field: str, context: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise RuntimeError(f"apex load {context} field {field!r} must be a boolean")
    return value


def _provider_list_field(
    data: dict[str, Any],
    field: str,
    context: str,
    *,
    max_items: int,
    required: bool = True,
) -> list[Any]:
    if field not in data and not required:
        return []
    value = data.get(field)
    if not isinstance(value, list):
        raise RuntimeError(f"apex load {context} field {field!r} must be a list")
    if len(value) > max_items:
        raise RuntimeError(
            f"apex load {context} field {field!r} exceeds the {max_items}-item limit"
        )
    return value


def _provider_total(data: dict[str, Any], actual_count: int, context: str) -> int | None:
    total: int | None = None
    for field in ("count", "total"):
        if field not in data:
            continue
        parsed = _provider_integer(data[field], f"{context} field {field!r}")
        if parsed < actual_count:
            raise RuntimeError(
                f"apex load {context} field {field!r} cannot be smaller than its result list"
            )
        if field == "total":
            total = parsed
    return total


def _live_stats_from(test: dict[str, Any]) -> LiveStats | None:
    metrics = test.get("live_metrics")
    if metrics is None:
        return None
    if not isinstance(metrics, dict):
        raise RuntimeError("apex load test field 'live_metrics' must be an object")
    return LiveStats(
        vusers=_provider_number_field(
            metrics,
            "active_vusers",
            "live_metrics",
            maximum=1_000_000_000,
        ),
        tps=_provider_number_field(metrics, "tps", "live_metrics"),
        error_rate=_provider_number_field(
            metrics,
            "error_pct",
            "live_metrics",
            maximum=100,
        )
        / 100.0,
        p95_ms=_provider_number_field(metrics, "p95_ms", "live_metrics"),
    )


def _progress_pct(test: dict[str, Any], phase: EngineRunPhase) -> float:
    if phase in TERMINAL_ENGINE_PHASES:
        return 100.0
    if phase in (EngineRunPhase.PROVISIONING, EngineRunPhase.READY):
        return 0.0
    duration_s: float | None = None
    if "duration_ns" in test:
        raw_ns = _provider_number(
            test["duration_ns"],
            "test field 'duration_ns'",
            maximum=_MAX_PROVIDER_DURATION_S * 1e9,
        )
        if raw_ns > 0:
            duration_s = raw_ns / 1e9
    if duration_s is None:
        duration_s = parse_go_duration_s(test.get("duration"))
    started_raw = test.get("started_at")
    if not duration_s or duration_s <= 0 or started_raw is None:
        return 0.0
    if (
        not isinstance(started_raw, str)
        or not started_raw
        or len(started_raw) > 128
        or "\x00" in started_raw
    ):
        raise RuntimeError("apex load test field 'started_at' must be a bounded timestamp string")
    started: datetime | None = None
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        pass
    if started is None:
        raise RuntimeError(
            "apex load test field 'started_at' must be an ISO-8601 timestamp"
        ) from None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return round(max(0.0, min(99.0, elapsed / duration_s * 100.0)), 1)


def _kpis_from_report(report: dict[str, Any]) -> dict[str, float]:
    raw_overview = report.get("overview", {})
    if not isinstance(raw_overview, dict):
        raise RuntimeError("apex load archive report field 'overview' must be an object")
    overview = raw_overview
    timeline = _provider_list_field(
        report,
        "summary_timeline",
        "archive report",
        max_items=_MAX_PROVIDER_TIMELINE_POINTS,
        required=False,
    )
    tps_points: list[float] = []
    p95_points: list[float] = []
    for index, point in enumerate(timeline):
        if not isinstance(point, dict):
            raise RuntimeError(
                f"apex load archive report field 'summary_timeline'[{index}] must be an object"
            )
        if "tps" in point:
            tps_points.append(
                _provider_number(point["tps"], f"summary_timeline[{index}] field 'tps'")
            )
        if "p95_ms" in point:
            p95_points.append(
                _provider_number(point["p95_ms"], f"summary_timeline[{index}] field 'p95_ms'")
            )

    tps_avg = (
        sum(tps_points) / len(tps_points)
        if tps_points
        else _provider_number_field(overview, "peak_tps", "archive overview")
    )
    if p95_points:
        p95_ms = sum(p95_points) / len(p95_points)
    else:  # fall back to a transaction-weighted average of per-action p95s
        p95_ms = 0.0
        by_action = report.get("by_action", {})
        if not isinstance(by_action, dict):
            raise RuntimeError("apex load archive report field 'by_action' must be an object")
        if len(by_action) > _MAX_PROVIDER_ACTIONS:
            raise RuntimeError(
                "apex load archive report field 'by_action' exceeds the 10000-action limit"
            )
        if by_action:
            weighted = 0.0
            total_tx = 0
            for stats in by_action.values():
                if not isinstance(stats, dict):
                    raise RuntimeError("apex load archive report action entry must be an object")
                tx = _provider_integer(
                    stats.get("transactions", 0),
                    "archive action field 'transactions'",
                    maximum=1_000_000_000_000,
                )
                action_p95 = _provider_number_field(
                    stats,
                    "p95_ms",
                    "archive action",
                )
                weighted += action_p95 * tx
                total_tx += tx
            if total_tx > 0:
                p95_ms = weighted / total_tx
    return {
        "tps_avg": round(tps_avg, 1),
        "p95_ms": round(p95_ms, 1),
        "error_rate": _provider_number_field(
            overview,
            "error_pct",
            "archive overview",
            maximum=100,
        )
        / 100.0,
        "vusers_peak": _provider_number_field(
            overview,
            "peak_active_vusers",
            "archive overview",
            maximum=1_000_000_000,
        ),
    }


def _kpis_from_live(test: dict[str, Any]) -> dict[str, float]:
    metrics = test.get("live_metrics")
    if metrics is None:
        return {}
    if not isinstance(metrics, dict):
        raise RuntimeError("apex load test field 'live_metrics' must be an object")
    vusers_peak = _provider_number_field(
        test,
        "total_vusers",
        "test",
        default=_provider_number_field(
            metrics,
            "active_vusers",
            "live_metrics",
            maximum=1_000_000_000,
        ),
        maximum=1_000_000_000,
    )
    return {
        "tps_avg": _provider_number_field(metrics, "tps", "live_metrics"),
        "p95_ms": _provider_number_field(metrics, "p95_ms", "live_metrics"),
        "error_rate": _provider_number_field(
            metrics,
            "error_pct",
            "live_metrics",
            maximum=100,
        )
        / 100.0,
        "vusers_peak": vusers_peak,
    }


def _error_text(response: httpx.Response) -> str:
    try:
        data = parse_json_response(response, context="apex load error response")
    except RuntimeError:
        return bounded_diagnostic(response.text, max_chars=300)
    if isinstance(data, dict) and data.get("error"):
        return bounded_diagnostic(data["error"])
    return bounded_diagnostic(response.text, max_chars=300)


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    data: Any = None
    invalid_json = False
    try:
        data = parse_json_response(response, context=f"apex load {context} response")
    except RuntimeError:
        invalid_json = True
    if invalid_json:
        raise RuntimeError(f"apex load {context} returned invalid JSON")
    if not isinstance(data, dict):
        raise RuntimeError(f"apex load {context} returned non-object JSON")
    return data


def _provider_messages(data: dict[str, Any], field: str, context: str) -> list[str]:
    """Validate provider message arrays before materializing domain output lists."""

    raw = data.get(field)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeError(f"apex load {context} response field {field!r} must be a list")
    if len(raw) > _MAX_PROVIDER_MESSAGES:
        raise RuntimeError(
            f"apex load {context} response field {field!r} exceeds the "
            f"{_MAX_PROVIDER_MESSAGES}-message limit"
        )
    messages: list[str] = []
    for index, message in enumerate(raw):
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > _MAX_PROVIDER_MESSAGE_CHARS
            or "\x00" in message
        ):
            raise RuntimeError(
                f"apex load {context} response field {field!r}[{index}] must be a "
                f"non-empty string of at most {_MAX_PROVIDER_MESSAGE_CHARS} characters"
            )
        # Provider issue/breach strings become checkpointed domain output. Treat
        # them as untrusted diagnostics before they cross that durable/public
        # boundary; recognized credential material is redacted, not reflected.
        messages.append(bounded_diagnostic(message, max_chars=_MAX_PROVIDER_MESSAGE_CHARS))
    return messages


def _extend_validation_issues(issues: list[str], incoming: list[str]) -> None:
    if len(incoming) > _MAX_PROVIDER_MESSAGES - len(issues):
        raise RuntimeError("apex load validation returned more than 128 aggregate issue messages")
    issues.extend(incoming)


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.EXECUTION_ENGINE, PROVIDER)
class ApexLoadExecutionEngine:
    """ExecutionEnginePort against APEX Load. Stateless: every method is driven
    from EngineHandle.external_run_id plus remote queries (contract req. 6)."""

    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        raw_base_url = options.get("base_url")
        if (
            not isinstance(raw_base_url, str)
            or not raw_base_url
            or raw_base_url != raw_base_url.strip()
        ):
            raise ValueError(
                f"apex_load connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://apexload.internal:8080")'
            )
        base_url = raw_base_url.rstrip("/")
        if secret is None:
            raise ValueError(
                f"apex_load connection {conn.id!r} requires a service-account API key; "
                'set secret_ref on the connection (e.g. "env:APEX_INTEGRATION_APEXLOAD_API_KEY")'
            )
        api_key = require_bounded_credential(
            secret.value,
            label="apex_load API key",
            header_token=True,
        )
        self._conn = conn
        self._base_url = base_url
        self._allow_private_hosts = private_hosts_allowed(options)
        raw_project_id = options.get("project_id")
        if raw_project_id in (None, ""):
            self._project_id = None
        elif isinstance(raw_project_id, str) and _is_safe_named_ref(raw_project_id):
            self._project_id = raw_project_id
        else:
            raise ValueError("apex_load project_id must be a safe string of at most 255 characters")
        raw_max_report_bytes = options.get("max_report_bytes", _DEFAULT_MAX_REPORT_BYTES)
        if isinstance(raw_max_report_bytes, bool) or not isinstance(raw_max_report_bytes, int):
            raise ValueError("apex_load max_report_bytes must be an integer")
        self._max_report_bytes = raw_max_report_bytes
        if not 1 <= self._max_report_bytes <= _HARD_MAX_REPORT_BYTES:
            raise ValueError(
                f"apex_load max_report_bytes must be between 1 and {_HARD_MAX_REPORT_BYTES}"
            )
        self._headers = {
            "X-APEXLoad-API-Key": api_key,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._breaker = CircuitBreaker(f"apex_load:{conn.id}")

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Lazy client, rebuilt when the running event loop changes."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = safe_async_http_client(
                base_url=self._base_url,
                headers=self._headers,
                timeout=_TIMEOUT_S,
                allow_private_hosts=self._allow_private_hosts,
            )
            self._http_loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._http_loop = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        request_timeout = timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT
        breaker = (
            self._breaker if method.upper() == "GET" and path.startswith("/api/v1/tests/") else None
        )
        # Explicit per-attempt timeouts also become the absolute request budget;
        # the resilience layer carries it through retries and body consumption.
        retry = retry_policy(total_timeout_s=timeout_s) if timeout_s is not None else None
        response: httpx.Response | None = None
        request_failure: RuntimeError | None = None
        try:
            stream = await resilient_stream_request(
                self._client(),
                method,
                path,
                json=json,
                params=params,
                headers=headers,
                timeout=request_timeout,
                retry=retry,
                breaker=breaker,
            )
            response = await read_bounded_response(
                stream,
                max_bytes=_MAX_JSON_RESPONSE_BYTES,
            )
        except CircuitOpenError:
            request_failure = RuntimeError(f"apex load request circuit is open for {method} {path}")
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            request_failure = _TransportRequestError(
                bounded_diagnostic(
                    f"apex load request {method} {path} failed before a response arrived: {detail}"
                )
            )
        if request_failure is not None:
            raise request_failure
        if response is None:  # pragma: no cover - resilience contract invariant
            raise RuntimeError("apex load request completed without a response")
        if response.status_code == 404 and not_found is not None:
            raise EngineProviderRunNotFoundError(not_found)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"apex load rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's "
                "X-APEXLoad-API-Key secret and the service account's scopes"
            )
        if response.status_code in (400, 422):
            raise ValueError(f"apex load rejected the request: {_error_text(response)}")
        if response.status_code == 409:
            raise _RemoteConflictError(
                f"apex load refused {method} {path} (HTTP 409): {_error_text(response)}"
            )
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise _AmbiguousResponseError(
                f"apex load {method} {path} returned an ambiguous transient "
                f"HTTP {response.status_code}: {_error_text(response)}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"apex load {method} {path} failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        return response

    async def _stream_download(self, path: str, *, not_found: str) -> httpx.Response:
        """Open a bounded-consumer streaming response; caller must ``aclose`` it."""
        response: httpx.Response | None = None
        request_failure: RuntimeError | None = None
        try:
            response = await resilient_stream_request(
                self._client(),
                "GET",
                path,
                timeout=_DOWNLOAD_TIMEOUT_S,
                retry=retry_policy(total_timeout_s=_DOWNLOAD_TOTAL_TIMEOUT_S),
                breaker=self._breaker,
            )
        except CircuitOpenError:
            request_failure = RuntimeError(f"apex load request circuit is open for GET {path}")
        except httpx.HTTPError as exc:
            detail = bounded_diagnostic(exc)
            request_failure = RuntimeError(
                bounded_diagnostic(
                    f"apex load request GET {path} failed before a response arrived: {detail}"
                )
            )
        if request_failure is not None:
            raise request_failure
        if response is None:  # pragma: no cover - resilience contract invariant
            raise RuntimeError("apex load download completed without a response")
        if response.status_code < 400:
            unsupported_encoding = False
            try:
                require_identity_content_encoding(response)
            except Exception:
                unsupported_encoding = True
            if unsupported_encoding:
                await close_response_definitively(response)
                raise RuntimeError("apex load download used an unsupported content encoding")
            return response
        preview = await read_stream_error_preview(response)
        preview_response = httpx.Response(response.status_code, content=preview)
        try:
            if response.status_code == 404:
                raise EngineProviderRunNotFoundError(not_found)
            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"apex load rejected credentials for GET {path} (HTTP {response.status_code})"
                )
            raise RuntimeError(
                f"apex load GET {path} failed with HTTP {response.status_code}: "
                f"{_error_text(preview_response)}"
            )
        finally:
            await close_response_definitively(response)

    def _run_id(self, handle: EngineHandle) -> str:
        if not handle.external_run_id:
            raise ValueError("apex_load handle has no external_run_id; call provision() first")
        run_id: str | None = None
        invalid_run_id = False
        try:
            run_id = _provider_identifier(handle.external_run_id, "handle external_run_id")
        except RuntimeError:
            invalid_run_id = True
        if invalid_run_id:
            raise ValueError("apex_load handle has an invalid external_run_id")
        if run_id is None:  # pragma: no cover - identifier contract invariant
            raise ValueError("apex_load handle has an invalid external_run_id")
        return run_id

    def _require_test_identity(
        self,
        test: dict[str, Any],
        *,
        name: str,
        context: str,
    ) -> None:
        """Prove a returned test belongs to this idempotency/project scope."""

        if test.get("name") != name:
            raise RuntimeError(f"apex load {context} returned an unexpected test name")
        if self._project_id is not None and test.get("project_id") != self._project_id:
            raise RuntimeError(f"apex load {context} returned a test from an unexpected project")

    def _require_script_identity(
        self,
        script: dict[str, Any],
        *,
        name: str,
        context: str,
    ) -> None:
        """Prove an uploaded/reconciled script is the content-bound resource."""

        if script.get("name") != name:
            raise RuntimeError(f"apex load {context} returned an unexpected script name")
        if "project_id" in script and script.get("project_id") != self._project_id:
            raise RuntimeError(f"apex load {context} returned a script from an unexpected project")

    def _require_test_resource_identity(
        self,
        test: dict[str, Any],
        *,
        run_id: str,
        context: str,
        expected_name: str | None = None,
    ) -> None:
        """Prove a direct test-resource response matches its request target."""

        returned_id = _provider_identifier(test.get("id"), f"{context} field 'id'")
        if returned_id != run_id:
            raise RuntimeError(f"apex load {context} returned an unexpected test id")
        if expected_name is not None and test.get("name") != expected_name:
            raise RuntimeError(f"apex load {context} returned an unexpected test name")
        if self._project_id is not None and test.get("project_id") != self._project_id:
            raise RuntimeError(f"apex load {context} returned a test from an unexpected project")

    async def _require_owned_test(
        self,
        handle: EngineHandle,
        *,
        run_id: str,
        context: str,
    ) -> None:
        """Prove a durable handle still targets its content-bound test."""

        response = await self._request(
            "GET",
            f"/api/v1/tests/{run_id}",
            not_found=f"apex load test {run_id!r} not found",
        )
        test = _json_object(response, f"GET /api/v1/tests/{run_id}")
        self._require_test_resource_identity(
            test,
            run_id=run_id,
            context=context,
            expected_name=test_name_for(handle.idempotency_key),
        )

    @staticmethod
    def _require_mutation_acknowledgement(
        response: httpx.Response,
        *,
        run_id: str,
        context: str,
    ) -> None:
        acknowledgement = _json_object(response, context)
        returned_id = _provider_identifier(
            acknowledgement.get("test_id"),
            f"{context} field 'test_id'",
        )
        if returned_id != run_id:
            raise RuntimeError(f"apex load {context} returned an unexpected test id")

    # ── port surface ──────────────────────────────────────────────────────────

    async def validate(self, spec: LoadTestSpec) -> ValidationReport:
        """Local structural checks, then APEX Load's server-side script validation
        (POST /api/v1/scripts/validate for drafts; POST /scripts/{id}/validate
        for named refs)."""
        try:
            spec = _revalidate_spec(spec)
        except ValueError:
            return ValidationReport(
                ok=False,
                issues=["load test specification failed structural validation"],
            )
        issues: list[str] = []
        if spec.vusers < 1:
            issues.append("vusers must be >= 1")
        if spec.duration_s <= 0:
            issues.append("duration_s must be > 0")
        if spec.ramp_s < 0:
            issues.append("ramp_s must be >= 0")
        if spec.script_refs and len(spec.script_refs) > spec.vusers:
            issues.append(
                f"vusers ({spec.vusers}) must be >= script_refs count "
                f"({len(spec.script_refs)}); one group requires at least one vuser"
            )

        inline_scripts: list[dict[str, Any]] = []
        named_refs: list[tuple[int, str]] = []
        for index, ref in enumerate(spec.script_refs):
            if _is_inline_ref(ref):
                try:
                    parsed = parse_json_bytes(
                        ref,
                        context=f"script_refs[{index}]",
                    )
                except RuntimeError as exc:
                    detail = bounded_diagnostic(exc)
                    issues.append(
                        bounded_diagnostic(
                            f"script_refs[{index}] is not valid JSON: {detail}",
                            max_chars=2_048,
                        )
                    )
                    continue
                if not isinstance(parsed, dict):
                    issues.append(
                        f"script_refs[{index}] must be a JSON object (APEX Load DSL script)"
                    )
                    continue
                inline_scripts.append(_prepare_inline_script(parsed, spec, index))
            else:
                named_refs.append((index, ref))
        if not spec.script_refs and not spec.target_environment:
            issues.append(
                "the generated default workload needs spec.target_environment "
                "(or provide script_refs)"
            )
        if issues:
            return ValidationReport(ok=False, issues=issues)

        drafts = inline_scripts if spec.script_refs else [_default_script(spec)]
        for script in drafts:
            payload: dict[str, Any] = {"script": script}
            if self._project_id:
                payload["project_id"] = self._project_id
            response = await self._request("POST", "/api/v1/scripts/validate", json=payload)
            data = _json_object(response, "script validation")
            if not _provider_boolean(data, "valid", "script validation"):
                _extend_validation_issues(
                    issues,
                    _provider_messages(data, "issues", "script validation"),
                )
        for index, ref in named_refs:
            if not _is_safe_named_ref(ref):
                issues.append(f"script_refs[{index}] is not a safe APEX Load script id")
                continue
            try:
                response = await self._request(
                    "POST",
                    f"/api/v1/scripts/{ref}/validate",
                    not_found=f"script {ref!r} not found in apex load",
                )
            except KeyError as exc:
                issues.append(str(exc.args[0]))
                continue
            data = _json_object(response, "script validation")
            if not _provider_boolean(data, "valid", "script validation"):
                _extend_validation_issues(
                    issues,
                    _provider_messages(data, "issues", "script validation"),
                )
        return ValidationReport(ok=not issues, issues=issues)

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        """Get-or-create by spec.idempotency_key via the remote test NAME
        "apex-orch:<key>".

        The guarded lookup closes the local check-then-create race.  The
        provider's Idempotency-Key reservation closes it across callers that do
        not share this process; a 409 is followed by a bounded lookup for the
        winning test because the reservation can become visible just before
        the test record does.
        """
        spec = _revalidate_spec(spec)
        name = test_name_for(spec.idempotency_key)
        guard_key = ":".join(
            (PROVIDER, self._base_url, self._project_id or "", spec.idempotency_key)
        )
        async with remote_create_guard(guard_key):
            test = await self._find_test_by_name(name)
            if test is None:
                conflict = False
                try:
                    test = await self._create_test(name, spec)
                except _RemoteConflictError:
                    conflict = True
                if conflict:
                    test = await self._find_test_after_conflict(name)
                    if test is None:
                        raise RuntimeError(
                            "apex load reserved the idempotency key but the winning test "
                            f"{name!r} did not become visible"
                        )
                    external_run_id = _provider_identifier(
                        test.get("id"), "conflict-adopted test response field 'id'"
                    )
                    logger.info(
                        "apex_load.test_conflict_adopted",
                        test_id=external_run_id,
                        test_name=name,
                    )
                else:
                    if test is None:  # pragma: no cover - create contract invariant
                        raise RuntimeError("apex load test creation returned no test")
                    external_run_id = _provider_identifier(
                        test.get("id"), "created test response field 'id'"
                    )
                    logger.info("apex_load.test_created", test_id=external_run_id, test_name=name)
            else:
                external_run_id = _provider_identifier(
                    test.get("id"), "reused test response field 'id'"
                )
                logger.info("apex_load.test_reused", test_id=external_run_id, test_name=name)
        self._require_test_identity(test, name=name, context="provisioning")
        extras = {"test_name": name}
        if self._project_id:
            extras["project_id"] = self._project_id
        external_run_id = _provider_identifier(test.get("id"), "test response field 'id'")
        return EngineHandle(
            engine=PROVIDER,
            connection_id=self._conn.id,
            external_run_id=external_run_id,
            idempotency_key=spec.idempotency_key,
            extras=extras,
        )

    async def start(self, handle: EngineHandle) -> None:
        """POST /tests/{id}/start; tolerates the already-started rejection."""
        run_id = self._run_id(handle)
        await self._require_owned_test(
            handle,
            run_id=run_id,
            context="start preflight response",
        )
        start_error: ValueError | None = None
        response: httpx.Response | None = None
        try:
            response = await self._request(
                "POST",
                f"/api/v1/tests/{run_id}/start",
                not_found=f"apex load test {run_id!r} not found",
            )
        except ValueError as exc:
            detail = bounded_diagnostic(exc)
            if _ALREADY_STARTED_MARKER in detail:
                logger.info("apex_load.start_noop", external_run_id=run_id, detail=detail)
                return
            start_error = ValueError(detail)
        if start_error is not None:
            raise start_error
        if response is None:  # pragma: no cover - request contract invariant
            raise RuntimeError("apex load start returned no acknowledgement")
        self._require_mutation_acknowledgement(
            response,
            run_id=run_id,
            context="start acknowledgement",
        )

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        """GET /tests/{id}: one cheap read per poll cycle."""
        run_id = self._run_id(handle)
        response = await self._request(
            "GET",
            f"/api/v1/tests/{run_id}",
            not_found=f"apex load test {run_id!r} not found",
        )
        test = _json_object(response, f"GET /api/v1/tests/{run_id}")
        expected_name = handle.extras.get("test_name")
        self._require_test_resource_identity(
            test,
            run_id=run_id,
            context="test status response",
            expected_name=expected_name if type(expected_name) is str else None,
        )
        raw_status = _provider_status(test, "test response")
        phase = _PHASE_BY_STATUS.get(raw_status)
        message = f"APEX Load test {run_id} is {raw_status or 'UNKNOWN'}"
        if phase is None:
            phase = EngineRunPhase.RUNNING
            message += " (unmapped status; treating as running)"
        if test.get("error"):
            message = bounded_diagnostic(f"{message}: {bounded_diagnostic(test['error'])}")
        return EngineRunStatus(
            phase=phase,
            progress_pct=_progress_pct(test, phase),
            live_stats=_live_stats_from(test),
            message=message,
        )

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        """POST /tests/{id}/abort; 400 (already terminal) and 404 (gone) are
        tolerated so abort stays idempotent."""
        run_id = self._run_id(handle)
        missing = False
        try:
            await self._require_owned_test(
                handle,
                run_id=run_id,
                context="abort preflight response",
            )
        except KeyError:
            missing = True
        if missing:
            logger.info(
                "apex_load.abort_noop",
                external_run_id=run_id,
                reason_present=bool(reason),
                reason_length=min(len(reason), 1_024),
                detail="test not found",
            )
            return
        response: httpx.Response | None = None
        try:
            response = await self._request(
                "POST",
                f"/api/v1/tests/{run_id}/abort",
                not_found=f"apex load test {run_id!r} not found",
            )
        except (KeyError, ValueError) as exc:
            detail = bounded_diagnostic(exc.args[0] if exc.args else exc)
            logger.info(
                "apex_load.abort_noop",
                external_run_id=run_id,
                reason_present=bool(reason),
                reason_length=min(len(reason), 1_024),
                detail=detail,
            )
            return
        if response is None:  # pragma: no cover - request contract invariant
            raise RuntimeError("apex load abort returned no acknowledgement")
        self._require_mutation_acknowledgement(
            response,
            run_id=run_id,
            context="abort acknowledgement",
        )
        logger.info(
            "apex_load.abort",
            external_run_id=run_id,
            reason_present=bool(reason),
            reason_length=min(len(reason), 1_024),
        )

    async def collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        """Collect under one aggregate deadline including object-store writes."""

        artifacts: list[dict[str, Any]] | None = None
        timed_out = False
        try:
            async with asyncio.timeout(_COLLECTION_TOTAL_TIMEOUT_S):
                artifacts = await self._collect_artifacts(handle, store)
        except TimeoutError:
            timed_out = True
        if timed_out:
            raise RuntimeError("APEX Load artifact collection exceeded its total deadline")
        if artifacts is None:  # pragma: no cover - collection contract invariant
            raise RuntimeError("APEX Load artifact collection returned no result")
        return artifacts

    async def _collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        """Stream the archive report JSON into the artifact store. Runs aborted
        before start never archive: a 404 yields zero artifacts, not an error."""
        run_id = self._run_id(handle)
        await self._require_owned_test(
            handle,
            run_id=run_id,
            context="artifact collection preflight response",
        )
        try:
            response = await self._stream_download(
                f"/api/v1/tests/{run_id}/archive/report",
                not_found=f"apex load archive for test {run_id!r} not found",
            )
        except KeyError:
            logger.warning("apex_load.archive_missing", external_run_id=run_id)
            return []
        key = engine_artifact_key(handle.idempotency_key, "apex-load-report.json")
        try:
            stored = await store.put_stream(
                key,
                response.aiter_raw(),
                content_type="application/json",
                max_bytes=self._max_report_bytes,
            )
        finally:
            await close_response_definitively(response)
        ref = ArtifactRef(
            kind="engine_report",
            name="apex-load-report.json",
            uri=stored.uri,
            key=stored.key,
            media_type="application/json",
            summary=f"APEX Load archive report for test {run_id}",
        )
        return [ref.model_dump(mode="json")]

    async def fetch_summary(self, handle: EngineHandle) -> TestResultSummary:
        """passed/breaches from GET /tests/{id}/sla-status; KPIs from the archive
        report, with a live-metrics fallback when the archive is missing."""
        run_id = self._run_id(handle)
        sla_response = await self._request(
            "GET",
            f"/api/v1/tests/{run_id}/sla-status",
            not_found=f"apex load test {run_id!r} not found",
        )
        sla = _json_object(sla_response, f"GET /api/v1/tests/{run_id}/sla-status")
        returned_sla_test_id = _provider_identifier(
            sla.get("test_id"),
            "SLA status response field 'test_id'",
        )
        if returned_sla_test_id != run_id:
            raise RuntimeError("apex load SLA status response returned an unexpected test id")
        status = _provider_status(sla, "SLA status response")
        breached = _provider_boolean(sla, "sla_breached", "SLA status response")
        breaches = _provider_messages(sla, "details", "SLA status")

        kpis: dict[str, float] = {}
        notes = f"APEX Load test {run_id} status {status or 'UNKNOWN'}"
        try:
            report_response = await self._stream_download(
                f"/api/v1/tests/{run_id}/archive/report",
                not_found=f"apex load archive for test {run_id!r} not found",
            )
        except KeyError:
            kpis = await self._live_kpis(run_id)
            notes += "; archive report unavailable, KPIs from live metrics"
        else:
            try:
                report = await _read_bounded_json_object(
                    report_response,
                    f"GET /api/v1/tests/{run_id}/archive/report",
                    max_bytes=min(self._max_report_bytes, _MAX_SUMMARY_REPORT_BYTES),
                )
            except ValueError as exc:
                logger.warning(
                    "apex_load.archive_summary_unavailable",
                    external_run_id=run_id,
                    detail=bounded_diagnostic(exc),
                )
                kpis = await self._live_kpis(run_id)
                notes += "; archive report invalid or oversized, KPIs from live metrics"
            else:
                if "manifest" not in report:
                    # Older APEX Load archives predate the manifest field. The
                    # archive endpoint is still test-scoped, but prove that the
                    # requested resource is this handle's content-bound test
                    # before trusting KPI data that cannot identify itself.
                    await self._require_owned_test(
                        handle,
                        run_id=run_id,
                        context="legacy archive identity response",
                    )
                    notes += "; legacy archive identity verified from test resource"
                else:
                    manifest = report.get("manifest")
                    if not isinstance(manifest, dict):
                        raise RuntimeError("apex load archive report has no valid manifest")
                    archive_test_id = _provider_identifier(
                        manifest.get("test_id"),
                        "archive report manifest field 'test_id'",
                    )
                    if archive_test_id != run_id:
                        raise RuntimeError(
                            "apex load archive report returned an unexpected test id"
                        )
                kpis = _kpis_from_report(report)
                notes += "; KPIs from archive report"

        passed = status == "COMPLETE" and not breached
        return TestResultSummary(
            engine=PROVIDER,
            passed=passed,
            kpis=kpis,
            sla_breaches=breaches,
            notes=notes,
        )

    async def teardown(self, handle: EngineHandle) -> None:
        """Documented no-op: APEX Load tests are immutable archives; deleting
        them would destroy the result of record. Never raises (contract req. 4)."""
        logger.debug(
            "apex_load.teardown_noop",
            external_run_id_present=bool(handle.external_run_id),
        )

    async def _live_kpis(self, run_id: str) -> dict[str, float]:
        """Best-effort KPI fallback from the in-memory test record (no archive)."""
        try:
            response = await self._request(
                "GET",
                f"/api/v1/tests/{run_id}",
                not_found=f"apex load test {run_id!r} not found",
            )
        except KeyError:
            return {}
        test = _json_object(response, f"GET /api/v1/tests/{run_id}")
        self._require_test_resource_identity(
            test,
            run_id=run_id,
            context="live metrics response",
        )
        return _kpis_from_live(test)

    # ── provisioning internals ────────────────────────────────────────────────

    async def _find_test_by_name(self, name: str) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        for offset in range(0, _MAX_TEST_RECONCILIATION_ROWS, _TEST_LIST_PAGE_SIZE):
            params: dict[str, Any] = {
                "summary": True,
                "limit": _TEST_LIST_PAGE_SIZE,
                "offset": offset,
            }
            if self._project_id:
                params["project_id"] = self._project_id
            response = await self._request("GET", "/api/v1/tests", params=params)
            data = _json_object(response, "GET /api/v1/tests")
            tests = _provider_list_field(
                data,
                "tests",
                "GET /api/v1/tests response",
                max_items=_TEST_LIST_PAGE_SIZE,
            )
            total = _provider_total(data, len(tests), "GET /api/v1/tests response")
            for test in tests:
                if not isinstance(test, dict):
                    raise RuntimeError("GET /api/v1/tests response contains a non-object test")
                if test.get("name") == name:
                    self._require_test_identity(
                        test,
                        name=name,
                        context="test reconciliation",
                    )
                    matches.append(test)
            if not tests or len(tests) < _TEST_LIST_PAGE_SIZE:
                break
            if total is not None and offset + len(tests) >= total:
                break
        else:
            raise RuntimeError(
                "apex load test reconciliation exceeded the 5000-row provider scan budget"
            )
        if len(matches) > 1:
            raise RuntimeError("apex load idempotency name is attached to multiple tests")
        return matches[0] if matches else None

    async def _find_test_after_conflict(self, name: str) -> dict[str, Any] | None:
        for attempt in range(_CONFLICT_RECHECK_ATTEMPTS):
            test = await self._find_test_by_name(name)
            if test is not None:
                return test
            if attempt + 1 < _CONFLICT_RECHECK_ATTEMPTS:
                await asyncio.sleep(_CONFLICT_RECHECK_DELAY_S * (attempt + 1))
        return None

    async def _find_script_by_name(self, name: str) -> dict[str, Any] | None:
        params = {"project_id": self._project_id} if self._project_id else None
        response = await self._request("GET", "/api/v1/scripts", params=params)
        data = _json_object(response, "GET /api/v1/scripts")
        scripts = _provider_list_field(
            data,
            "scripts",
            "GET /api/v1/scripts response",
            max_items=_MAX_SCRIPT_RECONCILIATION_ROWS,
        )
        _provider_total(data, len(scripts), "GET /api/v1/scripts response")
        matches: list[dict[str, Any]] = []
        for script in scripts:
            if not isinstance(script, dict):
                raise RuntimeError("GET /api/v1/scripts response contains a non-object script")
            if script.get("name") == name:
                self._require_script_identity(
                    script,
                    name=name,
                    context="script reconciliation",
                )
                matches.append(script)
        if len(matches) > 1:
            raise RuntimeError("apex load content-bound name is attached to multiple scripts")
        return matches[0] if matches else None

    async def _find_script_after_uncertain_create(self, name: str) -> dict[str, Any] | None:
        for attempt in range(_CONFLICT_RECHECK_ATTEMPTS):
            script = await self._find_script_by_name(name)
            if script is not None:
                return script
            if attempt + 1 < _CONFLICT_RECHECK_ATTEMPTS:
                await asyncio.sleep(_CONFLICT_RECHECK_DELAY_S * (attempt + 1))
        return None

    async def _upload_script(self, script: dict[str, Any], *, idempotency_key: str) -> str:
        script = _content_bound_script(script, self._project_id)
        idempotency_key = require_bounded_credential(
            idempotency_key,
            label="apex_load script idempotency key",
            max_bytes=512,
            header_token=True,
        )
        payload: dict[str, Any] = {"script": script}
        if self._project_id:
            payload["project_id"] = self._project_id
        response: httpx.Response | None = None
        stored: dict[str, Any] | None = None
        uncertain_error: _RemoteConflictError | _AmbiguousRequestError | None = None
        try:
            response = await self._request(
                "POST",
                "/api/v1/scripts",
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
            )
        except (_RemoteConflictError, _AmbiguousRequestError) as exc:
            uncertain_error = exc
        if uncertain_error is not None:
            name = str(script.get("name") or "")
            stored = await self._find_script_after_uncertain_create(name) if name else None
            if stored is None:
                raise uncertain_error
            stored_id = _provider_identifier(
                stored.get("id"), "reconciled script response field 'id'"
            )
            logger.warning(
                "apex_load.script_create_reconciled",
                script_id=stored_id,
                script_name=name,
            )
        else:
            if response is None:  # pragma: no cover - request contract invariant
                raise RuntimeError("apex load script creation returned no response")
            invalid_response = False
            try:
                stored = _json_object(response, "POST /api/v1/scripts")
                self._require_script_identity(
                    stored,
                    name=str(script["name"]),
                    context="script creation response",
                )
            except RuntimeError:
                invalid_response = True
            if invalid_response:
                name = str(script.get("name") or "")
                stored = await self._find_script_after_uncertain_create(name) if name else None
                if stored is None:
                    raise RuntimeError(
                        "apex load POST /api/v1/scripts returned an invalid acknowledgement "
                        "and could not be reconciled"
                    )
        if stored is None:  # pragma: no cover - create/reconciliation contract invariant
            raise RuntimeError("apex load script creation returned no stored script")
        self._require_script_identity(
            stored,
            name=str(script["name"]),
            context="script upload",
        )
        script_id: str | None = None
        invalid_script_id = False
        try:
            script_id = _provider_identifier(stored.get("id"), "script response field 'id'")
        except RuntimeError:
            invalid_script_id = True
        if invalid_script_id:
            name = str(script.get("name") or "")
            reconciled = await self._find_script_after_uncertain_create(name) if name else None
            if reconciled is None:
                raise RuntimeError(
                    "apex load POST /api/v1/scripts returned an invalid script id; cannot "
                    "build the test"
                )
            return _provider_identifier(
                reconciled.get("id"),
                "reconciled script response field 'id'",
            )
        if script_id is None:  # pragma: no cover - identifier contract invariant
            raise RuntimeError("apex load script response returned no script id")
        return script_id

    async def _create_test(self, name: str, spec: LoadTestSpec) -> dict[str, Any]:
        script_ids: list[str] = []
        for index, ref in enumerate(spec.script_refs):
            if _is_inline_ref(ref):
                parsed: Any = None
                invalid_json = False
                try:
                    parsed = parse_json_bytes(
                        ref,
                        context=f"script_refs[{index}]",
                    )
                except RuntimeError:
                    invalid_json = True
                if invalid_json:
                    raise ValueError(
                        f"script_refs[{index}] must be valid JSON (APEX Load DSL script)"
                    )
                if not isinstance(parsed, dict):
                    raise ValueError(
                        f"script_refs[{index}] must be a JSON object (APEX Load DSL script)"
                    )
                script_ids.append(
                    await self._upload_script(
                        _prepare_inline_script(parsed, spec, index),
                        idempotency_key=f"{spec.idempotency_key}:script:{index}",
                    )
                )
            else:
                if not _is_safe_named_ref(ref):
                    raise ValueError(f"script_refs[{index}] is not a safe APEX Load script id")
                script_ids.append(ref)
        if not script_ids:
            if not spec.target_environment:
                raise ValueError(
                    "the generated default workload needs spec.target_environment "
                    "(or provide script_refs)"
                )
            script_ids.append(
                await self._upload_script(
                    _default_script(spec),
                    idempotency_key=f"{spec.idempotency_key}:script:default",
                )
            )

        ramp = format_go_duration(spec.ramp_s)
        groups = [
            {
                "name": f"group-{index + 1}",
                "script_id": script_id,
                "vusers": vusers,
                "ramp_up": ramp,
            }
            for index, (script_id, vusers) in enumerate(
                zip(script_ids, _split_vusers(spec.vusers, len(script_ids)), strict=True)
            )
        ]
        body: dict[str, Any] = {
            "name": name,
            "duration": format_go_duration(spec.duration_s),
            "groups": groups,
        }
        if self._project_id:
            body["project_id"] = self._project_id
        goal = _goal_config_from_slas(spec.slas)
        if goal:
            body["goal_config"] = goal
        idempotency_key = require_bounded_credential(
            spec.idempotency_key,
            label="apex_load test idempotency key",
            max_bytes=256,
            header_token=True,
        )
        response: httpx.Response | None = None
        test: dict[str, Any] | None = None
        uncertain_error: _AmbiguousRequestError | None = None
        try:
            response = await self._request(
                "POST",
                "/api/v1/tests",
                json=body,
                headers={"Idempotency-Key": idempotency_key},
            )
        except _AmbiguousRequestError as exc:
            uncertain_error = exc
        if uncertain_error is not None:
            # The provider may have committed the idempotent create before the
            # response was lost or truncated. Adopt the named resource instead
            # of reporting failure and leaving an untracked remote test.
            test = await self._find_test_after_conflict(name)
            if test is None:
                raise uncertain_error
            test_id = _provider_identifier(test.get("id"), "reconciled test response field 'id'")
            logger.warning(
                "apex_load.test_create_reconciled",
                test_id=test_id,
                test_name=name,
                error=bounded_diagnostic(uncertain_error),
            )
        else:
            if response is None:  # pragma: no cover - request contract invariant
                raise RuntimeError("apex load test creation returned no response")
            invalid_response_error: str | None = None
            try:
                test = _json_object(response, "POST /api/v1/tests")
                self._require_test_identity(
                    test,
                    name=name,
                    context="test creation response",
                )
            except RuntimeError as exc:
                invalid_response_error = bounded_diagnostic(exc)
            if invalid_response_error is not None:
                # A proxy/provider can commit the create and then truncate or
                # replace the successful response body. Reconcile exactly as for a
                # lost transport response before abandoning the remote resource.
                test = await self._find_test_after_conflict(name)
                if test is None:
                    raise RuntimeError(
                        "apex load POST /api/v1/tests returned an invalid acknowledgement "
                        "and could not be reconciled"
                    )
                test_id = _provider_identifier(
                    test.get("id"), "reconciled test response field 'id'"
                )
                logger.warning(
                    "apex_load.test_create_reconciled",
                    test_id=test_id,
                    test_name=name,
                    error=invalid_response_error,
                )

        if test is None:  # pragma: no cover - create/reconciliation contract invariant
            raise RuntimeError("apex load test creation returned no test")
        invalid_test_id = False
        try:
            _provider_identifier(test.get("id"), "test response field 'id'")
        except RuntimeError:
            invalid_test_id = True
        if invalid_test_id:
            # A syntactically valid object without its durable id is equally
            # ambiguous. Adopt the canonical named test rather than checkpointing
            # an un-abortable handle.
            reconciled = await self._find_test_after_conflict(name)
            if reconciled is None:
                raise RuntimeError(
                    "apex load POST /api/v1/tests returned an invalid test id; cannot track "
                    "the test"
                )
            _provider_identifier(reconciled.get("id"), "reconciled test response field 'id'")
            test = reconciled
        return test
