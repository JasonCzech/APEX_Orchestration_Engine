"""LoadRunner Enterprise adapter against respx JSON wire fixtures (LRE REST).

Covers every adapter-contract requirement: LWSSO auth flow (cookie capture +
exactly-one 401 re-auth retry), provision/start get-or-create via the Runs-list
RunComment marker (including the fresh-instance process-restart case), the full
RunState mapping table, stop/abort idempotency, results listing + zip download
into a fake store, summary derivation, and error mapping.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

import apex.adapters.loadrunner.engine as loadrunner_engine
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
from apex.ports.artifact_store import engine_artifact_key
from apex.ports.execution_engine import EngineProviderRunNotFoundError, EngineRunPhase

BASE = "https://lre.internal"
AUTH_URL = f"{BASE}{AUTH_PATH}"
PROJECT_BASE = f"{BASE}/LoadTest/rest/domains/DEFAULT/projects/Phoenix"
RUNS_QUERY = {
    "query": "{test-id[88]}",
    "page-size": loadrunner_engine._RUN_LIST_PAGE_SIZE,
    "start-index": 1,
}
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


def mock_owned_run() -> respx.Route:
    return respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json())
    )


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
    with pytest.raises(ValueError, match="max_report_bytes"):
        make_adapter(max_report_bytes=loadrunner_engine._HARD_MAX_REPORT_BYTES + 1)


@pytest.mark.parametrize(
    ("option", "value", "match"),
    [
        ("base_url", True, "base_url"),
        ("domain", "../../other", "domain"),
        ("project", True, "project"),
        ("test_id", True, "test_id"),
        ("test_id", 0, "test_id"),
        ("test_instance_id", -2, "test_instance_id"),
        ("max_report_bytes", True, "integer"),
        ("max_report_bytes", "1024", "integer"),
    ],
)
def test_constructor_rejects_coercible_or_unsafe_options(
    option: str, value: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        make_adapter(**{option: value})


def test_adapter_is_registered_for_loadrunner_provider() -> None:
    import apex.adapters  # noqa: F401  (side-effect imports register providers)

    assert "loadrunner" in AdapterRegistry.providers_for(PortKind.EXECUTION_ENGINE)


# ── validate (local; no network) ──────────────────────────────────────────────


async def test_validate_flags_bad_spec_and_missing_test_id() -> None:
    adapter = make_adapter()
    good = await adapter.validate(make_spec())
    assert good.ok and good.issues == []

    # Public construction rejects these values; bypass validation to retain the
    # adapter's defense-in-depth checks for old/checkpointed model instances.
    bad_spec = LoadTestSpec(title="bad").model_copy(
        update={"vusers": 0, "duration_s": 0, "ramp_s": -1}
    )
    bad = await adapter.validate(bad_spec)
    assert not bad.ok
    assert bad.issues == ["load test specification failed structural validation"]


async def test_validate_accepts_connection_level_test_id() -> None:
    adapter = make_adapter(test_id=77)
    report = await adapter.validate(make_spec(script_refs=[]))
    assert report.ok


async def test_validate_rejects_malformed_script_ref() -> None:
    report = await make_adapter().validate(make_spec(script_refs=["lre-test:abc"]))
    assert not report.ok
    assert any("bounded positive" in issue for issue in report.issues)


async def test_provision_revalidates_model_copy_before_provider_io() -> None:
    spec = make_spec().model_copy(update={"duration_s": float("nan")})

    with pytest.raises(ValueError, match="structural validation"):
        await make_adapter().provision(spec)


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
async def test_provision_rejects_duplicate_exact_idempotency_markers() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(
            200,
            json=[
                run_json(run_id=1042, comment="apex-orch:key-1"),
                run_json(run_id=1043, comment="apex-orch:key-1"),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="attached to multiple runs"):
        await make_adapter().provision(make_spec())


@respx.mock
async def test_provision_finds_idempotency_marker_after_first_runs_page() -> None:
    mock_authenticate()
    first_page = [
        run_json(run_id=2_000 + index, comment=f"apex-orch:other-{index}")
        for index in range(loadrunner_engine._RUN_LIST_PAGE_SIZE)
    ]
    first = respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(
            200,
            json={"Runs": first_page, "TotalResults": len(first_page) + 1},
        )
    )
    second_query = {**RUNS_QUERY, "start-index": len(first_page) + 1}
    second = respx.get(f"{PROJECT_BASE}/Runs", params=second_query).mock(
        return_value=httpx.Response(
            200,
            json={"Runs": [run_json(run_id=1042)], "TotalResults": len(first_page) + 1},
        )
    )

    handle = await make_adapter().provision(make_spec())

    assert handle.external_run_id == "lre-1042"
    assert handle.extras["run_id"] == "1042"
    assert first.call_count == second.call_count == 1


@respx.mock
async def test_provision_fails_closed_when_runs_collection_exceeds_scan_budget() -> None:
    mock_authenticate()
    route = respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(
            200,
            json={
                "Runs": [],
                "TotalResults": loadrunner_engine._MAX_RUN_RECONCILIATION_ROWS + 1,
            },
        )
    )

    with pytest.raises(RuntimeError, match="scan budget"):
        await make_adapter().provision(make_spec())

    assert route.call_count == 1


@respx.mock
async def test_provision_rejects_falsey_non_list_runs_collection() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json={"Runs": {}})
    )

    with pytest.raises(RuntimeError, match="field 'Runs' must be a list"):
        await make_adapter().provision(make_spec())


@pytest.mark.parametrize(
    "payload",
    [
        {"Runs": [], "TotalResults": True},
        {"Runs": [], "TotalResults": "0"},
        {"Runs": [{"RunComment": {"value": "apex-orch:key-1"}}]},
        {"Runs": [{"RunComment": "apex-orch:key-1", "ID": True}]},
    ],
)
@respx.mock
async def test_provision_rejects_malformed_reconciliation_scalars(
    payload: dict[str, Any],
) -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(RuntimeError):
        await make_adapter().provision(make_spec())


@respx.mock
async def test_provision_spec_script_ref_overrides_connection_test_id() -> None:
    mock_authenticate()
    runs = respx.get(
        f"{PROJECT_BASE}/Runs",
        params={**RUNS_QUERY, "query": "{test-id[99]}"},
    ).mock(return_value=httpx.Response(200, json=[]))
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


async def test_concurrent_start_calls_create_one_remote_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh adapters serialize the last lookup and costly POST Runs call."""

    remote_run: dict[str, Any] | None = None
    create_count = 0

    async def find_run(
        _adapter: LoadRunnerExecutionEngine, _test_id: int, _key: str
    ) -> dict[str, Any] | None:
        # Snapshot before yielding so this reproduces the old check/create race.
        snapshot = remote_run
        await asyncio.sleep(0.02)
        return snapshot

    async def request(
        _adapter: LoadRunnerExecutionEngine,
        method: str,
        path: str,
        **_kwargs: Any,
    ) -> httpx.Response:
        nonlocal create_count, remote_run
        assert method == "POST"
        assert path == "/LoadTest/rest/domains/DEFAULT/projects/Phoenix/Runs"
        create_count += 1
        await asyncio.sleep(0.01)
        remote_run = run_json(state="Initializing")
        return httpx.Response(201, json=remote_run)

    monkeypatch.setattr(LoadRunnerExecutionEngine, "_find_run_by_comment", find_run)
    monkeypatch.setattr(LoadRunnerExecutionEngine, "_request", request)
    first = provisioned_handle()
    second = provisioned_handle()

    await asyncio.gather(make_adapter().start(first), make_adapter().start(second))

    assert create_count == 1
    assert first.external_run_id == second.external_run_id == "lre-1042"


async def test_start_adopts_run_when_create_response_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed POST followed by a transport failure is reconciled by marker."""

    remote_run: dict[str, Any] | None = None
    create_count = 0

    async def find_run(
        _adapter: LoadRunnerExecutionEngine, _test_id: int, _key: str
    ) -> dict[str, Any] | None:
        return remote_run

    async def request(
        _adapter: LoadRunnerExecutionEngine,
        method: str,
        path: str,
        **_kwargs: Any,
    ) -> httpx.Response:
        nonlocal create_count, remote_run
        assert method == "POST"
        assert path == "/LoadTest/rest/domains/DEFAULT/projects/Phoenix/Runs"
        create_count += 1
        remote_run = run_json(state="Initializing")
        raise RuntimeError("response lost after remote commit")

    monkeypatch.setattr(LoadRunnerExecutionEngine, "_find_run_by_comment", find_run)
    monkeypatch.setattr(LoadRunnerExecutionEngine, "_request", request)
    handle = provisioned_handle()

    await make_adapter().start(handle)

    assert create_count == 1
    assert handle.extras["run_id"] == "1042"
    assert handle.external_run_id == "lre-1042"


@pytest.mark.parametrize("mismatch", ["test_id", "marker"])
@respx.mock
async def test_start_reconciles_wrong_target_create_acknowledgement(mismatch: str) -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[run_json(state="Initializing")]),
        ]
    )
    wrong_run = run_json(run_id=999, state="Initializing")
    if mismatch == "test_id":
        wrong_run["TestID"] = 99
    else:
        wrong_run["RunComment"] = "apex-orch:another-run"
    create = respx.post(f"{PROJECT_BASE}/Runs").mock(
        return_value=httpx.Response(201, json=wrong_run)
    )
    handle = provisioned_handle()

    await make_adapter().start(handle)

    assert create.call_count == 1
    assert handle.extras["run_id"] == "1042"
    assert handle.external_run_id == "lre-1042"


@respx.mock
async def test_start_rejects_marker_match_from_wrong_test() -> None:
    mock_authenticate()
    wrong_run = run_json(state="Initializing")
    wrong_run["TestID"] = 99
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[wrong_run])
    )
    create = respx.post(f"{PROJECT_BASE}/Runs").mock(
        return_value=httpx.Response(201, json=run_json(state="Initializing"))
    )

    with pytest.raises(RuntimeError, match="unexpected test"):
        await make_adapter().start(provisioned_handle())

    assert not create.called


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


@pytest.mark.parametrize("mismatch", ["run_id", "test_id", "marker"])
@respx.mock
async def test_get_status_rejects_wrong_target_run_response(mismatch: str) -> None:
    mock_authenticate()
    payload = run_json(state="Running")
    if mismatch == "run_id":
        payload["ID"] = 999
    elif mismatch == "test_id":
        payload["TestID"] = 99
    else:
        payload["RunComment"] = "apex-orch:another-run"
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(RuntimeError, match="unexpected run ID|unexpected test|idempotency marker"):
        await make_adapter().get_status(started_handle())


@respx.mock
async def test_get_status_unknown_state_stays_nonterminal() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Mysterious New State"))
    )
    status = await make_adapter().get_status(started_handle())
    assert status.phase is EngineRunPhase.RUNNING
    assert status.message is not None and "unmapped" in status.message


@pytest.mark.parametrize("state", [True, 7, {"value": "Running"}, "x" * 256])
@respx.mock
async def test_get_status_rejects_malformed_run_state(state: Any) -> None:
    mock_authenticate()
    payload = run_json()
    payload["RunState"] = state
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(RuntimeError, match="RunState"):
        await make_adapter().get_status(started_handle())


@respx.mock
async def test_get_status_rejects_provider_token_state_before_public_message() -> None:
    canary = "ghp_" + ("D" * 24)
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state=canary))
    )

    with pytest.raises(RuntimeError, match="unsafe material") as raised:
        await make_adapter().get_status(started_handle())

    assert canary not in str(raised.value)


@pytest.mark.parametrize("duration", [True, -1, "2", 1_000_000_001])
@respx.mock
async def test_get_status_rejects_malformed_provider_duration(duration: Any) -> None:
    mock_authenticate()
    payload = run_json(state="Running")
    payload["Duration"] = duration
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(RuntimeError, match="Duration"):
        await make_adapter().get_status(started_handle())


@respx.mock
async def test_get_status_invalid_handle_duration_does_not_retain_raw_value() -> None:
    canary = "bare-handle-duration-secret-canary"
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Running", duration_min=1))
    )
    handle = started_handle()
    handle.extras["duration_s"] = canary

    with pytest.raises(ValueError, match="finite non-negative number") as excinfo:
        await make_adapter().get_status(handle)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert canary not in str(excinfo.value)


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
async def test_get_status_missing_run_raises_definitive_provider_not_found() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(404))
    with pytest.raises(EngineProviderRunNotFoundError, match="1042"):
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


@pytest.mark.parametrize("run_id", ["../../other", "0", "-1", "9" * 20])
async def test_corrupt_persisted_run_id_is_rejected_before_network_io(run_id: str) -> None:
    with pytest.raises(ValueError, match="invalid run_id"):
        await make_adapter().get_status(started_handle(run_id))


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
async def test_abort_logs_only_non_content_reason_metadata() -> None:
    secret_reason = "opaque-api-key-value-that-redaction-cannot-recognize"
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(
        return_value=httpx.Response(200, json=run_json(state="Running"))
    )
    respx.post(f"{PROJECT_BASE}/Runs/1042/stop").mock(return_value=httpx.Response(200, json={}))

    with capture_logs() as logs:
        await make_adapter().abort(started_handle(), reason=secret_reason)

    assert secret_reason not in repr(logs)
    event = next(log for log in logs if log.get("event") == "loadrunner.abort")
    assert event["reason_present"] is True
    assert event["reason_length"] == len(secret_reason)


@respx.mock
async def test_abort_rejects_wrong_target_precheck_without_stopping() -> None:
    mock_authenticate()
    payload = run_json(state="Running")
    payload["RunComment"] = "apex-orch:another-run"
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=payload))
    stop = respx.post(f"{PROJECT_BASE}/Runs/1042/stop").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(RuntimeError, match="idempotency marker"):
        await make_adapter().abort(started_handle(), reason="operator request")

    assert not stop.called


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
    mock_authenticate()
    lookup = respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[])
    )
    await make_adapter().abort(provisioned_handle(), reason="never started")
    assert lookup.call_count == 1


@respx.mock
async def test_abort_recovers_ambiguous_started_run_before_stopping_it() -> None:
    mock_authenticate()
    respx.get(f"{PROJECT_BASE}/Runs", params=RUNS_QUERY).mock(
        return_value=httpx.Response(200, json=[run_json(state="Running")])
    )
    stop = respx.post(f"{PROJECT_BASE}/Runs/1042/stop").mock(
        return_value=httpx.Response(200, json={})
    )
    handle = provisioned_handle()

    await make_adapter().abort(handle, reason="start response was ambiguous")

    assert handle.extras["run_id"] == "1042"
    assert handle.external_run_id == "lre-1042"
    assert stop.call_count == 1


async def test_teardown_never_raises_and_makes_no_calls() -> None:
    # No respx activation: any network attempt would error the test.
    await make_adapter().teardown(started_handle())
    await make_adapter().teardown(EngineHandle(engine="loadrunner"))


async def test_teardown_does_not_log_untrusted_handle_id() -> None:
    canary = "opaque-loadrunner-handle-canary-46dc"
    handle = EngineHandle(engine="loadrunner", external_run_id=canary)

    with capture_logs() as logs:
        await make_adapter().teardown(handle)

    assert canary not in repr(logs)


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
    mock_owned_run()
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
    reports_key = engine_artifact_key("key-1", "0000-result-2001-Reports.zip")
    analyzed_key = engine_artifact_key("key-1", "0001-result-2002-AnalyzedResult.zip")
    assert refs[0]["uri"] == f"memory://{reports_key}"
    assert refs[0]["key"] == reports_key
    assert refs[0]["summary"] == "LRE HTML Report for run 1042"
    assert await store.get(reports_key) == b"PK\x03\x04html-report-bytes"
    assert await store.get(analyzed_key) == b"PK\x03\x04analyzed-bytes"
    assert_all_calls_authed()


@respx.mock
async def test_collect_artifacts_retries_transient_stream_status() -> None:
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[RESULTS[0]])
    )
    route = respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        side_effect=[
            httpx.Response(503, json={"Message": "busy"}),
            httpx.Response(200, content=b"PK\x03\x04retried"),
        ]
    )

    store = MemoryArtifactStore()
    refs = await make_adapter().collect_artifacts(started_handle(), store)

    assert route.call_count == 2
    assert [ref["name"] for ref in refs] == ["Reports.zip"]
    assert (
        await store.get(engine_artifact_key("key-1", "0000-result-2001-Reports.zip"))
        == b"PK\x03\x04retried"
    )


@respx.mock
async def test_collect_artifacts_keeps_duplicate_result_names_distinct() -> None:
    mock_authenticate()
    mock_owned_run()
    duplicates = [
        {"ID": 2001, "Name": "Report.zip", "Type": "HTML Report", "RunID": 1042},
        {"ID": 2002, "Name": "Report.zip", "Type": "HTML Report", "RunID": 1042},
    ]
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=duplicates)
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"first")
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2002/data").mock(
        return_value=httpx.Response(200, content=b"second")
    )

    store = MemoryArtifactStore()
    refs = await make_adapter().collect_artifacts(started_handle(), store)

    first_key = engine_artifact_key("key-1", "0000-result-2001-Report.zip")
    second_key = engine_artifact_key("key-1", "0001-result-2002-Report.zip")
    assert [ref["name"] for ref in refs] == ["Report.zip", "Report.zip"]
    assert [ref["key"] for ref in refs] == [first_key, second_key]
    assert first_key != second_key
    assert await store.get(first_key) == b"first"
    assert await store.get(second_key) == b"second"


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

    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[RESULTS[0]])
    )
    stream = CountingStream()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(418, stream=stream)
    )

    with pytest.raises(RuntimeError, match="HTTP 418"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())

    assert 0 < stream.yielded < 100
    assert stream.closed is True


@respx.mock
async def test_collect_artifacts_enforces_configured_stream_limit() -> None:
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[RESULTS[0]])
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"too-large")
    )

    with pytest.raises(ValueError, match="maximum size of 4 bytes"):
        await make_adapter(max_report_bytes=4).collect_artifacts(
            started_handle(), MemoryArtifactStore()
        )


@respx.mock
async def test_collect_artifacts_falls_back_to_raw_results() -> None:
    # Collation failed -> no report-typed results; raw data is still preserved.
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[RESULTS[2]])
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2003/data").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04raw")
    )
    refs = await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())
    assert [ref["name"] for ref in refs] == ["RawResults.zip"]


@respx.mock
async def test_collect_artifacts_rejects_provider_result_count_budget() -> None:
    mock_authenticate()
    mock_owned_run()
    results = [
        {"ID": 3000 + index, "Name": f"report-{index}.zip", "Type": "HTML Report"}
        for index in range(loadrunner_engine._MAX_RESULT_ARTIFACTS + 1)
    ]
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=results)
    )

    with pytest.raises(RuntimeError, match="artifacts; limit"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", 123),
        ("Name", "bad\x00name.zip"),
        ("Name", "bad\nname.zip"),
        ("Name", "x" * 513),
        ("Type", {"kind": "HTML Report"}),
        ("Type", "HTML\rReport"),
        ("Type", "x" * 257),
    ],
)
@respx.mock
async def test_collect_artifacts_rejects_unsafe_provider_text_before_download_or_store(
    field: str, value: object
) -> None:
    mock_authenticate()
    mock_owned_run()
    result: dict[str, object] = {
        "ID": 2001,
        "Name": "Report.zip",
        "Type": "HTML Report",
        "RunID": 1042,
    }
    result[field] = value
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[result])
    )
    download = respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"must-not-be-read")
    )

    with pytest.raises(RuntimeError, match=field):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())

    assert download.call_count == 0
    assert MemoryArtifactStore._objects == {}


@respx.mock
async def test_collect_artifacts_rejects_provider_token_metadata_before_durable_output() -> None:
    canary = "ghp_" + ("E" * 24)
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ID": 2001,
                    "Name": f"{canary}.zip",
                    "Type": "HTML Report",
                    "RunID": 1042,
                }
            ],
        )
    )
    download = respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"must-not-be-read")
    )

    with pytest.raises(RuntimeError, match="unsafe material") as raised:
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())

    assert canary not in str(raised.value)
    assert download.call_count == 0
    assert MemoryArtifactStore._objects == {}


@pytest.mark.parametrize("payload", [{}, {"Results": None}, {"Results": {}}])
@respx.mock
async def test_collect_artifacts_requires_explicit_results_list(payload: object) -> None:
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=payload)
    )

    with pytest.raises(RuntimeError, match="Results list"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())


@respx.mock
async def test_collection_failure_never_deletes_successful_deterministic_object() -> None:
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=RESULTS[:2])
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"first-committed")
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2002/data").mock(return_value=httpx.Response(404))
    store = MemoryArtifactStore()

    with pytest.raises(KeyError):
        await make_adapter().collect_artifacts(started_handle(), store)

    first_key = engine_artifact_key("key-1", "0000-result-2001-Reports.zip")
    assert await store.get(first_key) == b"first-committed"


@respx.mock
async def test_collect_artifacts_enforces_aggregate_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loadrunner_engine, "_MAX_TOTAL_ARTIFACT_BYTES", 4)
    mock_authenticate()
    mock_owned_run()
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=RESULTS[:2])
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"abc")
    )
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2002/data").mock(
        return_value=httpx.Response(200, content=b"de")
    )

    with pytest.raises(ValueError, match="maximum size of 1 bytes"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())


@respx.mock
async def test_collect_artifacts_rejects_wrong_target_before_results_download() -> None:
    mock_authenticate()
    wrong = run_json()
    wrong["TestID"] = 99
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=wrong))
    results = respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=RESULTS)
    )

    with pytest.raises(RuntimeError, match="unexpected test"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())

    assert not results.called


@respx.mock
async def test_collect_artifacts_rejects_result_from_wrong_run_before_download() -> None:
    mock_authenticate()
    mock_owned_run()
    wrong_result = {**RESULTS[0], "RunID": 999}
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=[wrong_result])
    )
    download = respx.get(f"{PROJECT_BASE}/Runs/1042/Results/2001/data").mock(
        return_value=httpx.Response(200, content=b"must-not-be-read")
    )

    with pytest.raises(RuntimeError, match="unexpected run"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())

    assert not download.called


@respx.mock
async def test_collect_artifacts_rejects_duplicate_result_ids() -> None:
    mock_authenticate()
    mock_owned_run()
    duplicate = [{**RESULTS[0]}, {**RESULTS[0], "Name": "Other.zip"}]
    respx.get(f"{PROJECT_BASE}/Runs/1042/Results").mock(
        return_value=httpx.Response(200, json=duplicate)
    )

    with pytest.raises(RuntimeError, match="duplicate result ID"):
        await make_adapter().collect_artifacts(started_handle(), MemoryArtifactStore())


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


@pytest.mark.parametrize("sla", [True, 7, {"value": "Failed"}, "x" * 256])
@respx.mock
async def test_fetch_summary_rejects_malformed_sla_status(sla: Any) -> None:
    mock_authenticate()
    payload = run_json(state="Finished")
    payload["RunSLAStatus"] = sla
    respx.get(f"{PROJECT_BASE}/Runs/1042").mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(RuntimeError, match="RunSLAStatus"):
        await make_adapter().fetch_summary(started_handle())


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
