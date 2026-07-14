"""LoadRunner Enterprise (LRE, formerly Performance Center) execution-engine
adapter (provider "loadrunner", PortKind.EXECUTION_ENGINE).

Wire surface: the LRE 2023.x REST API, JSON via ``Accept: application/json``.
Connection options: {"base_url": "https://lre.internal", "domain": "<LRE
domain>", "project": "<LRE project>", "test_id"?: int, "test_instance_id"?:
int (-1 = let LRE auto-assign), "abortive_stop"?: bool}; the secret is
"user:password" for the LRE authentication point.

Auth flow: POST /LoadTest/rest/authentication-point/authenticate with HTTP
basic credentials; the LWSSO session cookie from Set-Cookie rides the client's
cookie jar on every subsequent call. Any API call answered with 401 (expired
session) triggers exactly one re-authentication + retry; a second 401 maps to
an actionable RuntimeError. Project base path:
/LoadTest/rest/domains/{domain}/projects/{project}.

Port-method -> LRE endpoint mapping:

    validate           local checks only (vusers/duration/ramp + a resolvable
                       test id); remote test existence surfaces at start()
    provision          GET  Runs?query={test-id[N]}   (idempotency lookup)
    start              GET  Runs?query={test-id[N]}   (get-or-create re-check)
                       POST Runs                      (create -> run starts)
    get_status         GET  Runs/{RunID}
    abort              GET  Runs/{RunID} (pre-check) +
                       POST Runs/{RunID}/stop  (graceful) or
                       POST Runs/{RunID}/abort (extras["abortive_stop"]="true")
    collect_artifacts  GET  Runs/{RunID}/Results +
                       GET  Runs/{RunID}/Results/{ResultID}/data (zip, 60s)
    fetch_summary      GET  Runs/{RunID}
    teardown           no-op (LRE releases the timeslot itself; see below)

The LRE-specific provision/start split: LRE cannot create a test definition
from a LoadTestSpec — it runs EXISTING tests, so provision() only resolves the
target test id (options["test_id"], overridden by an "lre-test:<id>" entry in
spec.script_refs) and performs the idempotency lookup: a run whose RunComment
carries "apex-orch:<idempotency_key>" is adopted. The run itself is created at
start() time (POST Runs both creates AND starts an LRE run), with the
RunComment carrying the key so any later lookup — including provision() from a
fresh process — finds it. start() re-runs the same lookup before POSTing, so a
crash between run creation and the spine's checkpoint cannot double-start
load; if several runs ever carry the same comment, the lowest RunID (the
first created) wins deterministically. The final lookup + POST is serialized by
a cross-event-loop process guard and, in locked multi-replica deployments, a
PostgreSQL advisory lock; LRE itself has no atomic idempotency-key API.

LRE RunState -> EngineRunPhase mapping (case-insensitive):

    Pending Creation                PROVISIONING
    Initializing                    PROVISIONING
    Running                         RUNNING
    Stopping                        STOPPING
    Halting                         STOPPING
    Before Collating Results        COLLECTING
    Collating Results               COLLECTING
    Before Creating Analysis Data   COLLECTING
    Creating Analysis Data          COLLECTING
    Finished                        COMPLETED
    Failed Collating Results        FAILED   (results unrecoverable via API)
    Failed Creating Analysis Data   FAILED   (results unrecoverable via API)
    Run Failure                     FAILED
    Canceled / Cancelled / Aborted  ABORTED
    <anything else>                 RUNNING  (non-terminal; the spine's poll
                                    timeout is the safety net — documented)

Documented v1 limitations (honest defaults, future work):
- live_stats is always None: LRE online metrics live behind the run dashboard
  API, which is not cleanly exposed REST-side; the port allows phase-only
  status. progress_pct for RUNNING is estimated from the run's elapsed
  Duration (minutes) vs the spec duration, capped at 95.
- fetch_summary KPIs are empty: real tps_avg/p95_ms/error_rate/vusers_peak
  need the LRE Analysis report parsed. passed = (RunState == Finished) and
  RunSLAStatus (when LRE supplies it on the run entity) is not "Failed";
  an SLA failure becomes the single sla_breaches entry.
- teardown is a no-op: the timeslot reserved at POST Runs is released by LRE
  itself when the run reaches a terminal state; there is no separate resource
  for the adapter to free, and already-gone runs must never raise.
- abort(reason) is logged locally only — the LRE stop endpoints take no
  reason field.

Statelessness: everything get_status/abort/collect/fetch_summary need rides
EngineHandle.extras as strings ("test_id", "run_id", "duration_s",
"timeslot_minutes", "vusers", optional "abortive_stop") or is re-queried from
LRE. The cached LWSSO session is a pure cache — any fresh instance just
re-authenticates.
"""

import asyncio
import base64
import math
from typing import Any

import httpx
import structlog

from apex.adapters.http_resilience import (
    CircuitBreaker,
    CircuitOpenError,
    read_stream_error_preview,
    resilient_request,
    resilient_stream_request,
    retry_policy,
)
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.adapters.remote_idempotency import remote_create_guard
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
    EngineRunPhase,
    EngineRunStatus,
)

logger = structlog.get_logger(__name__)

PROVIDER = "loadrunner"
COMMENT_PREFIX = "apex-orch:"
AUTH_PATH = "/LoadTest/rest/authentication-point/authenticate"

_SCRIPT_REF_PREFIX = "lre-test:"
_TIMEOUT_S = 15.0
_DOWNLOAD_TIMEOUT_S = 60.0  # results zips can be large
_DEFAULT_MAX_REPORT_BYTES = 512 * 1024 * 1024
_MIN_TIMESLOT_MINUTES = 30  # LRE's floor for a timeslot reservation
_TIMESLOT_HEADROOM_MINUTES = 15  # init + collation slack on top of the test window
_POST_RUN_ACTION = "Collate And Analyze"
_CREATE_RECHECK_ATTEMPTS = 6
_CREATE_RECHECK_DELAY_S = 0.05

# LRE RunState -> EngineRunPhase (lowercased lookup; table in module docstring).
RUN_STATE_PHASES: dict[str, EngineRunPhase] = {
    "pending creation": EngineRunPhase.PROVISIONING,
    "initializing": EngineRunPhase.PROVISIONING,
    "running": EngineRunPhase.RUNNING,
    "stopping": EngineRunPhase.STOPPING,
    "halting": EngineRunPhase.STOPPING,
    "before collating results": EngineRunPhase.COLLECTING,
    "collating results": EngineRunPhase.COLLECTING,
    "before creating analysis data": EngineRunPhase.COLLECTING,
    "creating analysis data": EngineRunPhase.COLLECTING,
    "finished": EngineRunPhase.COMPLETED,
    "failed collating results": EngineRunPhase.FAILED,
    "failed creating analysis data": EngineRunPhase.FAILED,
    "run failure": EngineRunPhase.FAILED,
    "canceled": EngineRunPhase.ABORTED,
    "cancelled": EngineRunPhase.ABORTED,
    "aborted": EngineRunPhase.ABORTED,
}


def resolve_test_id(spec: LoadTestSpec, default: int | None) -> int:
    """Target LRE test id: "lre-test:<id>" in script_refs beats options["test_id"]."""
    for ref in spec.script_refs:
        if ref.startswith(_SCRIPT_REF_PREFIX):
            raw = ref[len(_SCRIPT_REF_PREFIX) :]
            try:
                return int(raw)
            except ValueError:
                raise ValueError(
                    f"script ref {ref!r} does not carry a numeric LRE test id "
                    f'(expected "lre-test:<id>")'
                ) from None
    if default is not None:
        return default
    raise ValueError(
        "loadrunner adapter needs an existing LRE test to run: set options['test_id'] "
        "on the connection or include 'lre-test:<id>' in spec.script_refs (LRE cannot "
        "create test definitions from a LoadTestSpec)"
    )


def timeslot_minutes(spec: LoadTestSpec) -> int:
    """Timeslot reservation: test window + collation headroom, floored at LRE's 30."""
    window = math.ceil((float(spec.duration_s) + float(spec.ramp_s)) / 60.0)
    return max(_MIN_TIMESLOT_MINUTES, window + _TIMESLOT_HEADROOM_MINUTES)


def _is_report(result: dict[str, Any]) -> bool:
    """Pick analysis/report result files (HTML Report, Rich Report, Analyzed Result)."""
    kind = str(result.get("Type") or "").lower()
    return "analyzed" in kind or "report" in kind


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        parts = [
            str(data[field])
            for field in ("ExceptionMessage", "ErrorMessage", "Message")
            if data.get(field)
        ]
        code = data.get("ErrorCode")
        if code is not None:
            parts.append(f"(LRE error code {code})")
        if parts:
            return " ".join(parts)
    return response.text[:300]


@AdapterRegistry.register(PortKind.EXECUTION_ENGINE, PROVIDER)
class LoadRunnerExecutionEngine:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = dict(conn.options)
        base_url = str(options.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(
                f"loadrunner connection {conn.id!r} is missing options['base_url'] "
                '(e.g. "https://lre.internal")'
            )
        domain = str(options.get("domain") or "")
        if not domain:
            raise ValueError(f"loadrunner connection {conn.id!r} is missing options['domain']")
        project = str(options.get("project") or "")
        if not project:
            raise ValueError(f"loadrunner connection {conn.id!r} is missing options['project']")
        if secret is None:
            raise ValueError(
                f"loadrunner connection {conn.id!r} requires a 'user:password' secret; set "
                'secret_ref on the connection (e.g. "env:APEX_INTEGRATION_LRE_CREDENTIALS")'
            )
        user, sep, password = secret.value.partition(":")
        if not sep or not user:
            raise ValueError(
                f"loadrunner connection {conn.id!r} secret must be 'user:password' "
                "for the LRE authentication point"
            )
        self._conn_id = conn.id
        self._base_url = base_url
        self._allow_private_hosts = private_hosts_allowed(options)
        self._project = project
        self._project_base = f"/LoadTest/rest/domains/{domain}/projects/{project}"
        raw_test_id = options.get("test_id")
        self._default_test_id = int(raw_test_id) if raw_test_id is not None else None
        raw_instance = options.get("test_instance_id")
        # -1 asks LRE to auto-assign/create the test instance for the test set.
        self._test_instance_id = int(raw_instance) if raw_instance is not None else -1
        raw_abortive_stop = options.get("abortive_stop", False)
        if not isinstance(raw_abortive_stop, bool):
            raise ValueError("loadrunner abortive_stop must be a boolean")
        self._abortive_stop = raw_abortive_stop
        self._max_report_bytes = int(options.get("max_report_bytes", _DEFAULT_MAX_REPORT_BYTES))
        if self._max_report_bytes < 1:
            raise ValueError("loadrunner max_report_bytes must be >= 1")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._basic_auth = f"Basic {token}"
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._session_ok = False  # LWSSO cookie present in the current client's jar
        self._breaker = CircuitBreaker(f"loadrunner:{conn.id}")

    # ── http plumbing ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Lazy client, rebuilt when the running event loop changes (resolver
        caches adapter instances across graph-node loops). A rebuilt client has
        an empty cookie jar, so the LWSSO session is re-established lazily."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = safe_async_http_client(
                base_url=self._base_url,
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT_S,
                allow_private_hosts=self._allow_private_hosts,
            )
            self._http_loop = loop
            self._session_ok = False
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._http_loop = None
        self._session_ok = False

    async def _authenticate(self, client: httpx.AsyncClient) -> None:
        """POST the authentication point with basic credentials; the LWSSO
        session cookie from Set-Cookie lands in the client's cookie jar."""
        try:
            response = await client.post(AUTH_PATH, headers={"Authorization": self._basic_auth})
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"loadrunner authentication failed before a response arrived: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"loadrunner rejected credentials for connection {self._conn_id!r} "
                f"(HTTP {response.status_code}): check the connection's 'user:password' secret"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"loadrunner authentication failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        self._session_ok = True

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        breaker = (
            self._breaker
            if method.upper() == "GET" and path.startswith(f"{self._project_base}/Runs/")
            else None
        )
        try:
            return await resilient_request(
                client,
                method,
                path,
                json=json_body,
                params=params,
                timeout=timeout_s if timeout_s is not None else _TIMEOUT_S,
                breaker=breaker,
            )
        except CircuitOpenError as exc:
            raise RuntimeError(f"loadrunner request circuit is open for {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"loadrunner request {method} {path} failed before a response arrived: {exc}"
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        not_found: str | None = None,
    ) -> httpx.Response:
        client = self._client()
        if not self._session_ok:
            await self._authenticate(client)
        response = await self._send(
            client, method, path, json_body=json_body, params=params, timeout_s=timeout_s
        )
        if response.status_code == 401:
            # LWSSO session expired mid-flight: re-authenticate ONCE and retry.
            self._session_ok = False
            await self._authenticate(client)
            response = await self._send(
                client, method, path, json_body=json_body, params=params, timeout_s=timeout_s
            )
        if response.status_code == 404:
            raise KeyError(not_found or f"loadrunner resource not found: {method} {path}")
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"loadrunner rejected credentials for {method} {path} "
                f"(HTTP {response.status_code}): check the connection's 'user:password' secret"
            )
        if response.status_code in (400, 422):
            raise ValueError(f"loadrunner rejected the request: {_error_text(response)}")
        if response.status_code >= 400:
            raise RuntimeError(
                f"loadrunner {method} {path} failed with HTTP {response.status_code}: "
                f"{_error_text(response)}"
            )
        return response

    async def _stream_download(self, path: str, *, not_found: str) -> httpx.Response:
        """Open an authenticated streaming response; caller must ``aclose`` it."""
        client = self._client()
        if not self._session_ok:
            await self._authenticate(client)

        async def _send() -> httpx.Response:
            try:
                return await resilient_stream_request(
                    client,
                    "GET",
                    path,
                    timeout=_DOWNLOAD_TIMEOUT_S,
                    retry=retry_policy(total_timeout_s=None),
                    breaker=self._breaker,
                )
            except CircuitOpenError as exc:
                raise RuntimeError(f"loadrunner request circuit is open for GET {path}") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"loadrunner request GET {path} failed before a response arrived: {exc}"
                ) from exc

        response = await _send()
        if response.status_code == 401:
            await response.aclose()
            self._session_ok = False
            await self._authenticate(client)
            response = await _send()
        if response.status_code < 400:
            return response
        preview = await read_stream_error_preview(response)
        preview_response = httpx.Response(response.status_code, content=preview)
        try:
            if response.status_code == 404:
                raise KeyError(not_found)
            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"loadrunner rejected credentials for GET {path} (HTTP {response.status_code})"
                )
            raise RuntimeError(
                f"loadrunner GET {path} failed with HTTP {response.status_code}: "
                f"{_error_text(preview_response)}"
            )
        finally:
            await response.aclose()

    # ── handle / run helpers ──────────────────────────────────────────────────

    def _test_id_from(self, handle: EngineHandle) -> int:
        try:
            return int(handle.extras["test_id"])
        except KeyError:
            raise ValueError(
                f"handle for run {handle.external_run_id!r} was not provisioned by the "
                "loadrunner engine (missing extras['test_id']); call provision() first"
            ) from None

    def _run_id_from(self, handle: EngineHandle) -> str:
        run_id = handle.extras.get("run_id")
        if not run_id:
            self._test_id_from(handle)  # not-provisioned beats not-started
            raise ValueError(
                f"loadrunner run for key {handle.idempotency_key!r} has not been created "
                "yet; call start() first"
            )
        return run_id

    async def _find_run_by_comment(self, test_id: int, key: str) -> dict[str, Any] | None:
        """Idempotency lookup: the run whose RunComment carries apex-orch:<key>.

        Filters the Runs collection by test id server-side, scans RunComment
        client-side. Multiple matches (should never happen) resolve to the
        lowest RunID — the first run created — deterministically.
        """
        response = await self._request(
            "GET",
            f"{self._project_base}/Runs",
            params={"query": f"{{test-id[{test_id}]}}"},
        )
        payload = response.json()
        runs = payload if isinstance(payload, list) else list(payload.get("Runs") or [])
        marker = COMMENT_PREFIX + key
        matches = [run for run in runs if str(run.get("RunComment") or "").strip() == marker]
        if not matches:
            return None
        return min(matches, key=lambda run: int(run.get("ID") or 0))

    async def _find_run_after_ambiguous_create(
        self, test_id: int, key: str
    ) -> dict[str, Any] | None:
        """Boundedly reconcile a POST whose response may have been lost."""

        for attempt in range(_CREATE_RECHECK_ATTEMPTS):
            try:
                run = await self._find_run_by_comment(test_id, key)
            except RuntimeError:
                run = None
            if run is not None:
                return run
            if attempt + 1 < _CREATE_RECHECK_ATTEMPTS:
                await asyncio.sleep(_CREATE_RECHECK_DELAY_S * (attempt + 1))
        return None

    async def _get_run(self, run_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"{self._project_base}/Runs/{run_id}",
            not_found=f"LRE run {run_id} not found in project {self._project!r}",
        )
        return response.json()

    def _phase_for(self, run: dict[str, Any]) -> tuple[EngineRunPhase, str]:
        raw_state = str(run.get("RunState") or "")
        phase = RUN_STATE_PHASES.get(raw_state.lower())
        message = f"LRE run state: {raw_state or 'unknown'}"
        if phase is None:
            # Unknown states stay non-terminal; the spine's poll timeout is the net.
            phase = EngineRunPhase.RUNNING
            message += " (unmapped state; treated as running until the poll timeout)"
        return phase, message

    def _progress_for(
        self, phase: EngineRunPhase, run: dict[str, Any], handle: EngineHandle
    ) -> float:
        if phase in TERMINAL_ENGINE_PHASES:
            return 100.0
        if phase is EngineRunPhase.COLLECTING:
            return 99.0
        if phase is EngineRunPhase.STOPPING:
            return 95.0
        if phase is not EngineRunPhase.RUNNING:
            return 0.0
        # RUNNING: estimate from the run's elapsed Duration (minutes) vs the
        # spec duration carried in extras; capped at 95 until LRE goes terminal.
        try:
            duration_s = float(handle.extras.get("duration_s") or 0.0)
            elapsed_s = float(run["Duration"]) * 60.0
        except (KeyError, TypeError, ValueError):
            return 0.0
        if duration_s <= 0:
            return 0.0
        return round(min(95.0, elapsed_s / duration_s * 100.0), 1)

    # ── port surface ──────────────────────────────────────────────────────────

    async def validate(self, spec: LoadTestSpec) -> ValidationReport:
        """Local checks only; remote test existence surfaces at start() (404)."""
        issues: list[str] = []
        if spec.vusers < 1:
            issues.append("vusers must be >= 1")
        if spec.duration_s <= 0:
            issues.append("duration_s must be > 0")
        if spec.ramp_s < 0:
            issues.append("ramp_s must be >= 0")
        try:
            resolve_test_id(spec, self._default_test_id)
        except ValueError as exc:
            issues.append(str(exc))
        return ValidationReport(ok=not issues, issues=issues)

    async def provision(self, spec: LoadTestSpec) -> EngineHandle:
        """Resolve the target test + adopt any existing run carrying the key.

        LRE-specific split (module docstring): no run is created here — POST
        Runs both creates and starts an LRE run, so creation belongs to
        start(). Re-provisioning after the run exists (fresh process included)
        finds it via the RunComment marker and yields the same external_run_id.
        """
        test_id = resolve_test_id(spec, self._default_test_id)
        extras: dict[str, str] = {
            "test_id": str(test_id),
            "duration_s": f"{float(spec.duration_s):g}",
            "timeslot_minutes": str(timeslot_minutes(spec)),
            "vusers": str(spec.vusers),
        }
        if self._abortive_stop:
            extras["abortive_stop"] = "true"
        external_run_id: str | None = None
        existing = await self._find_run_by_comment(test_id, spec.idempotency_key)
        if existing is not None:
            run_id = str(existing["ID"])
            extras["run_id"] = run_id
            external_run_id = f"lre-{run_id}"
        return EngineHandle(
            engine=PROVIDER,
            connection_id=self._conn_id,
            external_run_id=external_run_id,
            idempotency_key=spec.idempotency_key,
            extras=extras,
        )

    async def start(self, handle: EngineHandle) -> None:
        """Get-or-create the LRE run; POST Runs creates AND starts it.

        Idempotent: a handle that already carries run_id is a no-op.  The
        comment lookup re-runs inside a local/distributed creation guard before
        POSTing, so concurrent workers and crash recovery cannot double-start
        load.
        """
        if handle.extras.get("run_id"):
            return  # run already exists; LRE runs start at creation
        test_id = self._test_id_from(handle)
        guard_key = ":".join(
            (
                PROVIDER,
                self._base_url,
                self._project_base,
                str(test_id),
                handle.idempotency_key,
            )
        )
        async with remote_create_guard(guard_key):
            # A concurrent call may share this handle, and a distinct adapter may
            # have created the remote run.  Repeat both checks under the guard.
            if handle.extras.get("run_id"):
                return
            run = await self._find_run_by_comment(test_id, handle.idempotency_key)
            if run is None:
                body = {
                    "TestID": test_id,
                    "TestInstanceID": self._test_instance_id,
                    "PostRunAction": _POST_RUN_ACTION,
                    "TimeslotDuration": int(
                        handle.extras.get("timeslot_minutes") or _MIN_TIMESLOT_MINUTES
                    ),
                    "VudsMode": False,
                    "RunComment": COMMENT_PREFIX + handle.idempotency_key,
                }
                try:
                    response = await self._request(
                        "POST",
                        f"{self._project_base}/Runs",
                        json_body=body,
                        not_found=f"LRE test {test_id} not found in project {self._project!r}",
                    )
                    run = response.json()
                except (RuntimeError, ValueError) as exc:
                    run = await self._find_run_after_ambiguous_create(
                        test_id, handle.idempotency_key
                    )
                    if run is None:
                        raise
                    logger.warning(
                        "loadrunner.run_create_reconciled",
                        test_id=test_id,
                        run_id=run.get("ID"),
                        error=str(exc),
                    )
        run_id = str(run["ID"])
        handle.extras["run_id"] = run_id
        handle.external_run_id = f"lre-{run_id}"
        logger.info(
            "loadrunner.run_started",
            external_run_id=handle.external_run_id,
            test_id=test_id,
        )

    async def get_status(self, handle: EngineHandle) -> EngineRunStatus:
        """One GET Runs/{id}; phase-only (live_stats=None — module docstring)."""
        run_id = handle.extras.get("run_id")
        if not run_id:
            self._test_id_from(handle)  # unprovisioned handles raise ValueError
            return EngineRunStatus(
                phase=EngineRunPhase.READY,
                message="LRE run not created yet; start() creates and starts it",
            )
        run = await self._get_run(run_id)
        phase, message = self._phase_for(run)
        return EngineRunStatus(
            phase=phase,
            progress_pct=self._progress_for(phase, run, handle),
            live_stats=None,
            message=message,
        )

    async def abort(self, handle: EngineHandle, *, reason: str) -> None:
        """Graceful POST Runs/{id}/stop (or /abort when extras flag it).

        Idempotent: an already-terminal/stopping run or a run that vanished
        mid-call is a quiet no-op. A handle without ``run_id`` is reconciled by
        its durable comment marker first: start() may have committed remotely
        before losing its response, and cleanup must not mistake that ambiguity
        for proof that no load is running.
        """
        run_id = handle.extras.get("run_id")
        run: dict[str, Any]
        if not run_id:
            test_id = self._test_id_from(handle)
            found = await self._find_run_by_comment(test_id, handle.idempotency_key)
            if found is None:
                logger.info("loadrunner.abort_noop", reason=reason, detail="no remote run found")
                return
            run = found
            run_id = str(run["ID"])
            handle.extras["run_id"] = run_id
            handle.external_run_id = f"lre-{run_id}"
        else:
            try:
                run = await self._get_run(run_id)
            except KeyError:
                return  # run is gone — nothing to stop
        phase, _ = self._phase_for(run)
        if phase in TERMINAL_ENGINE_PHASES or phase is EngineRunPhase.STOPPING:
            logger.info(
                "loadrunner.abort_noop",
                external_run_id=handle.external_run_id,
                state=run.get("RunState"),
                reason=reason,
            )
            return
        action = "abort" if handle.extras.get("abortive_stop") == "true" else "stop"
        logger.info(
            "loadrunner.abort",
            external_run_id=handle.external_run_id,
            action=action,
            reason=reason,
        )
        try:
            await self._request("POST", f"{self._project_base}/Runs/{run_id}/{action}")
        except KeyError:
            return  # raced to terminal/deleted between the pre-check and the stop

    async def collect_artifacts(
        self, handle: EngineHandle, store: ArtifactStorePort
    ) -> list[dict[str, Any]]:
        """Stream the run's analysis/report zips into the artifact store.

        Picks results whose Type looks like a report (Analyzed Result, HTML/
        Rich Report); when LRE produced none (e.g. collation failed), falls
        back to every listed result so raw data is still preserved.
        """
        run_id = self._run_id_from(handle)
        response = await self._request(
            "GET",
            f"{self._project_base}/Runs/{run_id}/Results",
            not_found=f"LRE run {run_id} has no results collection",
        )
        payload = response.json()
        results = payload if isinstance(payload, list) else list(payload.get("Results") or [])
        chosen = [result for result in results if _is_report(result)] or results
        refs: list[dict[str, Any]] = []
        for ordinal, result in enumerate(chosen):
            result_id = result.get("ID")
            name = str(result.get("Name") or f"result-{result_id}.zip")
            data_response = await self._stream_download(
                f"{self._project_base}/Runs/{run_id}/Results/{result_id}/data",
                not_found=f"LRE result {result_id} data not found for run {run_id}",
            )
            result_token = str(result_id) if result_id is not None else "unknown"
            key = engine_artifact_key(
                handle.idempotency_key,
                f"{ordinal:04d}-result-{result_token}-{name}",
            )
            try:
                stored = await store.put_stream(
                    key,
                    data_response.aiter_bytes(),
                    content_type="application/zip",
                    max_bytes=self._max_report_bytes,
                )
            finally:
                await data_response.aclose()
            ref = ArtifactRef(
                kind="engine_report",
                name=name,
                uri=stored.uri,
                key=stored.key,
                media_type="application/zip",
                summary=f"LRE {result.get('Type') or 'result'} for run {run_id}",
            )
            refs.append(ref.model_dump(mode="json"))
        return refs

    async def fetch_summary(self, handle: EngineHandle) -> TestResultSummary:
        """State-derived summary; KPIs empty (v1 limitation, module docstring)."""
        run_id = self._run_id_from(handle)
        run = await self._get_run(run_id)
        raw_state = str(run.get("RunState") or "unknown")
        phase, _ = self._phase_for(run)
        sla_status = str(run.get("RunSLAStatus") or "")
        sla_breaches: list[str] = []
        if sla_status.lower() == "failed":
            sla_breaches.append(
                "LRE reported run SLA status 'Failed' (per-SLA details require the "
                "Analysis report, not parsed in v1)"
            )
        passed = phase is EngineRunPhase.COMPLETED and not sla_breaches
        notes = (
            f"LRE run {run_id} state {raw_state!r}. v1 limitation: KPI extraction "
            "(tps_avg, p95_ms, error_rate, vusers_peak) requires parsing the LRE "
            "Analysis report — kpis are empty; passed derives from RunState plus "
            "RunSLAStatus when LRE provides it."
        )
        return TestResultSummary(
            engine=PROVIDER,
            passed=passed,
            kpis={},
            sla_breaches=sla_breaches,
            notes=notes,
        )

    async def teardown(self, handle: EngineHandle) -> None:
        """No-op: LRE releases the run's timeslot itself when the run reaches a
        terminal state, and never-raise-on-gone is part of the port contract."""
        logger.debug("loadrunner.teardown_noop", external_run_id=handle.external_run_id)
