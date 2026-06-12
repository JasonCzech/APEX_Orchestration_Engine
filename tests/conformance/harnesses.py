"""Provider harnesses for the cross-engine conformance suite (plan M5).

One engine-neutral LoadTestSpec (`conformance_spec`) drives every registered
execution engine through the same lifecycle. Each harness:

- yields FRESH adapter instances on demand, so process-restart simulation
  (re-provision with zero shared in-memory state) is a first-class operation;
- advances its remote one lifecycle step per `advance()` call, letting the
  suite observe a non-terminal poll before the terminal one;
- exposes capability flags for the documented provider limitations the suite
  must tolerate (sim's no-op abort, LoadRunner's empty v1 KPIs);
- counts remote side effects (`remote_create_count` / `remote_start_count`)
  so get-or-create and start idempotency are provable, not just non-raising.

The mocked remotes ("apex_load", "loadrunner") are tiny in-memory state
machines mounted on a respx router, with wire shapes lifted from the adapters'
own unit-test fixtures (tests/unit/test_apex_load_adapter.py and
tests/unit/test_loadrunner_adapter.py). Every handler enforces the provider's
auth scheme before answering: an unauthenticated call comes back 401, which
the adapters surface as an actionable RuntimeError and the test fails — making
"every call carries credentials" an executable property of the whole suite
rather than a per-test assertion.

"sim" runs the real adapter end to end; a connection-level ``duration_s``
override compresses the same 240s spec into ~0.3s of wall time, and
``advance()`` simply waits the compressed duration out.
"""

import base64
import json
import re
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx
import respx

from apex.adapters.apex_load.engine import ApexLoadExecutionEngine
from apex.adapters.loadrunner.engine import AUTH_PATH, LoadRunnerExecutionEngine
from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.sim_engine import SimExecutionEngine
from apex.domain.integrations import LoadTestSpec, SecretValue
from apex.ports.execution_engine import ExecutionEnginePort

PROVIDERS = ("sim", "apex_load", "loadrunner")

NORMALIZED_KPI_KEYS = frozenset({"tps_avg", "p95_ms", "error_rate", "vusers_peak"})

ARTIFACT_REF_KEYS = frozenset({"id", "kind", "name", "uri", "media_type"})


def conformance_spec(idempotency_key: str) -> LoadTestSpec:
    """The ONE LoadTestSpec every provider must run.

    Engine-neutral by design: no script_refs (apex_load generates its default
    workload from target_environment; loadrunner takes its test id from
    connection options; sim ignores scripts), normalized SLA keys only.
    """
    return LoadTestSpec(
        idempotency_key=idempotency_key,
        title="Conformance checkout load test",
        script_refs=[],
        vusers=20,
        ramp_s=5.0,
        duration_s=240.0,
        slas={"tps_avg": 80.0, "p95_ms": 500.0, "error_rate": 0.01},
        target_environment="https://shop.conformance.test",
    )


class EngineHarness:
    """Per-provider seam between the conformance suite and one engine adapter."""

    provider: str = ""
    # Capability flags for documented provider limitations (suite skips/degrades
    # with the flag's reason instead of failing the provider).
    remote_abort_supported: bool = True
    abort_limitation: str = ""
    summary_reports_kpis: bool = True
    kpi_limitation: str = ""

    def __enter__(self) -> "EngineHarness":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def fresh_adapter(self) -> ExecutionEnginePort:
        """A brand-new adapter instance — simulates a process restart."""
        raise NotImplementedError

    def make_spec(self, key_suffix: str) -> LoadTestSpec:
        return conformance_spec(f"conformance-{self.provider}-{key_suffix}")

    def enable_sla_breach(self) -> None:
        """Flip the remote into a mode where the run finishes breaching its SLA."""
        raise NotImplementedError

    def advance(self) -> None:
        """Move the remote run one lifecycle step toward a terminal state."""
        raise NotImplementedError

    def remote_create_count(self) -> int | None:
        """How many load tests/runs were ever created remotely (None: untracked)."""
        return None

    def remote_start_count(self) -> int | None:
        """How many times load actually started remotely (None: untracked)."""
        return None


# ── sim: the real adapter, time-compressed ────────────────────────────────────

_SIM_DURATION_S = 0.3


class SimHarness(EngineHarness):
    provider = "sim"
    remote_abort_supported = False
    abort_limitation = (
        "sim abort is a documented logged no-op (M1 limitation in the module "
        "docstring): there is no server-side run to kill — the execution spine "
        "stops polling instead, so the run never reports an 'aborted' phase"
    )

    def __init__(self) -> None:
        self._sla_breach = False

    def fresh_adapter(self) -> ExecutionEnginePort:
        options: dict[str, Any] = {"duration_s": _SIM_DURATION_S}
        if self._sla_breach:
            options["fail_at_pct"] = 50.0
        conn = ConnectionConfig(
            id="conformance-sim",
            kind=PortKind.EXECUTION_ENGINE,
            provider="sim",
            name="Conformance sim engine",
            options=options,
        )
        return SimExecutionEngine(conn)

    def enable_sla_breach(self) -> None:
        # Sim has no server-side SLA evaluation; the documented failure-injection
        # option is its SLA-breach analogue (summary: passed=False + breach note).
        self._sla_breach = True

    def advance(self) -> None:
        time.sleep(_SIM_DURATION_S + 0.1)  # wait the compressed run window out


# ── apex_load: in-memory APEX Load control plane behind respx ────────────────

APEX_LOAD_BASE = "https://apexload.conformance.test"
_APEX_LOAD_HOST = "apexload.conformance.test"
APEX_LOAD_API_KEY = "conformance-apexload-key"

_APEX_STARTABLE = frozenset({"QUEUED", "PENDING", "RESERVED", "SCHEDULED", "SHAKEOUT_REVIEW"})
_APEX_TERMINAL = frozenset({"COMPLETE", "FAILED", "ABORTED"})


class ApexLoadServer:
    """Just enough of the Go pkg/api surface for one managed-test lifecycle."""

    def __init__(self) -> None:
        self.tests: dict[str, dict[str, Any]] = {}
        self.scripts: dict[str, dict[str, Any]] = {}
        self.test_creates = 0
        self.starts = 0
        self.sla_breach = False

    # -- lifecycle clock ------------------------------------------------------

    def advance(self) -> None:
        for test in self.tests.values():
            if test["status"] == "RUNNING":
                test["status"] = "COMPLETE"
                test.pop("live_metrics", None)

    # -- handlers (respx side effects) ----------------------------------------

    def _denied(self, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("X-APEXLoad-API-Key") != APEX_LOAD_API_KEY:
            return httpx.Response(401, json={"error": "authentication required"})
        return None

    def validate_draft(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        return httpx.Response(
            200, json={"script_id": "", "valid": True, "issues": [], "warnings": []}
        )

    def create_script(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        script = dict(json.loads(request.content)["script"])
        script_id = f"script-{len(self.scripts) + 1}"
        script["id"] = script_id
        self.scripts[script_id] = script
        return httpx.Response(201, json=script)

    def list_tests(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        return httpx.Response(
            200, json={"tests": list(self.tests.values()), "count": len(self.tests)}
        )

    def create_test(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        body = json.loads(request.content)
        test_id = f"test-{len(self.tests) + 1}"
        record: dict[str, Any] = {
            "id": test_id,
            "name": body["name"],
            "status": "QUEUED",
            "groups": body["groups"],
            "total_vusers": sum(int(group["vusers"]) for group in body["groups"]),
            "duration": body["duration"],
            "goal_config": body.get("goal_config") or {},
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.tests[test_id] = record
        self.test_creates += 1
        return httpx.Response(201, json=record)

    def get_test(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        test = self.tests.get(str(kwargs["test_id"]))
        if test is None:
            return httpx.Response(404, json={"error": "test not found"})
        return httpx.Response(200, json=test)

    def start_test(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        test_id = str(kwargs["test_id"])
        test = self.tests.get(test_id)
        if test is None:
            return httpx.Response(404, json={"error": "test not found"})
        if test["status"] not in _APEX_STARTABLE:
            # startableTestStatusError wording (pkg/api/runner.go).
            return httpx.Response(
                400,
                json={
                    "error": f"test is {test['status']}, must be QUEUED, PENDING, "
                    "RESERVED, SCHEDULED, or SHAKEOUT_REVIEW to start"
                },
            )
        test["status"] = "RUNNING"
        test["started_at"] = datetime.now(UTC).isoformat()
        test["live_metrics"] = {
            "active_vusers": 18,
            "tps": 42.5,
            "error_pct": 1.5,  # percent on the wire; adapters must yield 0.015
            "p95_ms": 320.0,
        }
        self.starts += 1
        return httpx.Response(200, json={"status": "started", "test_id": test_id})

    def abort_test(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        test_id = str(kwargs["test_id"])
        test = self.tests.get(test_id)
        if test is None:
            return httpx.Response(404, json={"error": "test not found"})
        if test["status"] in _APEX_TERMINAL:
            return httpx.Response(400, json={"error": "test backend does not support stop"})
        test["status"] = "ABORTED"
        test.pop("live_metrics", None)
        return httpx.Response(200, json={"status": "aborted", "test_id": test_id})

    def sla_status(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        test_id = str(kwargs["test_id"])
        test = self.tests.get(test_id)
        if test is None:
            return httpx.Response(404, json={"error": "test not found"})
        body: dict[str, Any] = {
            "test_id": test_id,
            "status": test["status"],
            "sla_breached": self.sla_breach,
        }
        if self.sla_breach:
            body["details"] = ["P95 latency breached threshold (p95_ms > 500)"]
        return httpx.Response(200, json=body)

    def archive_report(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        test = self.tests.get(str(kwargs["test_id"]))
        if test is None or test["status"] != "COMPLETE":
            return httpx.Response(404, json={"error": "archive not found"})
        return httpx.Response(
            200,
            json={
                "schema_version": "1",
                "overview": {
                    "transactions": 9000,
                    "errors": 90,
                    "error_pct": 1.0,
                    "peak_tps": 110.0,
                    "peak_active_vusers": test["total_vusers"],
                },
                "summary_timeline": [
                    {"tps": 80.0, "p95_ms": 420.0},
                    {"tps": 100.0, "p95_ms": 460.0},
                ],
                "by_action": {
                    "load_root": {
                        "transactions": 9000,
                        "errors": 90,
                        "error_pct": 1.0,
                        "p95_ms": 455.0,
                    }
                },
            },
        )


class ApexLoadHarness(EngineHarness):
    provider = "apex_load"

    def __enter__(self) -> "ApexLoadHarness":
        self.server = ApexLoadServer()
        self.router = respx.MockRouter(assert_all_called=False)
        server, route = self.server, self.router.route
        host = _APEX_LOAD_HOST
        tests_re = r"^/api/v1/tests/(?P<test_id>[^/]+)"
        route(method="POST", host=host, path="/api/v1/scripts/validate").mock(
            side_effect=server.validate_draft
        )
        route(method="POST", host=host, path="/api/v1/scripts").mock(
            side_effect=server.create_script
        )
        route(method="GET", host=host, path="/api/v1/tests").mock(side_effect=server.list_tests)
        route(method="POST", host=host, path="/api/v1/tests").mock(side_effect=server.create_test)
        route(method="POST", host=host, path__regex=tests_re + r"/start$").mock(
            side_effect=server.start_test
        )
        route(method="POST", host=host, path__regex=tests_re + r"/abort$").mock(
            side_effect=server.abort_test
        )
        route(method="GET", host=host, path__regex=tests_re + r"/sla-status$").mock(
            side_effect=server.sla_status
        )
        route(method="GET", host=host, path__regex=tests_re + r"/archive/report$").mock(
            side_effect=server.archive_report
        )
        route(method="GET", host=host, path__regex=tests_re + r"$").mock(
            side_effect=server.get_test
        )
        self.router.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.router.stop(quiet=exc_type is not None)

    def fresh_adapter(self) -> ExecutionEnginePort:
        conn = ConnectionConfig(
            id="conformance-apex-load",
            kind=PortKind.EXECUTION_ENGINE,
            provider="apex_load",
            name="Conformance APEX Load",
            options={"base_url": APEX_LOAD_BASE},
        )
        return ApexLoadExecutionEngine(conn, SecretValue(value=APEX_LOAD_API_KEY))

    def enable_sla_breach(self) -> None:
        self.server.sla_breach = True

    def advance(self) -> None:
        self.server.advance()

    def remote_create_count(self) -> int | None:
        return self.server.test_creates

    def remote_start_count(self) -> int | None:
        return self.server.starts


# ── loadrunner: in-memory LRE project behind respx ───────────────────────────

LRE_BASE = "https://lre.conformance.test"
_LRE_HOST = "lre.conformance.test"
LRE_PROJECT_PATH = "/LoadTest/rest/domains/CONF/projects/Apex"
LRE_TEST_ID = 88
LRE_SECRET = "conformance-svc:lre-password"
_LRE_EXPECTED_BASIC = "Basic " + base64.b64encode(LRE_SECRET.encode()).decode()


class LreServer:
    """Just enough of the LRE REST surface for one run lifecycle.

    State transitions per advance(): Initializing -> Running -> Finished,
    and Stopping -> Canceled for stopped runs. POST Runs both creates and
    starts a run (the LRE model), so run_creates doubles as the start count.
    """

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.run_creates = 0
        self.sla_breach = False
        self._next_run_id = 5000
        self._tokens: set[str] = set()

    # -- lifecycle clock ------------------------------------------------------

    def advance(self) -> None:
        for run in self.runs.values():
            state = run["RunState"]
            if state == "Initializing":
                run["RunState"] = "Running"
                run["Duration"] = 2  # minutes elapsed; spec is 240s -> 50%
            elif state == "Running":
                run["RunState"] = "Finished"
                run["Duration"] = 4
                run["RunSLAStatus"] = "Failed" if self.sla_breach else "Passed"
            elif state == "Stopping":
                run["RunState"] = "Canceled"

    # -- handlers (respx side effects) ----------------------------------------

    def authenticate(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if request.headers.get("Authorization") != _LRE_EXPECTED_BASIC:
            return httpx.Response(401)
        token = f"lwsso-{len(self._tokens) + 1}"
        self._tokens.add(token)
        return httpx.Response(200, headers={"Set-Cookie": f"LWSSO_COOKIE_KEY={token}; Path=/"})

    def _denied(self, request: httpx.Request) -> httpx.Response | None:
        cookie = request.headers.get("Cookie", "")
        if not any(f"LWSSO_COOKIE_KEY={token}" in cookie for token in self._tokens):
            return httpx.Response(401)
        return None

    def list_runs(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        return httpx.Response(200, json=list(self.runs.values()))

    def create_run(self, request: httpx.Request, **_: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        body = json.loads(request.content)
        run_id = self._next_run_id
        self._next_run_id += 1
        record: dict[str, Any] = {
            "ID": run_id,
            "TestID": body["TestID"],
            "TestInstanceID": 14,
            "PostRunAction": body.get("PostRunAction", ""),
            "TimeslotID": 7000 + run_id,
            "VudsMode": False,
            "RunState": "Initializing",
            "RunSLAStatus": "Not Completed",
            "Duration": 0,
            "RunComment": body.get("RunComment", ""),
        }
        self.runs[str(run_id)] = record
        self.run_creates += 1
        return httpx.Response(201, json=record)

    def get_run(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        run = self.runs.get(str(kwargs["run_id"]))
        if run is None:
            return httpx.Response(404)
        return httpx.Response(200, json=run)

    def stop_run(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        run = self.runs.get(str(kwargs["run_id"]))
        if run is None:
            return httpx.Response(404)
        run["RunState"] = "Stopping"
        return httpx.Response(200, json={})

    def list_results(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        run = self.runs.get(str(kwargs["run_id"]))
        if run is None:
            return httpx.Response(404)
        if run["RunState"] != "Finished":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {"ID": 9001, "Name": "Reports.zip", "Type": "HTML Report", "RunID": run["ID"]},
                {"ID": 9002, "Name": "RawResults.zip", "Type": "RAW Results", "RunID": run["ID"]},
            ],
        )

    def result_data(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if denied := self._denied(request):
            return denied
        run = self.runs.get(str(kwargs["run_id"]))
        if run is None or str(kwargs["result_id"]) not in {"9001", "9002"}:
            return httpx.Response(404)
        return httpx.Response(200, content=b"PK\x03\x04conformance-lre-report")


class LreHarness(EngineHarness):
    provider = "loadrunner"
    summary_reports_kpis = False
    kpi_limitation = (
        "loadrunner v1 reports no KPIs (documented limitation: tps_avg/p95_ms/"
        "error_rate/vusers_peak require parsing the LRE Analysis report); the "
        "degraded shape is empty kpis plus an honest note"
    )

    def __enter__(self) -> "LreHarness":
        self.server = LreServer()
        self.router = respx.MockRouter(assert_all_called=False)
        server, route = self.server, self.router.route
        host = _LRE_HOST
        runs_re = "^" + re.escape(LRE_PROJECT_PATH) + r"/Runs/(?P<run_id>\d+)"
        route(method="POST", host=host, path=AUTH_PATH).mock(side_effect=server.authenticate)
        route(method="GET", host=host, path=f"{LRE_PROJECT_PATH}/Runs").mock(
            side_effect=server.list_runs
        )
        route(method="POST", host=host, path=f"{LRE_PROJECT_PATH}/Runs").mock(
            side_effect=server.create_run
        )
        route(method="POST", host=host, path__regex=runs_re + r"/(?:stop|abort)$").mock(
            side_effect=server.stop_run
        )
        route(method="GET", host=host, path__regex=runs_re + r"/Results$").mock(
            side_effect=server.list_results
        )
        route(
            method="GET", host=host, path__regex=runs_re + r"/Results/(?P<result_id>\d+)/data$"
        ).mock(side_effect=server.result_data)
        route(method="GET", host=host, path__regex=runs_re + r"$").mock(side_effect=server.get_run)
        self.router.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.router.stop(quiet=exc_type is not None)

    def fresh_adapter(self) -> ExecutionEnginePort:
        conn = ConnectionConfig(
            id="conformance-lre",
            kind=PortKind.EXECUTION_ENGINE,
            provider="loadrunner",
            name="Conformance LRE",
            options={
                "base_url": LRE_BASE,
                "domain": "CONF",
                "project": "Apex",
                "test_id": LRE_TEST_ID,
            },
        )
        return LoadRunnerExecutionEngine(conn, SecretValue(value=LRE_SECRET))

    def enable_sla_breach(self) -> None:
        self.server.sla_breach = True

    def advance(self) -> None:
        self.server.advance()

    def remote_create_count(self) -> int | None:
        return self.server.run_creates

    def remote_start_count(self) -> int | None:
        # POST Runs creates AND starts an LRE run: creation count == start count.
        return self.server.run_creates


def make_harness(provider: str) -> EngineHarness:
    if provider == "sim":
        return SimHarness()
    if provider == "apex_load":
        return ApexLoadHarness()
    if provider == "loadrunner":
        return LreHarness()
    raise ValueError(f"no conformance harness for provider {provider!r}")
