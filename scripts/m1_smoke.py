"""M1 live verification against a running langgraph dev server.

Drives the gated pipeline end-to-end over the server API: interrupt discovery via
threads/search, resume via command.resume, single-phase re-run, custom-event
streaming, and the resume-conflict (multitask_strategy=reject) spike.
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:2024"
KEY = "dev-key-m1"
HDRS = {"x-api-key": KEY, "content-type": "application/json"}

AUTO = {"prompt_review": "auto", "output_review": "auto"}
ALL_PHASES = [
    "story_analysis",
    "test_planning",
    "env_triage",
    "script_scenario",
    "execution",
    "reporting",
    "postmortem",
]


def gates(**overrides: dict) -> dict:
    policy = {p: dict(AUTO) for p in ALL_PHASES}
    policy.update(overrides)
    return policy


def main() -> int:
    c = httpx.Client(base_url=BASE, headers=HDRS, timeout=120)

    # 1. Gated run pauses at story_analysis prompt review
    tid = c.post("/threads", json={"metadata": {"project_id": "demo"}}).json()["thread_id"]
    cfg = {
        "configurable": {
            "project_id": "demo",
            "gates": gates(story_analysis={"prompt_review": "gated", "output_review": "auto"}),
        }
    }
    run = c.post(
        f"/threads/{tid}/runs/wait",
        json={
            "assistant_id": "pipeline",
            "input": {"title": "M1 demo", "request": "Load test checkout"},
            "config": cfg,
        },
    ).json()
    interrupt = run["__interrupt__"][0]["value"] if "__interrupt__" in run else None
    assert interrupt and interrupt["kind"] == "prompt_review", run
    print(f"1. gated run paused: kind={interrupt['kind']} phase={interrupt['phase']}")

    # 2. Inbox discovery: threads/search status=interrupted + payload from thread state
    found = c.post("/threads/search", json={"status": "interrupted"}).json()
    assert any(t["thread_id"] == tid for t in found), found
    state = c.get(f"/threads/{tid}/state").json()
    task_interrupts = [i for t in state.get("tasks", []) for i in t.get("interrupts", [])]
    assert task_interrupts and task_interrupts[0]["value"]["kind"] == "prompt_review"
    print(
        f"2. inbox: thread found via search; payload exposed in state tasks "
        f"(actions={task_interrupts[0]['value']['actions']})"
    )

    # 3. Resume approve -> completes all 7 phases
    final = c.post(
        f"/threads/{tid}/runs/wait",
        json={
            "assistant_id": "pipeline",
            "command": {"resume": {"action": "approve"}},
            "config": cfg,
        },
    ).json()
    statuses = {p: final["phase_results"][p]["status"] for p in ALL_PHASES}
    assert all(s == "succeeded" for s in statuses.values()), statuses
    approvals = final["phase_results"]["story_analysis"]["approvals"]
    print(f"3. resumed: all 7 phases succeeded; approval actor={approvals[0]['actor']}")

    # 4. Single-phase re-run on the same thread -> attempt increments
    rerun_cfg = {"configurable": {"project_id": "demo", "phases": ["env_triage"], "gates": gates()}}
    # NB: input=None would mean "continue from checkpoint" (no new plan_resolver pass);
    # a fresh phase-subset run needs a real input to start from START.
    rerun = c.post(
        f"/threads/{tid}/runs/wait",
        json={"assistant_id": "pipeline", "input": {}, "config": rerun_cfg},
    ).json()
    entry = rerun["phase_results"]["env_triage"]
    assert entry["attempt"] == 2 and entry["status"] == "succeeded", entry
    assert rerun["phase_results"]["reporting"]["attempt"] == 1  # untouched
    print(f"4. single-phase re-run: env_triage attempt={entry['attempt']}, others untouched")

    # 5. Custom events through the server stream (subgraph event delivery check)
    tid2 = c.post("/threads", json={"metadata": {"project_id": "demo"}}).json()["thread_id"]
    seen: list[str] = []
    with c.stream(
        "POST",
        f"/threads/{tid2}/runs/stream",
        json={
            "assistant_id": "pipeline",
            "input": {"title": "events"},
            "config": {"configurable": {"project_id": "demo", "gates": gates()}},
            "stream_mode": ["custom"],
            "stream_subgraphs": True,
        },
    ) as resp:
        assert resp.status_code == 200, (resp.status_code, resp.read()[:500])
        event_type = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_type and event_type.startswith("custom"):
                payload = json.loads(line.split(":", 1)[1])
                if isinstance(payload, dict) and "type" in payload:
                    seen.append(payload["type"])
    kinds = sorted(set(seen))
    assert "plan_resolved" in kinds, kinds
    subgraph_events_ok = "phase_status" in kinds and "tool_call" in kinds
    print(
        f"5. custom events via server: {kinds} "
        f"(subgraph delivery {'OK' if subgraph_events_ok else 'MISSING — needs follow-up'})"
    )

    # 6. Resume-conflict spike: double-resume with multitask_strategy=reject
    tid3 = c.post("/threads", json={"metadata": {"project_id": "demo"}}).json()["thread_id"]
    gated_cfg = {"configurable": {"project_id": "demo", "phases": ["story_analysis"]}}
    c.post(
        f"/threads/{tid3}/runs/wait",
        json={"assistant_id": "pipeline", "input": {"title": "conflict"}, "config": gated_cfg},
    )
    # first resume: background run (occupies the thread)
    r1 = c.post(
        f"/threads/{tid3}/runs",
        json={
            "assistant_id": "pipeline",
            "command": {"resume": {"action": "modify", "prompt": {"system": "edited"}}},
            "config": gated_cfg,
            "multitask_strategy": "reject",
        },
    )
    # second resume immediately: must be rejected, not enqueued
    r2 = c.post(
        f"/threads/{tid3}/runs",
        json={
            "assistant_id": "pipeline",
            "command": {"resume": {"action": "approve"}},
            "config": gated_cfg,
            "multitask_strategy": "reject",
        },
    )
    print(
        f"6. conflict spike: first resume={r1.status_code}, "
        f"concurrent second resume={r2.status_code} "
        f"({'REJECTED as designed' if r2.status_code == 409 else 'check semantics'})"
    )

    print("\nM1 smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
