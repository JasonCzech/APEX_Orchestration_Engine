"""Execution-phase engine spine: engine_reserve -> engine_start -> engine_poll ⟲ -> engine_collect.

Replaces the stub `agent` node between the prompt and output gates for
Phase.EXECUTION (wired by make_phase_subgraph; the gate nodes are untouched and
keep routing to the node name "agent", which here is a no-op alias flowing into
engine_reserve). Durability rules (plan "Durability & the execution phase"):

- engine_reserve writes the LoadTestSpec — with the deterministic idempotency key
  ``{thread_id}-execution-a{attempt}`` — into graph state and returns, so the
  checkpoint commits BEFORE any engine side effect. A crash after the checkpoint
  can only re-issue the same get-or-create call.
- engine_start resolves the adapter per call (throwaway resolver: sync nodes run
  on worker threads with short-lived event loops, like the prompts service) and
  provisions by key: re-execution after a crash yields the same external_run_id.
- engine_poll is a self-loop (Command goto back to itself): one superstep — one
  checkpoint plus one ``engine_poll`` custom event — per cycle, with the
  inter-cycle sleep inside the node. A server restart resumes polling from the
  durable EngineHandle, losing at most one cycle.
- engine_collect persists artifacts + summary and ALWAYS tears the run down.

recursion_limit sizing: every poll cycle consumes one superstep inside the
execution subgraph, so runs containing the execution phase must budget roughly

    ceil(limits.poll_timeout_s / limits.poll_interval_s) + SPINE_SUPERSTEPS + headroom

LangGraph's default (25) only fits long production poll intervals; tests and
demos with tiny intervals must pass ``config={"recursion_limit": ...}`` — see
recommended_recursion_limit().
"""

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.types import Command

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.domain.integrations import LoadTestSpec, TestResultSummary
from apex.domain.pipeline import EngineHandle, Phase, PhaseStatus, utcnow_iso
from apex.graphs.pipeline.configurable import Limits, PipelineConfigurable
from apex.graphs.pipeline.phase_subgraph import EVENT_SCHEMA_VERSION, emit_event
from apex.graphs.pipeline.state import JsonDict, PipelineState
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineRunPhase,
    EngineRunStatus,
    LiveStats,
)
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver, DbConnectionStore

logger = structlog.get_logger(__name__)

_PHASE = Phase.EXECUTION

# Supersteps the execution subgraph consumes outside the poll loop (prepare, gate
# nodes, agent alias, reserve/start/collect, finalize) plus parent-spine slack.
SPINE_SUPERSTEPS = 16

# LoadTestSpec fields a per-run "load_test" configurable dict may override; any
# other key (e.g. the sim engine's fail_at_pct) is treated as an engine option.
_SPEC_OVERRIDE_FIELDS = frozenset(LoadTestSpec.model_fields) - {"idempotency_key"}


def recommended_recursion_limit(limits: Limits) -> int:
    """Recursion-limit hint for runs that include the execution phase."""
    cycles = math.ceil(limits.poll_timeout_s / max(limits.poll_interval_s, 1e-9))
    return cycles + SPINE_SUPERSTEPS + 25


def execution_idempotency_key(thread_id: str, attempt: int) -> str:
    """Write-ahead engine idempotency key: deterministic per (thread, attempt)."""
    return f"{thread_id}-execution-a{attempt}"


# ── small state helpers (execution-entry specializations of the phase spine's) ──


def _entry(state: PipelineState) -> JsonDict:
    return (state.get("phase_results") or {}).get(_PHASE.value) or {}


def _attempt(entry: JsonDict) -> int:
    return int(entry.get("attempt") or 1)


def _update(attempt: int, **fields: Any) -> JsonDict:
    # Carries the attempt so the phase_results reducer merges instead of clobbers.
    return {"phase_results": {_PHASE.value: {"attempt": attempt, **fields}}}


def _thread_id(config: RunnableConfig | None) -> str:
    configurable = dict((config or {}).get("configurable") or {})
    return str(configurable.get("thread_id") or "no-thread")


def _engine_options(entry: JsonDict) -> JsonDict:
    return dict(entry.get("engine_options") or {})


def _handle_from(state: PipelineState, entry: JsonDict) -> EngineHandle:
    raw = state.get("engine_handle") or entry.get("engine_handle")
    if not raw:
        raise ValueError(
            "execution phase: engine_handle missing from state (engine_start must run first)"
        )
    return EngineHandle.model_validate(raw)


def _elapsed_s(started_at_iso: str | None) -> float | None:
    if not started_at_iso:
        return None
    try:
        started = datetime.fromisoformat(started_at_iso)
    except ValueError:
        return None
    return (datetime.now(UTC) - started).total_seconds()


# ── adapter resolution (module-level seams so tests can pin/spy them) ──────────


def _make_resolver() -> ConnectionResolver:
    """Throwaway resolver per call: graph nodes run sync on worker threads with
    short-lived event loops, so no resolver/DB state may outlive one asyncio.run.
    Falls back to the static DEV_CONNECTIONS map when Postgres is absent."""
    return ConnectionResolver(store=DbConnectionStore())


async def _resolve_engine(cfg: PipelineConfigurable, engine_options: JsonDict) -> Any:
    """Resolve the execution-engine adapter.

    Per-run engine options (extra keys in the "load_test" configurable, e.g. the
    sim engine's fail_at_pct) build an ephemeral connection for cfg.engine —
    options are run state, not connection state. Otherwise normal connection
    resolution applies: cfg.connections["execution_engine"] > project default >
    global default > DEV_CONNECTIONS sim fallback.
    """
    if engine_options:
        conn = ConnectionConfig(
            id=f"run-engine-{cfg.engine}",
            kind=PortKind.EXECUTION_ENGINE,
            provider=cfg.engine,
            name=f"Per-run {cfg.engine} engine",
            options=engine_options,
        )
        return await ConnectionResolver(connections=[conn]).resolve(
            PortKind.EXECUTION_ENGINE, connection_id=conn.id
        )
    return await _make_resolver().resolve(
        PortKind.EXECUTION_ENGINE,
        connection_id=cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
        project_id=cfg.project_id,
    )


async def _resolve_artifact_store(cfg: PipelineConfigurable) -> Any:
    return await _make_resolver().resolve(
        PortKind.ARTIFACT_STORE,
        connection_id=cfg.connections.get(PortKind.ARTIFACT_STORE.value),
        project_id=cfg.project_id,
    )


# ── event/sample shapes ────────────────────────────────────────────────────────


def _poll_event(handle: EngineHandle, status: EngineRunStatus, attempt: int) -> JsonDict:
    stats = status.live_stats or LiveStats()
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "type": "engine_poll",
        "phase": _PHASE.value,
        "attempt": attempt,
        "engine": handle.engine,
        "external_run_id": handle.external_run_id,
        "status": status.phase.value,
        "progress_pct": status.progress_pct,
        "live_stats": {
            "vusers": stats.vusers,
            "tps": stats.tps,
            "error_rate": stats.error_rate,
            "p95_ms": stats.p95_ms,
        },
    }


def _poll_sample(status: EngineRunStatus) -> JsonDict:
    """Compact rolling sample for the phase entry (full series stays in events)."""
    sample: JsonDict = {
        "at": utcnow_iso(),
        "status": status.phase.value,
        "progress_pct": status.progress_pct,
    }
    if status.live_stats is not None:
        sample["live_stats"] = status.live_stats.model_dump(mode="json")
    if status.message:
        sample["message"] = status.message
    return sample


# ── nodes ──────────────────────────────────────────────────────────────────────


def _build_spec(
    state: PipelineState, config: RunnableConfig, attempt: int
) -> tuple[LoadTestSpec, JsonDict]:
    """Spec for this run: script_scenario output (or a default for standalone
    execution runs) + per-run "load_test" overrides + the authoritative key."""
    upstream = (state.get("phase_results") or {}).get(Phase.SCRIPT_SCENARIO.value) or {}
    raw = upstream.get("load_test_spec")
    base: JsonDict
    if isinstance(raw, dict) and raw:
        base = dict(raw)
    else:
        base = {
            "title": f"{state.get('title') or 'untitled run'} load test",
            "vusers": 10,
            "ramp_s": 1.0,
        }
    overrides = dict((config or {}).get("configurable") or {}).get("load_test")
    overrides = dict(overrides) if isinstance(overrides, dict) else {}
    spec_overrides = {k: v for k, v in overrides.items() if k in _SPEC_OVERRIDE_FIELDS}
    engine_options = {k: v for k, v in overrides.items() if k not in _SPEC_OVERRIDE_FIELDS}
    base.update(spec_overrides)
    base["idempotency_key"] = execution_idempotency_key(_thread_id(config), attempt)
    return LoadTestSpec.model_validate(base), engine_options


def engine_reserve(state: PipelineState, config: RunnableConfig) -> JsonDict:
    """Write-ahead idempotency: persist spec + engine choice, then RETURN.

    The superstep checkpoint commits this update before engine_start runs, so
    any later crash/re-execution provisions with the same idempotency key.
    """
    cfg = PipelineConfigurable.from_config(config)
    attempt = _attempt(_entry(state))
    spec, engine_options = _build_spec(state, config, attempt)
    engine_runs.record_engine_run_sync(
        _thread_id(config), attempt, cfg.engine, {}, EngineRunPhase.PROVISIONING.value
    )
    return _update(
        attempt,
        load_test_spec=spec.model_dump(mode="json"),
        engine=cfg.engine,
        engine_connection_id=cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
        engine_options=engine_options,
    )


def engine_start(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """validate -> provision (get-or-create by key) -> start; all idempotent."""
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    thread_id = _thread_id(config)
    spec = LoadTestSpec.model_validate(entry.get("load_test_spec") or {})
    engine_options = _engine_options(entry)

    async def _start() -> tuple[list[str], EngineHandle | None, EngineRunStatus | None]:
        adapter = await _resolve_engine(cfg, engine_options)
        report = await adapter.validate(spec)
        if not report.ok:
            return list(report.issues), None, None
        handle = await adapter.provision(spec)
        await adapter.start(handle)
        return [], handle, await adapter.get_status(handle)

    issues, handle, status = asyncio.run(_start())
    if handle is None or status is None:
        errors = [f"engine spec validation failed: {issue}" for issue in issues]
        engine_runs.record_engine_run_sync(
            thread_id, attempt, cfg.engine, {}, EngineRunPhase.FAILED.value
        )
        update = _update(attempt, status=PhaseStatus.FAILED.value, errors=errors)
        return Command(goto="finalize", update=update)

    handle_json = handle.model_dump(mode="json")
    engine_runs.record_engine_run_sync(
        thread_id,
        attempt,
        handle.engine,
        handle_json,
        EngineRunPhase.RUNNING.value,
        external_run_id=handle.external_run_id,
    )
    emit_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "type": "phase_status",
            "phase": _PHASE.value,
            "status": PhaseStatus.RUNNING.value,
            "attempt": attempt,
        }
    )
    emit_event(_poll_event(handle, status, attempt))  # initial tick
    update = _update(
        attempt,
        engine=handle.engine,
        engine_handle=handle_json,
        engine_started_at=utcnow_iso(),
        engine_poll_last=_poll_sample(status),
    )
    update["engine_handle"] = handle_json
    return Command(goto="engine_poll", update=update)


def engine_poll(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """One poll cycle per superstep; self-loops until the engine is terminal.

    The poll count rides the phase entry and is derived from the checkpointed
    value, so node re-execution after a crash never double-counts a cycle.
    """
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)

    async def _poll() -> EngineRunStatus:
        adapter = await _resolve_engine(cfg, engine_options)
        return await adapter.get_status(handle)

    status = asyncio.run(_poll())
    poll_count = int(entry.get("engine_poll_count") or 0) + 1
    emit_event(_poll_event(handle, status, attempt))
    sample = _poll_sample(status)

    if status.phase in TERMINAL_ENGINE_PHASES:
        update = _update(attempt, engine_poll_last=sample, engine_poll_count=poll_count)
        return Command(goto="engine_collect", update=update)

    elapsed = _elapsed_s(entry.get("engine_started_at"))
    if elapsed is not None and elapsed > cfg.limits.poll_timeout_s:
        reason = f"poll timeout after {cfg.limits.poll_timeout_s}s"

        async def _abort_and_teardown() -> None:
            adapter = await _resolve_engine(cfg, engine_options)
            try:
                await adapter.abort(handle, reason=reason)
            finally:
                await adapter.teardown(handle)

        try:
            asyncio.run(_abort_and_teardown())
        except Exception:  # noqa: BLE001 — abort is best-effort; the phase fails anyway
            logger.warning(
                "execution.abort_failed", external_run_id=handle.external_run_id, exc_info=True
            )
        engine_runs.record_engine_run_sync(
            _thread_id(config),
            attempt,
            handle.engine,
            handle.model_dump(mode="json"),
            EngineRunPhase.ABORTED.value,
            external_run_id=handle.external_run_id,
        )
        error = (
            f"execution engine run {handle.external_run_id} timed out after "
            f"{elapsed:.1f}s (limits.poll_timeout_s={cfg.limits.poll_timeout_s}); aborted"
        )
        update = _update(
            attempt,
            status=PhaseStatus.FAILED.value,
            errors=[error],
            engine_poll_last=sample,
            engine_poll_count=poll_count,
        )
        return Command(goto="finalize", update=update)

    time.sleep(cfg.limits.poll_interval_s)
    update = _update(attempt, engine_poll_last=sample, engine_poll_count=poll_count)
    return Command(goto="engine_poll", update=update)


def engine_collect(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Collect artifacts + summary, ALWAYS tear down, and settle the phase status.

    Successful runs continue to the output gate (so reviewers see the summary);
    failed/aborted runs short-circuit to finalize with a terminal status.
    """
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)
    last = dict(entry.get("engine_poll_last") or {})
    try:
        engine_phase = EngineRunPhase(str(last.get("status")))
    except ValueError:
        engine_phase = EngineRunPhase.COMPLETED
    if engine_phase not in TERMINAL_ENGINE_PHASES:
        engine_phase = EngineRunPhase.COMPLETED

    async def _collect() -> tuple[list[JsonDict], TestResultSummary]:
        adapter = await _resolve_engine(cfg, engine_options)
        store = await _resolve_artifact_store(cfg)
        try:
            refs = await adapter.collect_artifacts(handle, store)
            return refs, await adapter.fetch_summary(handle)
        finally:
            await adapter.teardown(handle)

    refs, summary = asyncio.run(_collect())
    artifacts: list[JsonDict] = []
    for index, raw_ref in enumerate(refs):
        ref = dict(raw_ref)
        # Deterministic ids: re-execution after a crash must not duplicate refs
        # under the append-unique-by-id artifacts reducer.
        ref["id"] = f"{_PHASE.value}-a{attempt}-engine-artifact-{index}"
        artifacts.append(ref)

    summary_json = summary.model_dump(mode="json")
    kpi_text = ", ".join(
        f"{key}={value:g}" if isinstance(value, int | float) else f"{key}={value}"
        for key, value in sorted(summary.kpis.items())
    )
    verdict = "passed" if summary.passed else "failed"
    summary_text = (
        f"Engine run {handle.external_run_id} ({handle.engine}) {engine_phase.value}; "
        f"SLA {verdict}. KPIs: {kpi_text or 'none reported'}"
    )

    fields: JsonDict = {
        "summary": summary_text,
        "test_summary": summary_json,
        "artifact_ids": [artifact["id"] for artifact in artifacts],
    }
    goto = "open_output_gate"
    if engine_phase is EngineRunPhase.ABORTED:
        fields["status"] = PhaseStatus.ABORTED.value
        fields["errors"] = [f"engine run {handle.external_run_id} was aborted"]
        goto = "finalize"
    elif engine_phase is EngineRunPhase.FAILED or not summary.passed:
        fields["status"] = PhaseStatus.FAILED.value
        fields["errors"] = list(summary.sla_breaches) or [
            str(last.get("message") or "engine run failed")
        ]
        goto = "finalize"

    engine_runs.record_engine_run_sync(
        _thread_id(config),
        attempt,
        handle.engine,
        handle.model_dump(mode="json"),
        engine_phase.value,
        external_run_id=handle.external_run_id,
        summary=summary_json,
    )
    update = _update(attempt, **fields)
    update["artifacts"] = artifacts
    return Command(goto=goto, update=update)


# ── wiring ─────────────────────────────────────────────────────────────────────


def _enter_engine_spine(state: PipelineState, config: RunnableConfig) -> JsonDict:
    """Gate-compat alias: the untouched gate nodes route Command(goto="agent");
    for the execution phase that target is this no-op flowing into engine_reserve."""
    return {}


def add_execution_engine_nodes(builder: StateGraph[PipelineState, Any, Any, Any]) -> None:
    """Wire the engine spine into the execution phase builder in place of `agent`.

    Callers must still add the shared spine edges (open_output_gate -> output_gate,
    finalize -> END etc.); this only contributes the agent-alias + engine nodes.
    """
    builder.add_node("agent", _enter_engine_spine)
    builder.add_node("engine_reserve", engine_reserve)
    builder.add_node("engine_start", engine_start, destinations=("engine_poll", "finalize"))
    builder.add_node(
        "engine_poll", engine_poll, destinations=("engine_poll", "engine_collect", "finalize")
    )
    builder.add_node(
        "engine_collect", engine_collect, destinations=("open_output_gate", "finalize")
    )
    builder.add_edge("agent", "engine_reserve")
    builder.add_edge("engine_reserve", "engine_start")


__all__ = [
    "SPINE_SUPERSTEPS",
    "add_execution_engine_nodes",
    "engine_collect",
    "engine_poll",
    "engine_reserve",
    "engine_start",
    "execution_idempotency_key",
    "recommended_recursion_limit",
]
