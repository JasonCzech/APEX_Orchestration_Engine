"""LoadRunner Enterprise adapter against respx JSON wire fixtures (LRE REST).

Covers every adapter-contract requirement: LWSSO auth flow (cookie capture +
exactly-one 401 re-auth retry), provision/start get-or-create via the Runs-list
RunComment marker (including the fresh-instance process-restart case), the full
RunState mapping table, stop/abort idempotency, results listing + zip download
into a fake store, summary derivation, and error mapping.
"""

import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from apex.adapters.loadrunner.engine import (
    AUTH_PATH,
    RUN_STATE_PHASES,
    LoadRunnerExecutionEngine,
    timeslot_minutes,
)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.adapters.stubs import MemoryArtifactStore
from apex.domain.integrations import LoadTestSpec, SecretValue
from apex.domain.pipeline import EngineHandle
from apex.ports.execution_engine import EngineRunPhase

BASE = "https://lre.internal"
AUTH_URL = f"{BASE}{AUTH_PATH}"
PROJECT_BASE = f"{BASE}/LoadTest/rest/domains/DEFAULT/projects/Phoenix"
RUNS_QUERY = {"query": "{test-id[88]}"}
EXPECTED_BASIC = "Basic " + base64.b64encode(b"apex-svc:lre-password").decode()


@pytest.fixture(autouse=True)
def _clean_artifact_store() -> Iterator[None]:
    MemoryArtifactStore.clear()
    yield
    MemoryArtifactStore.clear()


def make_adapter(**option_overrides: Any) -> LoadRunnerExecutionEngine:
    options: dict[str, Any] = {"base_url": BASE, "domain": "DEFAULT", "project": "Phoenix"}
    options.update(option_overrides)
    conn = ConnectionConfig(
        id="lre-acme",
        kind=PortKind.EXECUTION_ENGINE,
        provider="loadrunner",
        name="Acme LRE",
        options=options,
    )
    return LoadRunnerExecutionEngine(conn, SecretValue(value="apex-svc:lre-password"))


def make_spec(key: str = "key-1", **overrides: Any) -> LoadTestSpec:
    fields: dict[str, Any] = {
        "idempotency_key": key,
        "title": "checkout soak",
        "script_refs": ["lre-test:88"],
        "vusers": 25,
        "ramp_s": 60,
        "duration_s": 240,
    }
    fields.update(overrides)
    return LoadTestSpec(**fields)


def provisioned_handle(**extra: str) -> EngineHandle:
    """Handle as provision() returns it before any LRE run exists."""
    extras = {"test_id": "88", "duration_s": "240", "timeslot_minutes": "30", "vusers": "25"}
    extras.update(extra)
    return EngineHandle(
        engine="loadrunner",
        connection_id="lre-acme",
        idempotency_key="key-1",
        extras=extras,
    )


def started_handle(run_id: str = "1042", **extra: str) -> EngineHandle:
    handle = provisioned_handle(run_id=run_id, **extra)
    handle.external_run_id = f"lre-{run_id}"
    return handle


def run_json(
    run_id: int = 1042,
    state: str = "Running",
    comment: str = "apex-orch:key-1",
    duration_min: int | None = 2,
    sla: str = "Not Completed",
) -> dict[str, Any]:
    return {
        "ID": run_id,
        "TestID": 88,
        "TestInstanceID": 14,
        "PostRunAction": "Collate And Analyze",
        "TimeslotID": 5001,
        "VudsMode": False,
        "RunState": state,
        "RunSLAStatus": sla,
        "Duration": duration_min,
        "RunComment": comment,
    }


def mock_authenticate(*tokens: str) -> respx.Route:
    """LWSSO authentication-point fixture: each call hands out the next token."""
    responses = [
        httpx.Response(200, headers={"Set-Cookie": f"LWSSO_COOKIE_KEY={token}; Path=/"})
        for token in (tokens or ("lwsso-token-1",))
    ]
    return respx.post(AUTH_URL).mock(side_effect=responses)


def assert_all_calls_authed() -> None:
    """Authenticate calls carry basic creds; every API call carries the LWSSO cookie."""
    assert respx.calls, "expected at least one mocked call"
    for call in respx.calls:
        request = call.request
        if str(request.url) == AUTH_URL:
            assert request.headers["Authorization"] == EXPECTED_BASIC
        else:
            assert "LWSSO_COOKIE_KEY=" in request.headers.get("Cookie", "")


# ── construction / registration ───────────────────────────────────────────────


def test_constructor_validates_options_and_secret() -> None:
    conn = ConnectionConfig(
        id="lre-bad", kind=PortKind.EXECUTION_ENGINE, provider="loadrunner", name="bad", options={}
    )
    secret = SecretValue(value="user:pw")
    with pytest.raises(ValueError, match="base_url"):
        LoadRunnerExecutionEngine(conn, secret)
    conn.options = {"base_url": BASE}
    with pytest.raises(ValueError, match="domain"):
        LoadRunnerExecutionEngine(conn, secret)
    conn.options = {"base_url": BASE, "domain": "DEFAULT"}
    with pytest.raises(ValueError, match="project"):
        LoadRunnerExecutionEngine(conn, secret)
    conn.options = {"base_url": BASE, "domain": "DEFAULT", "project": "Phoenix"}
    with pytest.raises(ValueError, match="secret_ref"):
        LoadRunnerExecutionEngine(conn, None)
    with pytest.raises(ValueError, match="user:password"):
        LoadRunnerExecutionEngine(conn, SecretValue(value="token-without-colon"))


def test_adapter_is_registered_for_loadrunner_provider() -> None:
    import apex.adapters  # noqa: F401  (side-effect imports register providers)

    assert "loadrunner" in AdapterRegistry.providers_for(PortKind.EXECUTION_ENGINE)


# ── validate (local; no network) ──────────────────────────────────────────────


async def test_validate_flags_bad_spec_and_missing_test_id() -> None:
    adapter = make_adapter()
    good = await adapter.validate(make_spec())
    assert good.ok and good.issues == []

    bad = await adapter.validate(LoadTestSpec(title="bad", vusers=0, duration_s=0, ramp_s=-1))
    assert not bad.ok
    assert len(bad.issues) == 4  # vusers, duration, ramp, no resolvable LRE test id
    assert any("test_id" in issue for issue in bad.issues)


async def test_validate_accepts_connection_level_test_id() -> None:
    adapter = make_adapter(test_id=77)
    report = await adapter.validate(make_spec(script_refs=[]))
    assert report.ok


async def test_validate_rejects_malformed_script_ref() -> None:
    report = await make_adapter().validate(make_spec(script_refs=["lre-test:abc"]))
    assert not report.ok
    assert any("numeric" in issue for issue in report.issues)


def test_timeslot_minutes_floors_at_lre_minimum() -> None:
    assert timeslot_minutes(make_spec()) == 30  # 5 min window + 15 headroom < LRE floor
    assert timeslot_minutes(make_spec(duration_s=3600, ramp_s=300)) == 80  # 65 + 15


# ── auth flow ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_lwsso_session_is_captured_once_and_reused() -> None:
    auth = mock_authenticate()
    run_route = respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json())
    )
    adapter = make_adapter()
    await adapter.get_status(started_handle())
    await adapter.get_status(started_handle())
    assert auth.call_count == 1  # one authenticate for both API calls
    assert run_route.call_count == 2
    assert_all_calls_authed()


@respx.mock
async def test_expired_session_reauths_once_and_retries() -> None:
    auth = mock_authenticate("token-1", "token-2")
    run_route = respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=run_json(state="Running")),
        ]
    )
    status = await make_adapter().get_status(started_handle())
    assert status.phase is EngineRunPhase.RUNNING
    assert auth.call_count == 2
    assert run_route.call_count == 2
    # the retry carried the renewed LWSSO session cookie
    assert "LWSSO_COOKIE_KEY=token-2" in run_route.calls[1].request.headers["Cookie"]
    assert_all_calls_authed()


@respx.mock
async def test_persistent_401_is_actionable_after_exactly_one_retry() -> None:
    auth = mock_authenticate("token-1", "token-2")
    run_route = respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(401))
    with pytest.raises(RuntimeError, match="user:password"):
        await make_adapter().get_status(started_handle())
    assert auth.call_count == 2  # initial + exactly one re-auth
    assert run_route.call_count == 2  # original + exactly one retry


@respx.mock
async def test_rejected_credentials_at_the_authentication_point() -> None:
    respx.post(AUTH_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(RuntimeError, match="user:password"):
        await make_adapter().get_status(started_handle())


# ── provision (get-or-create by idempotency key) ──────────────────────────────


@respx.mock
async def test_provision_reserves_without_creating_a_run() -> None:
    mock_authenticate()
    runs = respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[])
    )
    handle = await make_adapter().provision(make_spec())
    assert handle.engine == "loadrunner"
    assert handle.connection_id == "lre-acme"
    assert handle.idempotency_key == "key-1"
    assert handle.external_run_id is None  # LRE split: the run is created at start()
    assert handle.extras == {
        "test_id": "88",
        "duration_s": "240",
        "timeslot_minutes": "30",
        "vusers": "25",
    }
    assert runs.call_count == 1
    assert_all_calls_authed()


@respx.mock
async def test_provision_adopts_lowest_run_carrying_the_comment_marker() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(
            200,
            json=[
                run_json(run_id=2000, comment="apex-orch:key-1 (retriggered by ops)"),
                run_json(run_id=1042, comment="apex-orch:key-1"),
                run_json(run_id=3000, comment="apex-orch:other-key"),
                run_json(run_id=4000, comment=""),
            ],
        )
    )
    handle = await make_adapter().provision(make_spec())
    assert handle.external_run_id == "lre-1042"
    assert handle.extras["run_id"] == "1042"
    assert_all_calls_authed()


@respx.mock
async def test_provision_spec_script_ref_overrides_connection_test_id() -> None:
    mock_authenticate()
    runs = respx.get(f"{PROJECT_BASE}/Runs", params={"query": "{test-id[99]}"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    adapter = make_adapter(test_id=77)
    handle = await adapter.provision(make_spec(script_refs=["lre-test:99"]))
    assert handle.extras["test_id"] == "99"
    assert runs.call_count == 1


async def test_provision_without_any_test_id_is_actionable_value_error() -> None:
    with pytest.raises(ValueError, match=r"options\['test_id'\]"):
        await make_adapter().provision(make_spec(script_refs=[]))


# ── start (get-or-create; crash- and restart-safe) ────────────────────────────


@respx.mock
async def test_provision_start_get_or_create_survives_process_restart() -> None:
    mock_authenticate("token-1", "token-2")
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        side_effect=[
            httpx.Response(200, json=[]),  # provision: nothing yet
            httpx.Response(200, json=[]),  # start re-check: still nothing -> create
            httpx.Response(200, json=[run_json(state="Running")]),  # fresh provision: adopt
        ]
    )
    create = respx.post(f"{PROJECT_BASE}/Runs").mock(
        return_value=httpx.Response(201, json=run_json(state="Initializing"))
    )
    spec = make_spec()
    adapter = make_adapter()
    handle = await adapter.provision(spec)
    await adapter.start(handle)
    assert handle.external_run_id == "lre-1042"
    assert handle.extras["run_id"] == "1042"

    fresh = make_adapter()  # fresh instance: simulates a process restart, zero shared state
    again = await fresh.provision(spec)
    assert again.external_run_id == handle.external_run_id == "lre-1042"
    assert create.call_count == 1  # exactly one remote run was ever created
    body = json.loads(create.calls[0].request.content)
    assert body == {
        "TestID": 88,
        "TestInstanceID": -1,
        "PostRunAction": "Collate And Analyze",
        "TimeslotDuration": 30,
        "VudsMode": False,
        "RunComment": "apex-orch:key-1",
    }
    await fresh.start(again)  # starting an existing run stays a no-op
    assert create.call_count == 1
    assert_all_calls_authed()


@respx.mock
async def test_start_is_noop_when_run_already_exists() -> None:
    await make_adapter().start(started_handle())
    assert not respx.calls  # no HTTP at all — the handle already carries run_id


@respx.mock
async def test_start_adopts_run_created_before_a_crash() -> None:
    # Crash between POST Runs and the spine's checkpoint: the comment lookup
    # finds the run, so start() adopts instead of double-starting load.
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[run_json(state="Initializing")])
    )
    handle = provisioned_handle()
    await make_adapter().start(handle)
    assert handle.extras["run_id"] == "1042"
    assert handle.external_run_id == "lre-1042"
    assert_all_calls_authed()


# ── get_status (state mapping table) ──────────────────────────────────────────

STATE_TABLE = [
    ("Pending Creation", EngineRunPhase.PROVISIONING),
    ("Initializing", EngineRunPhase.PROVISIONING),
    ("Running", EngineRunPhase.RUNNING),
    ("Stopping", EngineRunPhase.STOPPING),
    ("Halting", EngineRunPhase.STOPPING),
    ("Before Collating Results", EngineRunPhase.COLLECTING),
    ("Collating Results", EngineRunPhase.COLLECTING),
    ("Before Creating Analysis Data", EngineRunPhase.COLLECTING),
    ("Creating Analysis Data", EngineRunPhase.COLLECTING),
    ("Finished", EngineRunPhase.COMPLETED),
    ("Failed Collating Results", EngineRunPhase.FAILED),
    ("Failed Creating Analysis Data", EngineRunPhase.FAILED),
    ("Run Failure", EngineRunPhase.FAILED),
    ("Canceled", EngineRunPhase.ABORTED),
    ("Cancelled", EngineRunPhase.ABORTED),
    ("Aborted", EngineRunPhase.ABORTED),
]


def test_state_table_matches_the_adapter_mapping_exactly() -> None:
    assert {state.lower() for state, _ in STATE_TABLE} == set(RUN_STATE_PHASES)
    for state, phase in STATE_TABLE:
        assert RUN_STATE_PHASES[state.lower()] is phase


@pytest.mark.parametrize(("lre_state", "expected_phase"), STATE_TABLE)
@respx.mock
async def test_get_status_maps_every_documented_lre_state(
    lre_state: str, expected_phase: EngineRunPhase
) -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state=lre_state))
    )
    status = await make_adapter().get_status(started_handle())
    assert status.phase is expected_phase
    assert status.live_stats is None  # documented v1 limitation: phase-only status
    assert status.message is not None and f"LRE run state: {lre_state}" in status.message
    assert_all_calls_authed()


@respx.mock
async def test_get_status_unknown_state_stays_nonterminal() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Mysterious New State"))
    )
    status = await make_adapter().get_status(started_handle())
    assert status.phase is EngineRunPhase.RUNNING
    assert status.message is not None and "unmapped" in status.message


@pytest.mark.parametrize(
    ("state", "duration_min", "expected_pct"),
    [
        ("Initializing", 0, 0.0),
        ("Running", 2, 50.0),  # 120 s elapsed of 240 s spec duration
        ("Running", 60, 95.0),  # capped until LRE goes terminal
        ("Running", None, 0.0),  # no Duration reported yet
        ("Stopping", 4, 95.0),
        ("Collating Results", 4, 99.0),
        ("Finished", 4, 100.0),
        ("Run Failure", 1, 100.0),
    ],
)
@respx.mock
async def test_get_status_progress_estimation(
    state: str, duration_min: int | None, expected_pct: float
) -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state=state, duration_min=duration_min))
    )
    status = await make_adapter().get_status(started_handle())
    assert status.progress_pct == expected_pct


@respx.mock
async def test_get_status_before_start_is_ready_without_network() -> None:
    status = await make_adapter().get_status(provisioned_handle())
    assert status.phase is EngineRunPhase.READY
    assert not respx.calls


@respx.mock
async def test_get_status_missing_run_raises_key_error() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(404))
    with pytest.raises(KeyError, match="1042"):
        await make_adapter().get_status(started_handle())


async def test_unprovisioned_handle_raises_value_error() -> None:
    adapter = make_adapter()
    bare = EngineHandle(engine="loadrunner")
    with pytest.raises(ValueError, match="provision"):
        await adapter.get_status(bare)
    with pytest.raises(ValueError, match="provision"):
        await adapter.start(bare)
    with pytest.raises(ValueError, match="provision"):
        await adapter.fetch_summary(bare)
    with pytest.raises(ValueError, match="start"):
        await adapter.collect_artifacts(provisioned_handle(), MemoryArtifactStore())


# ── abort / teardown ──────────────────────────────────────────────────────────


@respx.mock
async def test_abort_stops_a_running_run_gracefully() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Running"))
    )
    stop = respx.post(f"{PROJECT_BASE}/Runs/1042/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    await make_adapter().abort(started_handle(), reason="operator request")
    assert stop.call_count == 1
    assert_all_calls_authed()


@respx.mock
async def test_abort_uses_abortive_endpoint_when_extras_flag_it() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Running"))
    )
    hard_stop = respx.post(f"{PROJECT_BASE}/Runs/1042/abort").mock(
        return_value=httpx.Response(200, json={})
    )
    await make_adapter().abort(started_handle(abortive_stop="true"), reason="kill it")
    assert hard_stop.call_count == 1


@pytest.mark.parametrize("state", ["Finished", "Canceled", "Run Failure", "Stopping"])
@respx.mock
async def test_abort_is_idempotent_on_terminal_or_stopping_runs(state: str) -> None:
    mock_authenticate()
    run_route = respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state=state))
    )
    await make_adapter().abort(started_handle(), reason="late abort")  # no stop POST mocked
    assert run_route.call_count == 1


@respx.mock
async def test_abort_tolerates_already_gone_runs() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(404))
    await make_adapter().abort(started_handle(), reason="too late")  # no raise


@respx.mock
async def test_abort_tolerates_run_vanishing_between_check_and_stop() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Running"))
    )
    respx.post(f"{PROJECT_BASE}/Runs/1042/stop").mock(return_value=httpx.Response(404))
    await make_adapter().abort(started_handle(), reason="race")  # no raise


@respx.mock
async def test_abort_before_any_run_exists_is_a_noop() -> None:
    await make_adapter().abort(provisioned_handle(), reason="never started")
    assert not respx.calls


async def test_teardown_never_raises_and_makes_no_calls() -> None:
    # No respx activation: any network attempt would error the test.
    await make_adapter().teardown(started_handle())
    await make_adapter().teardown(EngineHandle(engine="loadrunner"))


# ── collect_artifacts ─────────────────────────────────────────────────────────

RESULTS = [
    {"ID": 2001, "Name": "Reports.zip", "Type": "HTML Report", "RunID": 1042},
    {"ID": 2002, "Name": "AnalyzedResult.zip", "Type": "Analyzed Result", "RunID": 1042},
    {"ID": 2003, "Name": "RawResults.zip", "Type": "RAW Results", "RunID": 1042},
    {"ID": 2004, "Name": "output.mdb.zip", "Type": "Output Log", "RunID": 1042},
]


@respx.mock
async def test_collect_artifacts_streams_report_zips_into_the_store() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=RESULTS)
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04html-report-bytes")
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2002/data").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04analyzed-bytes")
    )
    store = MemoryArtifactStore()
    refs = await make_adapter().collect_artifacts(started_handle(), store)

    assert [ref["name"] for ref in refs] == ["Reports.zip", "AnalyzedResult.zip"]
    for ref in refs:
        assert ref["kind"] == "engine_report"
        assert ref["media_type"] == "application/zip"
        assert ref["id"] and ref["created_at"]  # ArtifactRef-shaped dicts
    assert refs[0]["uri"] == "memory://engine-runs/lre-1042/Reports.zip"
    assert refs[0]["summary"] == "LRE HTML Report for run 1042"
    assert await store.get("engine-runs/lre-1042/Reports.zip") == b"PK\x03\x04html-report-bytes"
    assert await store.get("engine-runs/lre-1042/AnalyzedResult.zip") == b"PK\x03\x04analyzed-bytes"
    assert_all_calls_authed()


@respx.mock
async def test_collect_artifacts_falls_back_to_raw_results() -> None:
    # Collation failed -> no report-typed results; raw data is still preserved.
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[RESULTS[2]])
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2003/data").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04raw")
    )
    refs = await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())
    assert [ref["name"] for ref in refs] == ["RawResults.zip"]


# ── fetch_summary ─────────────────────────────────────────────────────────────


@respx.mock
async def test_fetch_summary_finished_run_passes_with_honest_notes() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Finished", sla="Passed"))
    )
    summary = await make_adapter().fetch_summary(started_handle())
    assert summary.engine == "loadrunner"
    assert summary.passed is True
    assert summary.kpis == {}  # documented v1 limitation: needs Analysis report parsing
    assert summary.sla_breaches == []
    assert summary.notes is not None and "Analysis" in summary.notes
    assert_all_calls_authed()


@respx.mock
async def test_fetch_summary_sla_failure_fails_the_run() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Finished", sla="Failed"))
    )
    summary = await make_adapter().fetch_summary(started_handle())
    assert summary.passed is False
    assert len(summary.sla_breaches) == 1 and "SLA" in summary.sla_breaches[0]


@pytest.mark.parametrize("state", ["Run Failure", "Canceled", "Failed Collating Results"])
@respx.mock
async def test_fetch_summary_non_finished_states_do_not_pass(state: str) -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state=state))
    )
    summary = await make_adapter().fetch_summary(started_handle())
    assert summary.passed is False
    assert summary.notes is not None and state in summary.notes


# ── error mapping ─────────────────────────────────────────────────────────────


@respx.mock
async def test_bad_request_maps_to_value_error_with_lre_message() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{PROJECT_BASE}/Runs").mock(
        return_value=httpx.Response(
            400, json={"ExceptionMessage": "Timeslot not available", "ErrorCode": 1500}
        )
    )
    with pytest.raises(ValueError, match="Timeslot not available"):
        await make_adapter().start(provisioned_handle())


@respx.mock
async def test_forbidden_maps_to_actionable_runtime_error() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(403))
    with pytest.raises(RuntimeError, match="user:password"):
        await make_adapter().get_status(started_handle())


@respx.mock
async def test_server_error_maps_to_runtime_error() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(500, json={"ExceptionMessage": "LRE internal error"})
    )
    with pytest.raises(RuntimeError, match="HTTP 500.*LRE internal error"):
        await make_adapter().get_status(started_handle())


@respx.mock
async def test_transport_error_maps_to_runtime_error() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="before a response arrived"):
        await make_adapter().get_status(started_handle())
