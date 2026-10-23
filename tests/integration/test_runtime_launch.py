"""A wizard launch through the /v1 facade completes on a real LangGraph runtime.

Both launch-breaking bugs of the new-test review passed the unit suite: the
facade skipped the native run-create hook (no prompt manifest, F-01), and the
Postgres runtime's `HTTPStatus` errors became 500s, so every idempotent launch
returned 503. This test sends the dashboard wizard's request shape
(`test_scope`, prompt selections, idempotency key) to `POST /v1/pipelines` on a
running server and checks what the production-runtime check verified by hand
(docs/qa/new-test-pipeline-review-2026-10-22/PRODUCTION_RUNTIME_CHECK.md).

Two runtimes:

- `inmem`: `langgraph dev` against a fresh database created on the server that
  APEX_TEST_DATABASE_URI names (dropped afterwards). Runs in the CI
  `integration` job; about 20s.
- `postgres`: the licensed `langgraph build` image on `docker-compose.yaml`, as
  its own Compose project (removed afterwards). Opt in with
  APEX_TEST_POSTGRES_RUNTIME=1; it also needs LANGGRAPH_CLOUD_LICENSE_KEY or
  LANGSMITH_API_KEY and skips without one. APEX_TEST_RUNTIME_IMAGE reuses an
  already-built image instead of building one.

Keys are only ever passed through the environment, and server output shown on a
failure has them redacted.
"""

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASES = [
    "story_analysis",
    "test_planning",
    "env_triage",
    "script_scenario",
    "execution",
    "reporting",
    "postmortem",
]
PROJECT_ID = "runtime-test"
AUTO_GATES = {"prompt_review": "auto", "output_review": "auto"}
LICENSE_ENV = ("LANGGRAPH_CLOUD_LICENSE_KEY", "LANGSMITH_API_KEY")
# Ambient settings that would make the server under test differ from the one
# the test describes (the developer's .env values, tracing, a real LLM key).
AMBIENT_PREFIXES = ("APEX_", "LANGSMITH_", "LANGCHAIN_", "LANGGRAPH_", "ANTHROPIC_")
AMBIENT_NAMES = ("DATABASE_URI", "POSTGRES_URI", "REDIS_URI")
STARTUP_TIMEOUT_S = 120.0
RUN_TIMEOUT_S = 180.0


@dataclass
class Runtime:
    base_url: str
    admin_key: str
    # libpq conninfo for the APEX schema's database.
    apex_db: str
    # Returns the runtime's stored `configurable` for a run. On `inmem` it stops
    # the server (the store is only flushed to disk), so call it last.
    read_run_configurable: Callable[[str, str], dict[str, Any]]


def _secret_values() -> list[str]:
    return [value for name in LICENSE_ENV if (value := os.environ.get(name))]


def _redact(text: str, *extra: str) -> str:
    for value in (*_secret_values(), *extra):
        if value:
            text = text.replace(value, "***")
    return text


def _hermetic_env(**overrides: str) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(AMBIENT_PREFIXES) and name not in AMBIENT_NAMES
    }
    env.update(PYTHONPATH=str(REPO_ROOT / "src"), PYTHONUNBUFFERED="1")
    env.update(overrides)
    return env


def _run(command: list[str], *, env: dict[str, str], cwd: Path, timeout: float) -> None:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        output = _redact(result.stdout[-4000:] + result.stderr[-4000:])
        pytest.fail(f"{command[0]} {command[1]} exited {result.returncode}:\n{output}")


def _wait_until_ok(base_url: str, describe_failure: Callable[[], str]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/ok", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    pytest.fail(f"runtime did not become ready at {base_url}:\n{describe_failure()}")


# ── inmem: `langgraph dev` ───────────────────────────────────────────────────


def _dev_config(path: Path) -> None:
    """The repo's langgraph.json with absolute paths and no `.env`.

    The server runs from a temporary directory so it neither reads the
    developer's `.env` nor shares their `.langgraph_api` store.
    """
    config = json.loads((REPO_ROOT / "langgraph.json").read_text())

    def absolute(spec: str) -> str:
        return str(REPO_ROOT / spec[2:]) if spec.startswith("./") else spec

    config["graphs"] = {name: absolute(spec) for name, spec in config["graphs"].items()}
    config["http"]["app"] = absolute(config["http"]["app"])
    config["auth"]["path"] = absolute(config["auth"]["path"])
    config.pop("source", None)
    config["dependencies"] = [str(REPO_ROOT)]
    config["env"] = {}
    path.write_text(json.dumps(config))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Runs in a child interpreter: langgraph_api reads its runtime settings at
# import, which must not leak into the test process.
READ_INMEM_RUN = r"""
import json, pickle, sys
with open(".langgraph_api/.langgraph_ops.pckl", "rb") as stream:
    ops = pickle.load(stream)
run = next(r for r in ops["runs"] if str(r["run_id"]) == sys.argv[1])
assert str(run["thread_id"]) == sys.argv[2], "run belongs to another thread"
print(json.dumps(run["kwargs"]["config"]["configurable"], default=str))
"""


@pytest.fixture
def inmem_runtime(tmp_path: Path) -> Iterator[Runtime]:
    server_uri = os.environ.get("APEX_TEST_DATABASE_URI")
    if not server_uri:
        pytest.skip("needs postgres for the APEX schema (set APEX_TEST_DATABASE_URI)")
    if shutil.which("langgraph", path=str(Path(sys.executable).parent)) is None:
        pytest.skip("langgraph CLI is not installed in this environment")

    admin_conninfo = server_uri.replace("+asyncpg", "").replace("+psycopg", "")
    database = f"apex_runtime_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    apex_db = str(make_conninfo(admin_conninfo, dbname=database))
    host_db_uri = (
        server_uri.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://", 1)
        + f"/{database}"
    )
    admin_key = secrets.token_urlsafe(32)
    env = _hermetic_env(
        APEX_ENVIRONMENT="dev",
        APEX_DATABASE__URI=host_db_uri,
        APEX_BOOTSTRAP_ADMIN_KEY=admin_key,
        LANGGRAPH_CLI_NO_ANALYTICS="1",
    )
    server: subprocess.Popen[bytes] | None = None
    log_path = tmp_path / "server.log"
    try:
        _run([sys.executable, "-m", "apex.persistence.migrate"], env=env, cwd=tmp_path, timeout=300)
        (tmp_path / "bootstrap.json").write_text(
            json.dumps({"seed_default_prompts": True, "admin": {"name": "runtime-test-admin"}})
        )
        _run(
            [sys.executable, "-m", "apex.bootstrap", "bootstrap.json"],
            env=env,
            cwd=tmp_path,
            timeout=120,
        )
        _dev_config(tmp_path / "langgraph.json")
        port = _free_port()
        with log_path.open("wb") as log:
            server = subprocess.Popen(
                [
                    str(Path(sys.executable).parent / "langgraph"),
                    "dev",
                    "--no-browser",
                    "--no-reload",
                    "--port",
                    str(port),
                    "--config",
                    "langgraph.json",
                ],
                cwd=tmp_path,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        base_url = f"http://127.0.0.1:{port}"
        _wait_until_ok(base_url, lambda: _redact(log_path.read_text()[-6000:], admin_key))

        def read_run_configurable(thread_id: str, run_id: str) -> dict[str, Any]:
            assert server is not None
            _stop(server)
            result = subprocess.run(
                [sys.executable, "-c", READ_INMEM_RUN, run_id, thread_id],
                cwd=tmp_path,
                env=_hermetic_env(DATABASE_URI=":memory:", REDIS_URI="fake"),
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr[-4000:]
            return json.loads(result.stdout.strip().splitlines()[-1])

        yield Runtime(base_url, admin_key, apex_db, read_run_configurable)
    finally:
        if server is not None:
            _stop(server)
        with psycopg.connect(admin_conninfo, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _stop(server: subprocess.Popen[bytes]) -> None:
    """Ctrl-C the server's process group, so the in-memory store is flushed."""
    if server.poll() is not None:
        return
    os.killpg(server.pid, signal.SIGINT)
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=10)


# ── postgres: the licensed image on docker-compose.yaml ──────────────────────

COMPOSE_OVERRIDE = """\
services:
  postgres:
    ports: ["127.0.0.1::5432"]
  apex-server:
    ports: !override ["127.0.0.1::8000"]
"""


@pytest.fixture
def postgres_runtime(tmp_path: Path) -> Iterator[Runtime]:
    if os.environ.get("APEX_TEST_POSTGRES_RUNTIME") != "1":
        pytest.skip("opt in with APEX_TEST_POSTGRES_RUNTIME=1 (builds and starts the image)")
    if not _secret_values():
        pytest.skip("needs LANGGRAPH_CLOUD_LICENSE_KEY or LANGSMITH_API_KEY")
    if shutil.which("docker") is None:
        pytest.skip("needs docker")

    image = os.environ.get("APEX_TEST_RUNTIME_IMAGE")
    if not image:
        image = "apex-orchestration-engine:runtime-test"
        _run(
            [str(Path(sys.executable).parent / "langgraph"), "build", "-t", image],
            env=_hermetic_env(),
            cwd=REPO_ROOT,
            timeout=1200,
        )

    admin_key = secrets.token_urlsafe(32)
    db_password = secrets.token_urlsafe(16)
    # Distinct from the dev stack's project (apex_orchestration_engine), so its
    # volumes and database are never touched.
    project = f"apex_runtime_test_{uuid.uuid4().hex[:8]}"
    (tmp_path / "override.yaml").write_text(COMPOSE_OVERRIDE)
    (tmp_path / "empty.env").write_text("")
    env = _hermetic_env(
        APEX_IMAGE=image,
        APEX_BOOTSTRAP_ADMIN_KEY=admin_key,
        APEX_POSTGRES_PASSWORD=db_password,
        **{name: os.environ[name] for name in LICENSE_ENV if os.environ.get(name)},
    )
    compose = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(REPO_ROOT),
        # An empty env file instead of the developer's auto-loaded `.env`.
        "--env-file",
        str(tmp_path / "empty.env"),
        "-f",
        str(REPO_ROOT / "docker-compose.yaml"),
        "-f",
        str(tmp_path / "override.yaml"),
    ]

    def port_of(service: str, port: int) -> str:
        result = subprocess.run(
            [*compose, "port", service, str(port)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().rsplit(":", 1)[1]

    def logs() -> str:
        result = subprocess.run(
            [*compose, "logs", "--no-color", "--tail", "80"],
            env=env,
            capture_output=True,
            text=True,
        )
        return _redact(result.stdout + result.stderr, admin_key, db_password)

    try:
        _run(
            [*compose, "up", "--detach", "--quiet-pull", "apex-server", "minio-storage"],
            env=env,
            cwd=REPO_ROOT,
            timeout=600,
        )
        base_url = f"http://127.0.0.1:{port_of('apex-server', 8000)}"
        _wait_until_ok(base_url, logs)
        apex_db = make_conninfo(
            host="127.0.0.1",
            port=port_of("postgres", 5432),
            user="apex",
            password=db_password,
            dbname="apex",
        )

        def read_run_configurable(thread_id: str, run_id: str) -> dict[str, Any]:
            with psycopg.connect(apex_db) as conn:
                row = conn.execute(
                    "SELECT kwargs FROM public.run WHERE run_id = %s AND thread_id = %s",
                    (run_id, thread_id),
                ).fetchone()
            assert row is not None, "the runtime has no stored run"
            return row[0]["config"]["configurable"]

        yield Runtime(base_url, admin_key, apex_db, read_run_configurable)
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans", "--timeout", "10"],
            env=env,
            capture_output=True,
        )


# ── the launch ───────────────────────────────────────────────────────────────


@pytest.fixture(params=["inmem", "postgres"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.getfixturevalue(f"{request.param}_runtime")


def _ok(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, f"{response.request.url}: {response.text[:2000]}"
    return response.json()


def _wizard_launch(client: httpx.Client) -> dict[str, Any]:
    """The body NewRunWizard sends for a Simulated, all-agent, all-auto test."""
    suffix = uuid.uuid4().hex[:8]
    app = _ok(
        client.post(
            "/v1/catalog/applications",
            json={"project_id": PROJECT_ID, "name": f"Checkout {suffix}"},
        ),
        201,
    )
    environment = _ok(
        client.post("/v1/catalog/environments", json={"application_id": app["id"], "name": "PERF"}),
        201,
    )
    prompts = _ok(client.get("/v1/prompts", params={"selection_enabled": True}), 200)

    def default(namespace: str, phase: str | None = None) -> dict[str, str]:
        row = next(
            row
            for row in prompts
            if row["namespace"] == namespace
            and row["phase"] == phase
            and row["is_default"]
            and row["active_version"]
        )
        return {"prompt_id": row["id"], "version_id": row["active_version"]["id"]}

    test_scope = {
        "schema_version": 1,
        "participants": [
            {
                "id": "backend",
                "layer": "backend",
                "project_id": PROJECT_ID,
                "application_id": app["id"],
                "environment_id": environment["id"],
            }
        ],
        "primary_participant_id": "backend",
        "traffic_entry_participant_id": "backend",
    }
    return {
        "idempotency_key": str(uuid.uuid4()),
        "test_scope": test_scope,
        "assistant_id": "pipeline",
        "title": f"Runtime launch {suffix}",
        "request": "Verify checkout holds P95 under peak load",
        "project_id": PROJECT_ID,
        "app_id": app["id"],
        "configurable": {
            "project_id": PROJECT_ID,
            "engine": "sim",
            "gates": {phase: dict(AUTO_GATES) for phase in PHASES},
            "prompt_selections": {
                "schema_version": 1,
                "system": default("system"),
                "phases": {phase: default("phase", phase) for phase in PHASES},
                "application": [],
                "environment": [],
            },
            "test_scope": test_scope,
            "app_id": app["id"],
            "environment_id": environment["id"],
            "phases": PHASES,
            "phase_execution": {
                "schema_version": 1,
                "owners": {phase: "langgraph" for phase in PHASES},
            },
            "analysis": {"schema_version": 1, "selections": []},
        },
    }


def _wait_for_completion(client: httpx.Client, thread_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        detail = _ok(client.get(f"/v1/pipelines/{thread_id}"), 200)
        if detail["thread_status"] not in ("busy", "pending"):
            return detail
        if time.monotonic() > deadline:
            pytest.fail(f"run still {detail['thread_status']} after {RUN_TIMEOUT_S}s")
        time.sleep(1)


def _phase_usage(apex_db: str, thread_id: str) -> list[tuple[Any, ...]]:
    # Usage is recorded as each phase finishes; allow the last write to land.
    deadline = time.monotonic() + 15
    while True:
        with psycopg.connect(apex_db) as conn:
            rows = conn.execute(
                "SELECT action, consumer_id, consumer_name FROM apex.usage_events "
                "WHERE thread_id = %s AND action LIKE 'phase:%%'",
                (thread_id,),
            ).fetchall()
        if len(rows) >= len(PHASES) or time.monotonic() > deadline:
            return rows
        time.sleep(0.5)


def test_wizard_launch_completes_on_a_real_runtime(runtime: Runtime) -> None:
    client = httpx.Client(
        base_url=runtime.base_url, headers={"x-api-key": runtime.admin_key}, timeout=60
    )
    consumer_id = _ok(client.get("/v1/system/info"), 200)["consumer"]["id"]
    body = _wizard_launch(client)

    launched = _ok(client.post("/v1/pipelines", json=body), 202)
    thread_id, run_id = launched["thread_id"], launched["run_id"]
    detail = _wait_for_completion(client, thread_id)

    assert detail["failure"] is None
    assert detail["interrupts"] == []
    assert detail["thread_status"] == "idle"
    assert [(p["phase"], p["status"]) for p in detail["phase_strip"]] == [
        (phase, "succeeded") for phase in PHASES
    ]
    assert detail["execution_result"] == "passed"
    assert detail["engine"]["engine"] == "sim"
    assert detail["values"]["run_config"]["engine"] == "sim"
    assert detail["values"]["run_config"]["agent_backend"] == "stub"

    # Retrying the same request adopts the run on its deterministic thread.
    retried = _ok(client.post("/v1/pipelines", json=body), 202)
    assert (retried["thread_id"], retried["run_id"]) == (thread_id, run_id)

    with psycopg.connect(runtime.apex_db) as conn:
        admissions = conn.execute(
            "SELECT actor_id, models FROM apex.model_run_admissions WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
    assert len(admissions) == 1, admissions
    actor_id, models = admissions[0]
    assert actor_id == consumer_id
    assert set(models) == set(PHASES)

    usage = _phase_usage(runtime.apex_db, thread_id)
    assert {action.split(":")[1] for action, _, _ in usage} == set(PHASES)
    assert all(row_consumer == consumer_id for _, row_consumer, _ in usage), usage
    assert all(name for _, _, name in usage), usage

    configurable = runtime.read_run_configurable(thread_id, run_id)
    assert configurable["apex_actor_id"] == consumer_id
    manifest = configurable["prompt_manifest"]
    assert set(manifest["phases"]) == set(PHASES)
    selections = body["configurable"]["prompt_selections"]
    assert manifest["system"]["prompt_id"] == selections["system"]["prompt_id"]
    assert manifest["system"]["version_id"] == selections["system"]["version_id"]
