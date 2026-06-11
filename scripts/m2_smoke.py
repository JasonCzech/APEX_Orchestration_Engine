"""M2 live verification against langgraph dev + Postgres (seeded).

Usage: PYTHONPATH=src uv run --no-sync python scripts/m2_smoke.py <admin_key> <viewer_key>
Covers: prompt catalog lifecycle + catalog-resolved prompts in runs, app/env catalog,
connection probe, document upload + artifact proxy, pipelines facade, CAS gate resume
(incl. superseded 409), consumers admin (key-once), drafts, context, playground.
"""

import sys
import time

import httpx

BASE = "http://127.0.0.1:2024"
ALL_PHASES = [
    "story_analysis",
    "test_planning",
    "env_triage",
    "script_scenario",
    "execution",
    "reporting",
    "postmortem",
]
AUTO = {"prompt_review": "auto", "output_review": "auto"}


def gates(**overrides: dict) -> dict:
    policy = {p: dict(AUTO) for p in ALL_PHASES}
    policy.update(overrides)
    return policy


def wait_idle(c: httpx.Client, tid: str, timeout_s: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = c.get(f"/threads/{tid}").json()["status"]
        if status not in ("busy",):
            return status
        time.sleep(0.5)
    raise TimeoutError(f"thread {tid} still busy")


def main() -> int:
    admin_key, viewer_key = sys.argv[1], sys.argv[2]
    c = httpx.Client(
        base_url=BASE,
        headers={"x-api-key": admin_key, "content-type": "application/json"},
        timeout=120,
    )

    # 1. DB-backed identity
    info = c.get("/v1/system/info").json()
    assert info["consumer"]["name"] == "apex-admin", info
    print(f"1. identity: {info['consumer']['name']} role={info['consumer']['role']}")

    # 2. Prompt catalog lifecycle
    prompts = c.get("/v1/prompts", params={"namespace": "phase"}).json()
    target = next(p for p in prompts if p["key"] == "story_analysis/system")
    pid = target["id"]
    before_version = target["active_version"]["version"]
    saved = c.post(
        f"/v1/prompts/{pid}/versions",
        json={"content": "You are the APEX story analyst (edited via API).", "note": "m2 demo"},
    ).json()
    detail = c.get(f"/v1/prompts/{pid}").json()
    new_version = detail["active_version"]["version"]
    assert new_version > before_version, detail
    versions = c.get(f"/v1/prompts/{pid}/versions").json()
    v1_id = next(v["id"] for v in versions if v["version"] == 1)
    c.post(f"/v1/prompts/{pid}/rollback", json={"version_id": v1_id})
    rolled = c.get(f"/v1/prompts/{pid}").json()
    assert rolled["active_version"]["version"] == 1, rolled
    print(
        f"2. prompts: {len(prompts)} phase prompts; saved v{new_version}, rolled back to v1 "
        f"(v{new_version} id {saved['id'][:8]}... preserved immutably)"
    )

    # 3. Catalog-resolved prompt flows into a pipeline run
    tid = c.post("/threads", json={"metadata": {"project_id": "demo"}}).json()["thread_id"]
    run = c.post(
        f"/threads/{tid}/runs/wait",
        json={
            "assistant_id": "pipeline",
            "input": {"title": "M2 demo", "request": "Catalog prompt check"},
            "config": {
                "configurable": {
                    "project_id": "demo",
                    "phases": ["story_analysis"],
                    "gates": gates(),
                }
            },
        },
    ).json()
    src = run["phase_results"]["story_analysis"]["resolved_prompt_source"]
    assert src["origin"] == "catalog" and "@v1" in src["ref"], src
    print(f"3. run resolved prompt from catalog: ref={src['ref'].split(',')[0]}...")

    # 4. App/env catalog
    apps = c.get("/v1/catalog/applications", params={"project": "demo"}).json()
    envs = c.get("/v1/catalog/environments").json()
    assert any(a["name"] == "Checkout" for a in apps) and envs, (apps, envs)
    print(f"4. catalog: {len(apps)} application(s), {len(envs)} environment(s)")

    # 5. Connection probe
    conns = c.get("/v1/admin/connections").json()
    wt = next(x for x in conns if x["kind"] == "work_tracking")
    probe = c.post(f"/v1/admin/connections/{wt['id']}/test").json()
    assert probe["ok"] is True, probe
    print(f"5. connection probe: {wt['name']} ok ({probe['latency_ms']}ms)")

    # 6. Document upload + artifact proxy round-trip
    content = b"Perf test context: checkout p95 regression notes."
    up = httpx.post(
        f"{BASE}/v1/documents",
        headers={"x-api-key": admin_key},
        files={"file": ("notes.txt", content, "text/plain")},
        data={"project_id": "demo", "summary": "demo doc"},
        timeout=60,
    ).json()
    fetched = c.get(f"/v1/artifacts/{up['artifact_key']}")
    assert fetched.content == content, fetched.status_code
    print(
        f"6. document {up['id'][:8]}... uploaded ({up['size_bytes']}B) and "
        f"served via artifact proxy"
    )

    # 7. Pipelines facade
    grid = c.get("/v1/pipelines", params={"project": "demo"}).json()["items"]
    row = next(r for r in grid if r["thread_id"] == tid)
    strip = {s["phase"]: s["status"] for s in row["phase_strip"]}
    assert strip["story_analysis"] == "succeeded" and strip["reporting"] == "none", strip
    print(f"7. facade: {len(grid)} run(s); phase strip correct for demo thread")

    # 8. CAS gate resume + superseded 409
    tid2 = c.post("/threads", json={"metadata": {"project_id": "demo"}}).json()["thread_id"]
    paused = c.post(
        f"/threads/{tid2}/runs/wait",
        json={
            "assistant_id": "pipeline",
            "input": {"title": "CAS demo"},
            "config": {"configurable": {"project_id": "demo", "phases": ["story_analysis"]}},
        },
    ).json()
    assert "__interrupt__" in paused
    pending = c.get(f"/v1/pipelines/{tid2}").json()["interrupts"]
    iid = pending[0]["interrupt_id"]
    first = c.post(f"/v1/pipelines/{tid2}/gates/{iid}/resume", json={"action": "approve"})
    assert first.status_code == 202, (first.status_code, first.text)
    wait_idle(c, tid2)
    second = c.post(f"/v1/pipelines/{tid2}/gates/{iid}/resume", json={"action": "approve"})
    assert second.status_code == 409, (second.status_code, second.text)
    print(f"8. CAS resume: first=202, replay after completion=409 ({second.json()['title']})")

    # 9. Consumers admin: key shown exactly once, immediately usable
    created = c.post(
        "/v1/admin/consumers",
        json={
            "name": "m2-smoke-operator",
            "consumer_type": "headless",
            "role": "operator",
            "scopes": [{"project_id": "demo"}],
        },
    )
    if created.status_code == 409:  # idempotent re-run: rotate instead
        existing = next(
            x for x in c.get("/v1/admin/consumers").json() if x["name"] == "m2-smoke-operator"
        )
        created = c.post(f"/v1/admin/consumers/{existing['id']}/rotate")
    new_key = created.json()["api_key"]
    op_info = httpx.get(f"{BASE}/v1/system/info", headers={"x-api-key": new_key}, timeout=30).json()
    assert op_info["consumer"]["role"] == "operator", op_info
    listed = next(
        x for x in c.get("/v1/admin/consumers").json() if x["name"] == "m2-smoke-operator"
    )
    assert "api_key" not in listed
    print("9. consumers: created operator key (shown once), key works, never re-exposed")

    # 10. Viewer role gating
    denied = httpx.post(
        f"{BASE}/v1/drafts",
        headers={"x-api-key": viewer_key},
        json={"title": "nope", "payload": {}},
        timeout=30,
    )
    assert denied.status_code == 403, denied.status_code
    print("10. viewer mutation denied with 403")

    # 11. Drafts CRUD
    d = c.post(
        "/v1/drafts",
        json={"title": "wizard draft", "project_id": "demo", "payload": {"step": "scope"}},
    ).json()
    c.put(
        f"/v1/drafts/{d['id']}",
        json={"title": "wizard draft", "project_id": "demo", "payload": {"step": "review"}},
    )
    got = c.get(f"/v1/drafts/{d['id']}").json()
    assert got["payload"]["step"] == "review"
    c.delete(f"/v1/drafts/{d['id']}")
    print("11. drafts: create/update/get/delete round-trip")

    # 12. Context summaries (background run) + evidence aggregation
    summary = c.post(
        "/v1/context/summaries",
        json={"subject": "checkout perf", "work_item_keys": ["PHX-241"], "project_id": "demo"},
    )
    assert summary.status_code == 202, summary.text
    evidence = c.get("/v1/context/evidence", params={"project": "demo"})
    assert evidence.status_code == 200
    print(
        f"12. context: summary run {summary.json()['run_id'][:8]}... started; "
        f"evidence endpoint returns {len(evidence.json())} packet(s)"
    )

    # 13. Prompt playground test-execute
    test = c.post(f"/v1/prompts/{pid}/test", json={"sample_input": {"story": "PHX-241 text"}})
    assert test.status_code == 202, test.text
    print(f"13. playground: test run {test.json()['run_id'][:8]}... accepted")

    print("\nM2 smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
