"""M3 flagship: a pipeline run survives a SIGKILL mid-poll and resumes durably.

Subprocess A compiles the pipeline with a Postgres checkpointer (durability="sync")
and starts an execution-only run against the sim engine (~6s simulated load,
0.3s poll interval). The parent SIGKILLs it mid-poll, then subprocess B re-invokes
the same thread with input None: LangGraph resumes from the last committed
checkpoint and the run completes. Core assertion: exactly ONE external_run_id
across both processes — the write-ahead idempotency key in graph state plus the
engine's get-or-create provision contract make the restart unable to double-start
load. Poll-count continuity is asserted best-effort.

Opt-in via APEX_TEST_DATABASE_URI (any SQLAlchemy/psycopg Postgres URI; the
checkpointer creates its own tables on .setup()). Deterministic, ~15s wall time.
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"),
    reason="needs postgres (set APEX_TEST_DATABASE_URI)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DURATION_S = 6.0
POLL_INTERVAL_S = 0.3
KILL_AFTER_POLLS = 2  # SIGKILL once the run is provably mid-poll-loop
CHILD_DEADLINE_S = 60.0

# Runs in a child interpreter via `python -c`. Parametrized through APEX_RESTART_*
# env vars; prints one JSON object per line ("kind": engine_poll | final) so the
# parent can assert across the kill boundary. stdout noise (structlog) is ignored
# by the parent's line parser.
CHILD_SCRIPT = r"""
import json
import os
import sys

from langgraph.checkpoint.postgres import PostgresSaver

from apex.domain.pipeline import Phase, PhaseResult, PhaseStatus
from apex.graphs.pipeline import execution_phase
from apex.graphs.pipeline.graph import builder
from apex.services.connections import ConnectionResolver

# Hermetic adapters: static DEV_CONNECTIONS (sim engine + in-memory artifact
# store) — the test exercises checkpoint durability, not connection resolution.
execution_phase._make_resolver = lambda: ConnectionResolver()

uri = os.environ["APEX_RESTART_DB_URI"]
thread_id = os.environ["APEX_RESTART_THREAD_ID"]
mode = os.environ["APEX_RESTART_MODE"]  # "run" (fresh) | "resume" (input None)
duration_s = float(os.environ["APEX_RESTART_DURATION_S"])
poll_interval_s = float(os.environ["APEX_RESTART_POLL_INTERVAL_S"])

config = {
    "configurable": {
        "thread_id": thread_id,
        "phases": ["execution"],
        "gates": {"execution": {"prompt_review": "auto", "output_review": "auto"}},
        "limits": {"poll_interval_s": poll_interval_s, "poll_timeout_s": 120.0},
    },
    # Sizing rule: poll_timeout/poll_interval + spine + headroom (execution_phase).
    "recursion_limit": 600,
}

if mode == "run":
    seeded = PhaseResult(
        phase=Phase.SCRIPT_SCENARIO, status=PhaseStatus.SUCCEEDED, attempt=1
    ).as_state()
    seeded["load_test_spec"] = {
        "title": "restart survival load test",
        "vusers": 5,
        "ramp_s": 0.5,
        "duration_s": duration_s,
    }
    inputs = {
        "title": "restart survival",
        "request": "survive a SIGKILL mid-poll",
        "phase_results": {"script_scenario": seeded},
    }
else:
    inputs = None  # resume from the latest committed checkpoint

with PostgresSaver.from_conn_string(uri) as saver:
    saver.setup()  # idempotent; creates the checkpointer's own tables
    graph = builder.compile(checkpointer=saver)
    for _ns, event in graph.stream(
        inputs, config, stream_mode="custom", subgraphs=True, durability="sync"
    ):
        if isinstance(event, dict) and event.get("type") == "engine_poll":
            print(
                json.dumps(
                    {
                        "kind": "engine_poll",
                        "external_run_id": event.get("external_run_id"),
                        "status": event.get("status"),
                    }
                ),
                flush=True,
            )
    state = graph.get_state(config)
    entry = state.values["phase_results"]["execution"]
    handle = state.values.get("engine_handle") or {}
    print(
        json.dumps(
            {
                "kind": "final",
                "status": entry.get("status"),
                "external_run_id": handle.get("external_run_id"),
                "idempotency_key": handle.get("idempotency_key"),
                "poll_count": entry.get("engine_poll_count"),
                "test_summary_passed": (entry.get("test_summary") or {}).get("passed"),
            }
        ),
        flush=True,
    )
"""


def _child_env(thread_id: str, mode: str, db_uri: str) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "APEX_RESTART_DB_URI": db_uri,
        "APEX_RESTART_THREAD_ID": thread_id,
        "APEX_RESTART_MODE": mode,
        "APEX_RESTART_DURATION_S": str(RUN_DURATION_S),
        "APEX_RESTART_POLL_INTERVAL_S": str(POLL_INTERVAL_S),
        # Point the engine_runs projection (best-effort) at the same test DB.
        "APEX_DATABASE__URI": os.environ["APEX_TEST_DATABASE_URI"],
    }


def _spawn(thread_id: str, mode: str, db_uri: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD_SCRIPT],
        cwd=REPO_ROOT,
        env=_child_env(thread_id, mode, db_uri),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _parse_lines(output: str) -> list[dict[str, Any]]:
    """Keep only this test's JSON lines; ignore structlog/SDK noise."""
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("kind") in ("engine_poll", "final"):
            records.append(payload)
    return records


def _read_polls_then_kill(proc: subprocess.Popen[str]) -> list[dict[str, Any]]:
    """Block until KILL_AFTER_POLLS engine_poll lines, then SIGKILL mid-cycle.

    A watchdog kills the child if it never reaches the poll loop so the test
    fails with output instead of hanging.
    """
    watchdog = threading.Timer(CHILD_DEADLINE_S, proc.kill)
    watchdog.start()
    polls: list[dict[str, Any]] = []
    assert proc.stdout is not None
    try:
        while len(polls) < KILL_AFTER_POLLS:
            line = proc.stdout.readline()
            if line == "":  # child exited (or watchdog fired) before enough polls
                stderr = proc.stderr.read() if proc.stderr else ""
                pytest.fail(
                    f"subprocess A ended before {KILL_AFTER_POLLS} polls; "
                    f"saw {polls!r}; stderr:\n{stderr}"
                )
            payload = _parse_lines(line)
            if payload and payload[0]["kind"] == "engine_poll":
                polls.append(payload[0])
    finally:
        watchdog.cancel()
    time.sleep(POLL_INTERVAL_S / 2)  # land inside a poll cycle, not on its boundary
    proc.kill()  # SIGKILL: no atexit, no checkpoint flush — a real crash
    remainder, _ = proc.communicate(timeout=10)
    polls.extend(p for p in _parse_lines(remainder) if p["kind"] == "engine_poll")
    return polls


def test_run_survives_sigkill_mid_poll_with_single_engine_run() -> None:
    db_uri = os.environ["APEX_TEST_DATABASE_URI"].replace("+asyncpg", "").replace("+psycopg", "")
    thread_id = f"restart-{uuid.uuid4().hex[:12]}"

    # Process A: fresh run, killed mid-poll.
    proc_a = _spawn(thread_id, "run", db_uri)
    try:
        a_polls = _read_polls_then_kill(proc_a)
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
    assert len(a_polls) >= KILL_AFTER_POLLS
    a_run_ids = {p["external_run_id"] for p in a_polls if p["external_run_id"]}
    assert len(a_run_ids) == 1, f"process A saw multiple engine runs: {a_run_ids}"

    # Process B: re-invoke the same thread with input None -> resume + complete.
    proc_b = _spawn(thread_id, "resume", db_uri)
    stdout_b, stderr_b = proc_b.communicate(timeout=CHILD_DEADLINE_S)
    assert proc_b.returncode == 0, f"subprocess B failed:\n{stderr_b}"
    b_records = _parse_lines(stdout_b)
    b_polls = [r for r in b_records if r["kind"] == "engine_poll"]
    finals = [r for r in b_records if r["kind"] == "final"]
    assert len(finals) == 1, f"expected one final record, got {b_records!r}"
    final = finals[0]

    # The flagship assertion: ONE engine run across the crash boundary.
    run_ids = a_run_ids | {p["external_run_id"] for p in b_polls} | {final["external_run_id"]}
    run_ids.discard(None)
    assert len(run_ids) == 1, f"expected exactly one external_run_id, got {run_ids}"
    (run_id,) = run_ids

    assert final["status"] == "succeeded"
    assert final["test_summary_passed"] is True
    assert final["idempotency_key"] == f"{thread_id}-execution-a1"

    # Poll continuity (best-effort): every poll B observed is counted, and the
    # resumed count never restarts below B's own cycles. A's polls may be
    # partially lost to the SIGKILL (its final cycle's checkpoint never
    # committed), so an additive A+B bound would be racy — the load-bearing
    # crash-boundary assertion is the single external_run_id above.
    assert final["poll_count"] >= len(b_polls)

    # And the idempotency mechanism itself: provisioning the checkpointed key
    # again (twice) yields the same external run — restarts cannot double-start.
    import asyncio

    from apex.adapters.sim_engine import SimExecutionEngine
    from apex.domain.integrations import LoadTestSpec

    engine = SimExecutionEngine(None)
    spec = LoadTestSpec(idempotency_key=f"{thread_id}-execution-a1", title="probe")
    assert asyncio.run(engine.provision(spec)).external_run_id == run_id
    assert asyncio.run(engine.provision(spec)).external_run_id == run_id
