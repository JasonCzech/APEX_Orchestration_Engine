"""Execution-phase engine spine with checkpointed external side-effect boundaries.

Replaces the stub `agent` node between the prompt and output gates for
Phase.EXECUTION (wired by make_phase_subgraph; the gate nodes are untouched and
keep routing to the node name "agent", which here is a no-op alias flowing into
engine_reserve). Durability rules (plan "Durability & the execution phase"):

- engine_reserve writes the LoadTestSpec — with the deterministic idempotency key
  ``{thread_id}-execution-a{attempt}`` — into graph state and returns, so the
  checkpoint commits BEFORE any engine side effect. A crash after the checkpoint
  can only re-issue the same get-or-create call.
- engine_provision resolves and provisions by key, then returns the durable
  EngineHandle before engine_start can issue the remote start side effect.
- engine_start and engine_status are separate supersteps. A status failure after
  start therefore still has a checkpointed handle available to abort/recover.
- engine_poll is a self-loop (Command goto back to itself): one superstep — one
  checkpoint plus one ``engine_poll`` custom event — per cycle, with the
  inter-cycle sleep inside the node. A server restart resumes polling from the
  durable EngineHandle, losing at most one cycle.
- engine_cleanup is a durable self-loop: once a started run needs aborting, the
  cleanup intent is checkpointed before the kill is attempted. Provider failures
  remain non-terminal and retry from the same handle; only a successful abort may
  project ABORTED and finalize the phase.
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
from datetime import UTC, datetime
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import StateGraph
from langgraph.types import Command

from apex.adapters.registry import PortKind
from apex.domain.integrations import LoadTestSpec, TestResultSummary
from apex.domain.pipeline import EngineHandle, Phase, PhaseStatus, utcnow_iso
from apex.graphs.pipeline.configurable import (
    MAX_RECOMMENDED_RECURSION_LIMIT,
    Limits,
    PipelineConfigurable,
)
from apex.graphs.pipeline.phase_subgraph import EVENT_SCHEMA_VERSION, emit_event
from apex.graphs.pipeline.state import JsonDict, PipelineState
from apex.ports.artifact_store import engine_artifact_namespace
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineRunPhase,
    EngineRunStatus,
    LiveStats,
)
from apex.services import engine_runs
from apex.services.connections import ConnectionResolver, DbConnectionStore, close_adapter

logger = structlog.get_logger(__name__)

_PHASE = Phase.EXECUTION

# Supersteps the execution subgraph consumes outside the poll loop (prepare, gate
# nodes, agent alias, reserve/start/collect, finalize) plus parent-spine slack.
SPINE_SUPERSTEPS = 19
MAX_CONSECUTIVE_POLL_ERRORS = 3

# LoadTestSpec fields a per-run "load_test" configurable dict may override; any
# other key (e.g. the sim engine's fail_at_pct) is treated as an engine option.
_SPEC_OVERRIDE_FIELDS = frozenset(LoadTestSpec.model_fields) - {
    "idempotency_key",
    # Targets are server-resolved from configurable.environment_id. Accepting
    # this through load_test would turn the engine into a caller-controlled SSRF proxy.
    "target_environment",
    # Provider-owned workload selectors can carry a target of their own and must
    # be selected by an approved connection/catalog binding, never by a run.
    "script_refs",
}
_ENGINE_OPTION_FIELDS = {
    "apex_load": frozenset[str](),
    "loadrunner": frozenset({"abortive_stop"}),
    "sim": frozenset({"fail_at_pct"}),
}


def recommended_recursion_limit(limits: Limits) -> int:
    """Recursion-limit hint for runs that include the execution phase."""
    # Revalidate even if a caller manufactured a model with model_construct().
    checked = Limits.model_validate(limits.model_dump(mode="python"))
    cycles = math.ceil(checked.poll_timeout_s / checked.poll_interval_s)
    # Reserve a second full polling window for durable asynchronous abort
    # confirmation. Cleanup no longer competes with the normal poll loop for the
    # same recursion budget.
    return min(cycles * 2 + SPINE_SUPERSTEPS + 25, MAX_RECOMMENDED_RECURSION_LIMIT)


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
    thread_id = str(configurable.get("thread_id") or "").strip()
    if not thread_id:
        raise ValueError(
            "execution phase requires a durable thread_id; stateless execution is not allowed"
        )
    return thread_id


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


async def _resolve_engine(
    cfg: PipelineConfigurable,
    engine_options: JsonDict,
    *,
    connection_id: str | None = None,
) -> Any:
    """Resolve the execution-engine adapter.

    Resolve the selected stored connection and overlay only the validated per-run
    options. The base URL/domain/project/secret and durable connection id remain
    those of the stored connection, so a later kill switch can resolve it.
    """
    adapter, _connection_id = await _make_resolver().resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        connection_id=connection_id or cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
        project_id=cfg.project_id,
        expected_provider=cfg.engine,
        options_overlay=engine_options,
    )
    return adapter


def _config_for_handle(cfg: PipelineConfigurable, handle: EngineHandle) -> PipelineConfigurable:
    """Pin adapter resolution to the durable handle while preserving test seams."""
    if not handle.connection_id:
        return cfg
    connections = dict(cfg.connections)
    connections[PortKind.EXECUTION_ENGINE.value] = handle.connection_id
    return cfg.model_copy(update={"connections": connections, "engine": handle.engine})


async def _resolve_artifact_store(cfg: PipelineConfigurable) -> tuple[Any, str]:
    return await _make_resolver().resolve_with_connection_id(
        PortKind.ARTIFACT_STORE,
        connection_id=cfg.connections.get(PortKind.ARTIFACT_STORE.value),
        project_id=cfg.project_id,
    )


async def _resolve_catalog_target(cfg: PipelineConfigurable) -> str | None:
    """Revalidate the stamped target without allowing gated-run target drift."""

    if not cfg.environment_id:
        return None
    from apex.persistence.db import get_sessionmaker
    from apex.persistence.repositories.catalog import CatalogRepository
    from apex.services.environments import resolve_environment_target

    async with get_sessionmaker()() as session:
        target = await resolve_environment_target(
            CatalogRepository(session),
            cfg.environment_id,
            project_id=cfg.project_id,
            app_id=cfg.app_id,
        )
    return _verified_stamped_target(cfg, target.base_url, target.version)


def _verified_stamped_target(
    cfg: PipelineConfigurable,
    current_target: str,
    current_version: int,
) -> str:
    if cfg.environment_target is None or cfg.environment_target_version is None:
        raise ValueError(
            "environment target was not authorized and stamped at run creation; "
            "select environment_id in the run configuration"
        )
    if (
        current_target != cfg.environment_target
        or current_version != cfg.environment_target_version
    ):
        raise ValueError("approved environment target changed after run creation; create a new run")
    return cfg.environment_target


async def _close_resource(resource: Any) -> None:
    """Close a throwaway adapter/client without masking the node outcome."""
    try:
        await close_adapter(resource)
    except Exception:  # noqa: BLE001 - resource cleanup is best-effort
        logger.warning("execution.adapter_close_failed", exc_info=True)


async def _teardown_after_confirmed_abort(adapter: Any, handle: EngineHandle) -> None:
    """Release provider resources after a successful, idempotent abort call.

    Teardown failure can leak a temporary provider resource, but it cannot leave
    load running once ``abort`` has returned successfully. It therefore remains
    best-effort while the abort itself is retried durably by ``engine_cleanup``.
    """

    try:
        await adapter.teardown(handle)
    except Exception:  # noqa: BLE001 - abort success is the safety boundary
        logger.warning(
            "execution.teardown_failed",
            external_run_id=handle.external_run_id,
            exc_info=True,
        )


async def _confirm_abort(adapter: Any, handle: EngineHandle, reason: str) -> EngineRunPhase:
    await adapter.abort(handle, reason=reason)
    try:
        status = await adapter.get_status(handle)
    except KeyError:
        # A definitive not-found means the provider has already discarded it.
        return EngineRunPhase.ABORTED
    if status.phase not in TERMINAL_ENGINE_PHASES:
        raise RuntimeError(f"abort accepted but external run remains {status.phase.value}")
    return status.phase


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
    state: PipelineState,
    config: RunnableConfig,
    attempt: int,
    engine: str,
    *,
    target_environment: str | None = None,
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
    cfg = PipelineConfigurable.from_config(config)
    overrides = cfg.load_test
    spec_overrides = {k: v for k, v in overrides.items() if k in _SPEC_OVERRIDE_FIELDS}
    engine_options = {k: v for k, v in overrides.items() if k not in _SPEC_OVERRIDE_FIELDS}
    _validate_engine_options(engine, engine_options)
    base.update(spec_overrides)
    # The script-scenario phase is model-authored input. Provider workload IDs are
    # security-sensitive because their stored definitions may target other hosts.
    base.pop("script_refs", None)
    # Ignore any caller-seeded upstream target; only the auth-resolved immutable
    # run target may reach an execution adapter.
    base["target_environment"] = target_environment
    base["idempotency_key"] = execution_idempotency_key(_thread_id(config), attempt)
    spec = LoadTestSpec.model_validate(base)
    if engine == "apex_load" and any(ref.lstrip().startswith("{") for ref in spec.script_refs):
        raise ValueError(
            "inline Apex Load script_refs are not allowed for pipeline runs; "
            "use the catalog-resolved default target or a provider-owned named script"
        )
    return spec, engine_options


def _validate_engine_options(engine: str, engine_options: JsonDict) -> None:
    if not engine_options:
        return
    allowed = _ENGINE_OPTION_FIELDS.get(engine, frozenset())
    unsupported = sorted(set(engine_options) - allowed)
    if unsupported:
        raise ValueError(
            f"unsupported load_test engine option(s) for {engine!r}: {', '.join(unsupported)}"
        )


def engine_reserve(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Write-ahead idempotency: persist spec + engine choice, then RETURN.

    The superstep checkpoint commits this update before engine_provision runs, so
    any later crash/re-execution provisions with the same idempotency key.
    """
    cfg = PipelineConfigurable.from_config(config)
    attempt = _attempt(_entry(state))
    try:
        target_environment = asyncio.run(_resolve_catalog_target(cfg))
        spec, engine_options = _build_spec(
            state,
            config,
            attempt,
            cfg.engine,
            target_environment=target_environment,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on catalog/config errors
        return Command(
            goto="finalize",
            update=_update(
                attempt,
                status=PhaseStatus.FAILED.value,
                engine=cfg.engine,
                errors=[f"load_test validation failed: {exc}"],
            ),
        )
    return Command(
        goto="engine_provision",
        update=_update(
            attempt,
            load_test_spec=spec.model_dump(mode="json"),
            engine=cfg.engine,
            engine_connection_id=cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
            engine_options=engine_options,
        ),
    )


def engine_provision(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Validate and provision idempotently; checkpoint the handle before start."""
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    thread_id = _thread_id(config)
    spec = LoadTestSpec.model_validate(entry.get("load_test_spec") or {})
    engine_options = _engine_options(entry)

    async def _provision() -> tuple[list[str], EngineHandle | None]:
        adapter = await _resolve_engine(cfg, engine_options)
        try:
            report = await adapter.validate(spec)
            if not report.ok:
                return list(report.issues), None
            return [], await adapter.provision(spec)
        finally:
            await _close_resource(adapter)

    try:
        issues, handle = asyncio.run(_provision())
    except Exception as exc:  # noqa: BLE001 - settle the phase instead of crashing the graph
        issues, handle = [f"engine provisioning failed: {exc}"], None
    if handle is None:
        errors = [f"engine spec validation failed: {issue}" for issue in issues]
        engine_runs.record_engine_run_sync(
            thread_id,
            attempt,
            cfg.engine,
            {},
            EngineRunPhase.FAILED.value,
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            artifact_namespace=engine_artifact_namespace(spec.idempotency_key),
        )
        update = _update(attempt, status=PhaseStatus.FAILED.value, errors=errors)
        return Command(goto="finalize", update=update)

    handle_json = handle.model_dump(mode="json")
    engine_runs.record_engine_run_sync(
        thread_id,
        attempt,
        handle.engine,
        handle_json,
        EngineRunPhase.PROVISIONING.value,
        project_id=cfg.project_id,
        app_id=cfg.app_id,
        external_run_id=handle.external_run_id,
        artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
    )
    update = _update(
        attempt,
        engine=handle.engine,
        engine_connection_id=handle.connection_id,
        engine_handle=handle_json,
    )
    update["engine_handle"] = handle_json
    return Command(goto="engine_start", update=update)


def engine_start(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Start a previously checkpointed handle, then checkpoint before status IO."""
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)

    async def _start() -> tuple[str | None, JsonDict]:
        adapter = await _resolve_engine(_config_for_handle(cfg, handle), engine_options)
        try:
            try:
                await adapter.start(handle)
            except Exception as exc:  # noqa: BLE001
                return str(exc), handle.model_dump(mode="json")
            return None, handle.model_dump(mode="json")
        finally:
            await _close_resource(adapter)

    try:
        error, handle_json = asyncio.run(_start())
    except Exception as exc:  # resolver/build failures are also terminal
        error, handle_json = str(exc), handle.model_dump(mode="json")
    handle = EngineHandle.model_validate(handle_json)
    if error is not None:
        reason = f"start failed: {error}"
        update = _update(
            attempt,
            engine_handle=handle_json,
            engine_cleanup_required=True,
            engine_cleanup_reason=reason,
            engine_cleanup_final_error=f"execution engine start failed: {error}",
            engine_cleanup_failures=0,
        )
        update["engine_handle"] = handle_json
        return Command(
            goto="engine_cleanup",
            update=update,
        )

    engine_runs.record_engine_run_sync(
        _thread_id(config),
        attempt,
        handle.engine,
        handle.model_dump(mode="json"),
        EngineRunPhase.RUNNING.value,
        project_id=cfg.project_id,
        app_id=cfg.app_id,
        external_run_id=handle.external_run_id,
        artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
    )
    update = _update(
        attempt,
        engine_handle=handle_json,
        engine_started_at=utcnow_iso(),
    )
    update["engine_handle"] = handle_json
    return Command(goto="engine_status", update=update)


def engine_status(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Fetch initial status, checkpointing transient errors for the poll loop."""
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)

    async def _status() -> EngineRunStatus:
        adapter = await _resolve_engine(_config_for_handle(cfg, handle), engine_options)
        try:
            return await adapter.get_status(handle)
        finally:
            await _close_resource(adapter)

    try:
        status = asyncio.run(_status())
    except Exception as exc:  # noqa: BLE001 - poll loop owns bounded recovery
        failures = int(entry.get("engine_poll_errors") or 0) + 1
        message = (
            "execution engine initial status failed "
            f"({failures}/{MAX_CONSECUTIVE_POLL_ERRORS}): {exc}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_poll_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "error": str(exc),
                "consecutive_errors": failures,
            }
        )
        # The checkpointed handle proves the external run was started. A single
        # status transport failure must not kill it; the normal poll node resumes
        # from this error count and performs cleanup only after the bounded cap.
        return Command(
            goto="engine_poll",
            update=_update(
                attempt,
                engine_poll_errors=failures,
                engine_poll_error_last=message,
            ),
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
    emit_event(_poll_event(handle, status, attempt))
    update = _update(
        attempt,
        engine_poll_last=_poll_sample(status),
        engine_poll_errors=0,
    )
    goto = "engine_collect" if status.phase in TERMINAL_ENGINE_PHASES else "engine_poll"
    return Command(goto=goto, update=update)


async def _engine_poll_async(state: PipelineState, config: RunnableConfig) -> Command[str]:
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
        adapter = await _resolve_engine(_config_for_handle(cfg, handle), engine_options)
        try:
            return await adapter.get_status(handle)
        finally:
            await _close_resource(adapter)

    try:
        status = await _poll()
    except Exception as exc:  # noqa: BLE001 - bounded retry before remote cleanup
        failures = int(entry.get("engine_poll_errors") or 0) + 1
        message = f"execution engine poll failed ({failures}/{MAX_CONSECUTIVE_POLL_ERRORS}): {exc}"
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_poll_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "error": str(exc),
                "consecutive_errors": failures,
            }
        )
        if failures < MAX_CONSECUTIVE_POLL_ERRORS:
            await asyncio.sleep(cfg.limits.poll_interval_s)
            return Command(
                goto="engine_poll",
                update=_update(
                    attempt,
                    engine_poll_errors=failures,
                    engine_poll_error_last=message,
                ),
            )

        return Command(
            goto="engine_cleanup",
            update=_update(
                attempt,
                engine_poll_errors=failures,
                engine_cleanup_required=True,
                engine_cleanup_reason=message,
                engine_cleanup_final_error=message,
                engine_cleanup_failures=0,
            ),
        )

    poll_count = int(entry.get("engine_poll_count") or 0) + 1
    emit_event(_poll_event(handle, status, attempt))
    sample = _poll_sample(status)

    if status.phase in TERMINAL_ENGINE_PHASES:
        update = _update(
            attempt,
            engine_poll_last=sample,
            engine_poll_count=poll_count,
            engine_poll_errors=0,
        )
        return Command(goto="engine_collect", update=update)

    elapsed = _elapsed_s(entry.get("engine_started_at"))
    if elapsed is not None and elapsed > cfg.limits.poll_timeout_s:
        reason = f"poll timeout after {cfg.limits.poll_timeout_s}s"

        error = (
            f"execution engine run {handle.external_run_id} timed out after "
            f"{elapsed:.1f}s (limits.poll_timeout_s={cfg.limits.poll_timeout_s})"
        )
        update = _update(
            attempt,
            engine_poll_last=sample,
            engine_poll_count=poll_count,
            engine_cleanup_required=True,
            engine_cleanup_reason=reason,
            engine_cleanup_final_error=error,
            engine_cleanup_failures=0,
        )
        return Command(goto="engine_cleanup", update=update)

    await asyncio.sleep(cfg.limits.poll_interval_s)
    update = _update(
        attempt,
        engine_poll_last=sample,
        engine_poll_count=poll_count,
        engine_poll_errors=0,
    )
    return Command(goto="engine_poll", update=update)


def engine_poll(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Synchronous compatibility path for local ``graph.invoke`` callers.

    The LangGraph server drives the Runnable's async path below, where poll waits
    use ``asyncio.sleep`` and do not occupy worker threads.
    """
    return asyncio.run(_engine_poll_async(state, config))


async def _engine_cleanup_async(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Retry a checkpointed external abort until the provider accepts it.

    The node never converts an abort transport/provider failure into a terminal
    graph state. Each failure count and message is checkpointed before the next
    attempt, so a process restart resumes cleanup instead of losing the handle.
    """

    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)
    reason = str(entry.get("engine_cleanup_reason") or "execution cleanup required")

    async def _cleanup() -> EngineRunPhase:
        adapter = await _resolve_engine(_config_for_handle(cfg, handle), engine_options)
        try:
            observed_phase = await _confirm_abort(adapter, handle, reason)
            await _teardown_after_confirmed_abort(adapter, handle)
            return observed_phase
        finally:
            await _close_resource(adapter)

    try:
        observed_phase = await _cleanup()
    except Exception as exc:  # noqa: BLE001 - durable retry is the safety contract
        failures = int(entry.get("engine_cleanup_failures") or 0) + 1
        logger.warning(
            "execution.cleanup_retry",
            external_run_id=handle.external_run_id,
            failures=failures,
            error=str(exc),
        )
        cleanup_budget = max(
            3,
            math.ceil(cfg.limits.poll_timeout_s / cfg.limits.poll_interval_s),
        )
        if failures >= cleanup_budget:
            raise RuntimeError(
                "external engine abort is still unconfirmed after the cleanup retry budget; "
                "the durable handle remains checkpointed for operator resume"
            ) from exc
        await asyncio.sleep(cfg.limits.poll_interval_s)
        return Command(
            goto="engine_cleanup",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_cleanup_required=True,
                engine_cleanup_failures=failures,
                engine_cleanup_last_error=str(exc),
            ),
        )

    engine_runs.record_engine_run_sync(
        _thread_id(config),
        attempt,
        handle.engine,
        handle.model_dump(mode="json"),
        observed_phase.value,
        project_id=cfg.project_id,
        app_id=cfg.app_id,
        external_run_id=handle.external_run_id,
        artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
    )
    final_error = str(entry.get("engine_cleanup_final_error") or reason)
    outcome = (
        "external engine abort confirmed"
        if observed_phase is EngineRunPhase.ABORTED
        else f"external engine reached {observed_phase.value} during cleanup"
    )
    return Command(
        goto="finalize",
        update=_update(
            attempt,
            status=PhaseStatus.FAILED.value,
            errors=[f"{final_error}; {outcome}"],
            engine_cleanup_required=False,
            engine_cleanup_completed_at=utcnow_iso(),
            engine_cleanup_last_error=None,
        ),
    )


def engine_cleanup(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Synchronous compatibility wrapper for local ``graph.invoke`` callers."""

    return asyncio.run(_engine_cleanup_async(state, config))


def route_execution_entry(state: PipelineState) -> str:
    """Resume an unfinished kill before any gate or engine side effect.

    A cleanup self-loop can exhaust a run's recursion budget while the provider
    is unavailable. A later run on the same checkpoint must continue that kill,
    not pass through prompt gates or reserve/start another remote execution.
    """

    if _entry(state).get("engine_cleanup_required"):
        return "engine_cleanup"
    return "prepare"


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
    state_errors: list[str] = []
    raw_engine_phase = last.get("status")
    invalid_collection_state = False
    try:
        engine_phase = EngineRunPhase(str(raw_engine_phase))
    except ValueError:
        engine_phase = EngineRunPhase.FAILED
        invalid_collection_state = True
        state_errors.append(
            "execution collection state is invalid: a terminal engine status is missing "
            f"or malformed (got {raw_engine_phase!r})"
        )
    if engine_phase not in TERMINAL_ENGINE_PHASES:
        invalid_collection_state = True
        state_errors.append(
            "execution collection requires a confirmed terminal engine status; "
            f"got {engine_phase.value!r}"
        )
        engine_phase = EngineRunPhase.FAILED

    if invalid_collection_state:
        reason = "; ".join(state_errors)
        return Command(
            goto="engine_cleanup",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_cleanup_required=True,
                engine_cleanup_reason=reason,
                engine_cleanup_last_error=reason,
            ),
        )

    async def _collect() -> tuple[
        list[JsonDict], TestResultSummary | None, str | None, list[str], list[str]
    ]:
        refs: list[JsonDict] = []
        summary: TestResultSummary | None = None
        artifact_connection_id: str | None = None
        warnings: list[str] = []
        errors: list[str] = []
        adapter: Any | None = None
        store: Any | None = None
        try:
            resolved_adapter = await _resolve_engine(
                _config_for_handle(cfg, handle), engine_options
            )
            adapter = resolved_adapter
            try:
                store, artifact_connection_id = await _resolve_artifact_store(cfg)
                refs = await resolved_adapter.collect_artifacts(handle, store)
            except Exception as exc:  # noqa: BLE001 - artifacts are best-effort
                warnings.append(f"engine artifact collection failed: {exc}")
            try:
                summary = await resolved_adapter.fetch_summary(handle)
            except Exception as exc:  # noqa: BLE001 - settle terminal state below
                errors.append(f"engine summary collection failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - resolution itself may fail
            errors.append(f"engine collection setup failed: {exc}")
        finally:
            if adapter is not None:
                try:
                    await adapter.teardown(handle)
                except Exception as exc:  # noqa: BLE001 - phase must still settle
                    warnings.append(f"engine teardown failed: {exc}")
                await _close_resource(adapter)
            if store is not None:
                await _close_resource(store)
        return refs, summary, artifact_connection_id, warnings, errors

    refs, summary, artifact_connection_id, collection_warnings, collection_errors = asyncio.run(
        _collect()
    )
    artifacts: list[JsonDict] = []
    for index, raw_ref in enumerate(refs):
        ref = dict(raw_ref)
        # Deterministic ids: re-execution after a crash must not duplicate refs
        # under the append-unique-by-id artifacts reducer.
        ref["id"] = f"{_PHASE.value}-a{attempt}-engine-artifact-{index}"
        ref["artifact_connection_id"] = artifact_connection_id
        artifacts.append(ref)

    collection_errors = [*state_errors, *collection_errors]
    if summary is None:
        summary = TestResultSummary(
            engine=handle.engine,
            passed=False,
            notes="; ".join(collection_errors) or "engine summary unavailable",
        )
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
        "artifact_namespace": engine_artifact_namespace(handle.idempotency_key),
        "artifact_store_connection_id": artifact_connection_id,
    }
    if collection_warnings:
        fields["warnings"] = collection_warnings
    goto = "open_output_gate"
    if engine_phase is EngineRunPhase.ABORTED:
        fields["status"] = PhaseStatus.ABORTED.value
        fields["errors"] = [
            f"engine run {handle.external_run_id} was aborted",
            *collection_errors,
        ]
        goto = "finalize"
    elif engine_phase is EngineRunPhase.FAILED or not summary.passed:
        fields["status"] = PhaseStatus.FAILED.value
        phase_errors = list(summary.sla_breaches)
        if not phase_errors and not state_errors:
            phase_errors = [str(last.get("message") or "engine run failed")]
        fields["errors"] = [*phase_errors, *collection_errors]
        goto = "finalize"
    elif collection_errors:
        fields["status"] = PhaseStatus.FAILED.value
        fields["errors"] = collection_errors
        goto = "finalize"

    projected_phase = (
        EngineRunPhase.FAILED if fields.get("status") == PhaseStatus.FAILED.value else engine_phase
    )
    engine_runs.record_engine_run_sync(
        _thread_id(config),
        attempt,
        handle.engine,
        handle.model_dump(mode="json"),
        projected_phase.value,
        project_id=cfg.project_id,
        app_id=cfg.app_id,
        external_run_id=handle.external_run_id,
        summary=summary_json,
        artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
        artifact_connection_id=artifact_connection_id,
        required=True,
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
    builder.add_node(
        "engine_reserve", engine_reserve, destinations=("engine_provision", "finalize")
    )
    builder.add_node(
        "engine_provision", engine_provision, destinations=("engine_start", "finalize")
    )
    builder.add_node("engine_start", engine_start, destinations=("engine_status", "engine_cleanup"))
    builder.add_node(
        "engine_status", engine_status, destinations=("engine_poll", "engine_collect", "finalize")
    )
    builder.add_node(
        "engine_poll",
        RunnableLambda(engine_poll, afunc=_engine_poll_async, name="engine_poll"),
        destinations=("engine_poll", "engine_collect", "engine_cleanup"),
    )
    builder.add_node(
        "engine_cleanup",
        RunnableLambda(engine_cleanup, afunc=_engine_cleanup_async, name="engine_cleanup"),
        destinations=("engine_cleanup", "finalize"),
    )
    builder.add_node(
        "engine_collect", engine_collect, destinations=("open_output_gate", "finalize")
    )
    builder.add_edge("agent", "engine_reserve")


__all__ = [
    "SPINE_SUPERSTEPS",
    "add_execution_engine_nodes",
    "engine_cleanup",
    "engine_collect",
    "engine_poll",
    "engine_provision",
    "engine_reserve",
    "engine_start",
    "engine_status",
    "execution_idempotency_key",
    "recommended_recursion_limit",
    "route_execution_entry",
]
