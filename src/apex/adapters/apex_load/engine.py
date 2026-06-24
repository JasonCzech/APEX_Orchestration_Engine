"""APEX Load execution-engine adapter (provider "apex_load", PortKind.EXECUTION_ENGINE).

Wire surface: APEX Load REST API v1 (Project_Stormrunner API_AND_DSL_REFERENCE.md;
the Go HTTP handlers in pkg/api are definitive where the reference is stale).
Connection options: {"base_url": "https://apexload.internal:8080",
"project_id"?: "proj-123"}; the secret is a service-account API key sent as the
X-APEXLoad-API-Key header (the server also accepts "Authorization: Bearer").

Wire-format decisions (documented mappings / deviations):

- Get-or-create (contract req. 1): provision() names the remote test
  "apex-orch:<idempotency_key>" and looks that name up via GET /api/v1/tests
  (project-scoped when project_id is set) BEFORE creating anything, so
  re-provisioning after a crash — even from a fresh process — finds the
  existing run instead of double-creating. The remote test name is the durable
  key registry; nothing is kept client-side.
- spec.script_refs convention: an entry starting with "{" is inline APEX Load
  JSON DSL (uploaded via POST /api/v1/scripts at provision time, wrapper shape
  {"project_id"?, "script": {...}} per pkg/api/handlers.go handleCreateScript);
  any other entry is an EXISTING APEX Load script id used as-is. An empty list
  generates a default single-GET HTTP workload against spec.target_environment.
- spec.vusers splits evenly across one group per script (remainder to the
  earliest groups, minimum 1 vuser per group — so the effective total can
  exceed spec.vusers when spec.vusers < len(script_refs)); ramp_s/duration_s
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
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import (
    LoadTestSpec,
    SecretValue,
    TestResultSummary,
    ValidationReport,
)
from apex.domain.pipeline import ArtifactRef, EngineHandle
from apex.ports.artifact_store import ArtifactStorePort
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineRunPhase,
    EngineRunStatus,
    LiveStats,
)

logger = structlog.get_logger(__name__)

PROVIDER = "apex_load"
TEST_NAME_PREFIX = "apex-orch:"
_TIMEOUT_S = 15.0
_DOWNLOAD_TIMEOUT_S = 60.0

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

# ── Go duration helpers ───────────────────────────────────────────────────────

_GO_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_GO_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")


def parse_go_duration_s(value: Any) -> float | None:
    """ "5m30s" -> 330.0; None for anything that is not a Go duration string."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    matches = list(_GO_DURATION_TOKEN.finditer(text))
    if not matches or "".join(match.group(0) for match in matches) != text:
        return None
    return sum(float(match.group(1)) * _GO_UNIT_S[match.group(2)] for match in matches)


def format_go_duration(seconds: float) -> str:
    """Seconds -> Go duration string ("300s", "2.5s"); ParseDuration-compatible."""
    return f"{seconds:g}s"


# ── spec -> wire helpers ──────────────────────────────────────────────────────


def test_name_for(idempotency_key: str) -> str:
    return f"{TEST_NAME_PREFIX}{idempotency_key}"


def _is_inline_ref(ref: str) -> bool:
    return ref.lstrip().startswith("{")


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
    script.setdefault("name", f"{test_name_for(spec.idempotency_key)} script {index + 1}")
    script.setdefault("protocol", "http")
    return script


def _split_vusers(total: int, group_count: int) -> list[int]:
    """Even split, remainder to the earliest groups, minimum 1 per group."""
    base, remainder = divmod(total, group_count)
    return [max(1, base + (1 if index < remainder else 0)) for index in range(group_count)]


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


# ── remote -> domain helpers ──────────────────────────────────────────────────


def _live_stats_from(test: dict[str, Any]) -> LiveStats | None:
    metrics = test.get("live_metrics")
    if not isinstance(metrics, dict):
        return None
    return LiveStats(
        vusers=float(metrics.get("active_vusers") or 0.0),
        tps=float(metrics.get("tps") or 0.0),
        error_rate=float(metrics.get("error_pct") or 0.0) / 100.0,
        p95_ms=float(metrics.get("p95_ms") or 0.0),
    )


def _progress_pct(test: dict[str, Any], phase: EngineRunPhase) -> float:
    if phase in TERMINAL_ENGINE_PHASES:
        return 100.0
    if phase in (EngineRunPhase.PROVISIONING, EngineRunPhase.READY):
        return 0.0
    duration_s: float | None = None
    raw_ns = test.get("duration_ns")
    if isinstance(raw_ns, int | float) and raw_ns > 0:
        duration_s = float(raw_ns) / 1e9
    else:
        duration_s = parse_go_duration_s(test.get("duration"))
    started_raw = test.get("started_at")
    if not duration_s or duration_s <= 0 or not isinstance(started_raw, str):
        return 0.0
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return round(max(0.0, min(99.0, elapsed / duration_s * 100.0)), 1)


def _kpis_from_report(report: dict[str, Any]) -> dict[str, float]:
    raw_overview = report.get("overview")
    overview: dict[str, Any] = raw_overview if isinstance(raw_overview, dict) else {}
    timeline = [
        point for point in (report.get("summary_timeline") or []) if isinstance(point, dict)
    ]
    tps_points = [float(p["tps"]) for p in timeline if isinstance(p.get("tps"), int | float)]
    p95_points = [float(p["p95_ms"]) for p in timeline if isinstance(p.get("p95_ms"), int | float)]

    tps_avg = (
        sum(tps_points) / len(tps_points) if tps_points else float(overview.get("peak_tps") or 0.0)
    )
    if p95_points:
        p95_ms = sum(p95_points) / len(p95_points)
    else:  # fall back to a transaction-weighted average of per-action p95s
        p95_ms = 0.0
        by_action = report.get("by_action")
        if isinstance(by_action, dict):
            weighted = 0.0
            total_tx = 0
            for stats in by_action.values():
                if not isinstance(stats, dict):
                    continue
                tx = int(stats.get("transactions") or 0)
                weighted += float(stats.get("p95_ms") or 0.0) * tx
                total_tx += tx
            if total_tx > 0:
                p95_ms = weighted / total_tx
    return {
        "tps_avg": round(tps_avg, 1),
        "p95_ms": round(p95_ms, 1),
        "error_rate": float(overview.get("error_pct") or 0.0) / 100.0,
        "vusers_peak": float(overview.get("peak_active_vusers") or 0.0),
    }


def _kpis_from_live(test: dict[str, Any]) -> dict[str, float]:
    metrics = test.get("live_metrics")
    if not isinstance(metrics, dict):
        return {}
    vusers_peak = float(test.get("total_vusers") or metrics.get("active_vusers") or 0.0)
    return {
        "tps_avg": float(metrics.get("tps") or 0.0),
        "p95_ms": float(metrics.get("p95_ms") or 0.0),
        "error_rate": float(metrics.get("error_pct") or 0.0) / 100.0,
        "vusers_peak": vusers_peak,
    }


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return response.text[:300]


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"apex load {context} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"apex load {context} returned non-object JSON")
    return data


# ── adapter ───────────────────────────────────────────────────────────────────


@AdapterRegistry.register(PortKind.EXECUTION_ENGINE, PROVIDER)
class ApexLoadExecutionEngine:
    """ExecutionEnginePort against APEX Load. Stateless: every method is driven
    from EngineHandle.external_run_id plus remote queries (contract req. 6)."""

    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        base_url = str(options.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(
                f"apex_load connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://apexload.internal:8080")'
            )
        if secret is None:
            raise ValueError(
                f"apex_load connection {conn.id!r} requires a service-account API key; "
                'set secret_ref on the connection (e.g. "env:APEX_APEXLOAD_API_KEY")'
            )
        self._conn = conn
        self._base_url = base_url
        self._project_id = str(options.get("project_id") or "") or None
        self._headers = {"X-APEXLoad-API-Key": secret.value, "Accept": "application/json"}
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Lazy client, rebuilt when the running event loop changes."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = httpx.AsyncClient(
                base_url=self._base_url, headers=self._headers, timeout=_TIMEOUT_S
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
        timeout_s: float | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        request_timeout = timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT
        try:
            response = await self._client().request(
                method, path, json=json, params=params, timeout=request_timeout
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"apex load request {method} {path} failed before a response arrived: {exc}"
            ) from exc
        if response.status_code == 404 and not_found is not None:
            raise KeyError(not_found)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"apex load rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's "
                "X-APEXLoad-API-Key secret and the service account's scopes"
            )
        if response.status_code in (400, 422):
            raise ValueError(f"apex load rejected the request: {_error_text(response)}")
        if response.status_code == 409:
            raise RuntimeError(
                f"apex load refused {method} {path} (HTTP 409): {_error_text(response)}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"apex load {method} {path} failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        return response

    def _run_id(self, handle: EngineHandle) -> str:
        if not handle.external_run_id:
            raise ValueError("apex_load handle has no external_run_id; call provision() first")
        return handle.external_run_id

    # ── port surface ──────────────────────────────────────────────────────────

    async def validate(self, spec: LoadTestSpec) -> ValidationReport:
        """Local structural checks, then APEX Load's server-side script validation
        (POST /api/v1/scripts/validate for drafts; POST /scripts/{id}/validate
        for named refs)."""
        issues: list[str] = []
        if spec.vusers < 1:
            issues.append("vusers must be >= 1")
        if spec.duration_s <= 0:
            issues.append("duration_s must be > 0")
        if spec.ramp_s < 0:
            issues.append("ramp_s must be >= 0")

        inline_scripts: list[dict[str, Any]] = []
        named_refs: list[str] = []
        for index, ref in enumerate(spec.script_refs):
            if _is_inline_ref(ref):
                try:
                    parsed = json.loads(ref)
                except ValueError as exc:
                    issues.append(f"script_refs[{index}] is not valid JSON: {exc}")
                    continue
                if not isinstance(parsed, dict):
                    issues.append(
                        f"script_refs[{index}] must be a JSON object (APEX Load DSL script)"
                    )
                    continue
                inline_scripts.append(_prepare_inline_script(parsed, spec, index))
            else:
                named_refs.append(ref)
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
            if not data.get("valid", False):
                issues.extend(str(issue) for issue in data.get("issues") or [])
        for ref in named_refs:
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
            if not data.get("valid", False):
                issues.extend(str(issue) for issue in data.get("issues") or [])
        return ValidationReport(ok=not issues, issues=issues)

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        """Get-or-create by spec.idempotency_key via the remote test NAME
        "apex-orch:<key>" (list first, create only when absent)."""
        name = test_name_for(spec.idempotency_key)
        test = await self._find_test_by_name(name)
        if test is None:
            test = await self._create_test(name, spec)
            logger.info("apex_load.test_created", test_id=test.get("id"), test_name=name)
        else:
            logger.info("apex_load.test_reused", test_id=test.get("id"), test_name=name)
        extras = {"test_name": name}
        if self._project_id:
            extras["project_id"] = self._project_id
        return EngineHandle(
            engine=PROVIDER,
            connection_id=self._conn.id,
            external_run_id=str(test.get("id") or ""),
            idempotency_key=spec.idempotency_key,
            extras=extras,
        )

    async def start(self, handle: EngineHandle) -> None:
        """POST /tests/{id}/start; tolerates the already-started rejection."""
        run_id = self._run_id(handle)
        try:
            await self._request(
                "POST",
                f"/api/v1/tests/{run_id}/start",
                not_found=f"apex load test {run_id!r} not found",
            )
        except ValueError as exc:
            if _ALREADY_STARTED_MARKER in str(exc):
                logger.info("apex_load.start_noop", external_run_id=run_id, detail=str(exc))
                return
            raise

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        """GET /tests/{id}: one cheap read per poll cycle."""
        run_id = self._run_id(handle)
        response = await self._request(
            "GET",
            f"/api/v1/tests/{run_id}",
            not_found=f"apex load test {run_id!r} not found",
        )
        test = _json_object(response, f"GET /api/v1/tests/{run_id}")
        raw_status = str(test.get("status") or "").upper()
        phase = _PHASE_BY_STATUS.get(raw_status)
        message = f"APEX Load test {run_id} is {raw_status or 'UNKNOWN'}"
        if phase is None:
            phase = EngineRunPhase.RUNNING
            message += " (unmapped status; treating as running)"
        if test.get("error"):
            message += f": {test['error']}"
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
        try:
            await self._request(
                "POST",
                f"/api/v1/tests/{run_id}/abort",
                not_found=f"apex load test {run_id!r} not found",
            )
        except (KeyError, ValueError) as exc:
            detail = exc.args[0] if exc.args else str(exc)
            logger.info(
                "apex_load.abort_noop", external_run_id=run_id, reason=reason, detail=detail
            )
            return
        logger.info("apex_load.abort", external_run_id=run_id, reason=reason)

    async def collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        """Stream the archive report JSON into the artifact store. Runs aborted
        before start never archive: a 404 yields zero artifacts, not an error."""
        run_id = self._run_id(handle)
        try:
            response = await self._request(
                "GET",
                f"/api/v1/tests/{run_id}/archive/report",
                timeout_s=_DOWNLOAD_TIMEOUT_S,
                not_found=f"apex load archive for test {run_id!r} not found",
            )
        except KeyError:
            logger.warning("apex_load.archive_missing", external_run_id=run_id)
            return []
        key = f"engine-runs/{run_id}/apex-load-report.json"
        stored = await store.put(key, response.content, content_type="application/json")
        ref = ArtifactRef(
            kind="engine_report",
            name="apex-load-report.json",
            uri=stored.uri,
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
        status = str(sla.get("status") or "").upper()
        breached = bool(sla.get("sla_breached"))
        breaches = [str(detail) for detail in sla.get("details") or []]

        kpis: dict[str, float] = {}
        notes = f"APEX Load test {run_id} status {status or 'UNKNOWN'}"
        try:
            report_response = await self._request(
                "GET",
                f"/api/v1/tests/{run_id}/archive/report",
                timeout_s=_DOWNLOAD_TIMEOUT_S,
                not_found=f"apex load archive for test {run_id!r} not found",
            )
        except KeyError:
            kpis = await self._live_kpis(run_id)
            notes += "; archive report unavailable, KPIs from live metrics"
        else:
            report = _json_object(report_response, f"GET /api/v1/tests/{run_id}/archive/report")
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
        logger.debug("apex_load.teardown_noop", external_run_id=handle.external_run_id)

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
        return _kpis_from_live(_json_object(response, f"GET /api/v1/tests/{run_id}"))

    # ── provisioning internals ────────────────────────────────────────────────

    async def _find_test_by_name(self, name: str) -> dict[str, Any] | None:
        params = {"project_id": self._project_id} if self._project_id else None
        response = await self._request("GET", "/api/v1/tests", params=params)
        data = _json_object(response, "GET /api/v1/tests")
        for test in data.get("tests") or []:
            if isinstance(test, dict) and test.get("name") == name:
                return test
        return None

    async def _upload_script(self, script: dict[str, Any]) -> str:
        payload: dict[str, Any] = {"script": script}
        if self._project_id:
            payload["project_id"] = self._project_id
        response = await self._request("POST", "/api/v1/scripts", json=payload)
        stored = _json_object(response, "POST /api/v1/scripts")
        script_id = str(stored.get("id") or "")
        if not script_id:
            raise RuntimeError(
                "apex load POST /api/v1/scripts returned no script id; cannot build the test"
            )
        return script_id

    async def _create_test(self, name: str, spec: LoadTestSpec) -> dict[str, Any]:
        script_ids: list[str] = []
        for index, ref in enumerate(spec.script_refs):
            if _is_inline_ref(ref):
                parsed = json.loads(ref)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        f"script_refs[{index}] must be a JSON object (APEX Load DSL script)"
                    )
                script_ids.append(
                    await self._upload_script(_prepare_inline_script(parsed, spec, index))
                )
            else:
                script_ids.append(ref)
        if not script_ids:
            if not spec.target_environment:
                raise ValueError(
                    "the generated default workload needs spec.target_environment "
                    "(or provide script_refs)"
                )
            script_ids.append(await self._upload_script(_default_script(spec)))

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
        response = await self._request("POST", "/api/v1/tests", json=body)
        return _json_object(response, "POST /api/v1/tests")
