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
- engine_collect persists artifacts + summary into graph state; only the following
  checkpoint-gated settle node may tear down the run and project it terminal.

recursion_limit sizing: every poll cycle consumes one superstep inside the
execution subgraph, so runs containing the execution phase must budget roughly

    ceil(limits.poll_timeout_s / limits.poll_interval_s) + SPINE_SUPERSTEPS + headroom

LangGraph's default (25) only fits long production poll intervals; tests and
demos with tiny intervals must pass ``config={"recursion_limit": ...}`` — see
recommended_recursion_limit().
"""

import asyncio
import math
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.integrations import LoadTestSpec, TestResultSummary, ValidationReport
from apex.domain.pipeline import (
    ArtifactRef,
    EngineConnectionAffinityMissingError,
    EngineHandle,
    Phase,
    PhaseStatus,
    utcnow_iso,
)
from apex.graphs.pipeline.configurable import (
    MAX_RECOMMENDED_RECURSION_LIMIT,
    Limits,
    PipelineConfigurable,
)
from apex.graphs.pipeline.phase_subgraph import EVENT_SCHEMA_VERSION, emit_event
from apex.graphs.pipeline.state import JsonDict, PipelineState
from apex.ports.artifact_store import StoredArtifact, engine_artifact_namespace
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineProviderRunNotFoundError,
    EngineRunPhase,
    EngineRunStatus,
    LiveStats,
)
from apex.services import engine_runs
from apex.services.artifact_references import (
    ArtifactReferenceInput,
    record_artifact_references,
)
from apex.services.connections import (
    ConnectionResolver,
    DbConnectionStore,
    ResolvedAdapter,
    close_adapter,
)
from apex.settings import get_settings

logger = structlog.get_logger(__name__)

_PHASE = Phase.EXECUTION

# Supersteps the execution subgraph consumes outside the poll loop (prepare, gate
# nodes, agent alias, reserve/start/collect, finalize) plus parent-spine slack.
# Includes execution/artifact-store affinity reservations, bounded provision and
# collection retries, the durable collection-settle split, explicit blocked nodes,
# and the resume paths used by later operator-triggered runs.
SPINE_SUPERSTEPS = 34
MAX_CONSECUTIVE_POLL_ERRORS = 3
MAX_ENGINE_PROVISION_ATTEMPTS = 3
MAX_ENGINE_COLLECTION_ATTEMPTS = 3
MAX_ENGINE_SETTLE_ATTEMPTS = 3
MAX_ENGINE_ARTIFACT_REFS = 32
_ENGINE_ARTIFACT_KINDS = frozenset({"engine_results", "engine_report"})


class _RequiredArtifactIndexError(RuntimeError):
    """Collected objects cannot become checkpoint-visible without exact ownership."""


class _DefinitiveProvisionError(RuntimeError):
    """Provisioning cannot safely continue with the checkpointed reservation."""


class _EngineArtifactStoreView:
    """Attempt-scoped, write-only facade for execution-engine artifact output."""

    def __init__(self, store: Any, namespace: str) -> None:
        self._store = store
        self._namespace = namespace
        self._prefix = f"{namespace}/"
        self.written: dict[str, tuple[StoredArtifact, str]] = {}

    def _key(self, key: str) -> str:
        if (
            not isinstance(key, str)
            or not key.startswith(self._prefix)
            or key == self._prefix
            or len(key) > 1_024
            or "\x00" in key
            or "\\" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ValueError(
                f"execution engine artifact key must remain beneath {self._namespace!r}"
            )
        return key

    @staticmethod
    def _content_type(content_type: str) -> str:
        if (
            not isinstance(content_type, str)
            or not content_type
            or len(content_type) > 255
            or any(char in content_type for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("execution engine artifact content type is invalid")
        return content_type

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        checked_key = self._key(key)
        stored = StoredArtifact.model_validate(
            _model_payload(
                await self._store.put(
                    checked_key,
                    data,
                    content_type=self._content_type(content_type),
                )
            )
        )
        if stored.key != checked_key:
            raise ValueError("artifact store returned a key different from the requested key")
        self.written[checked_key] = (stored.model_copy(deep=True), content_type)
        return stored

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("execution engine artifact max_bytes must be a positive integer")
        checked_key = self._key(key)
        stored = StoredArtifact.model_validate(
            _model_payload(
                await self._store.put_stream(
                    checked_key,
                    data,
                    content_type=self._content_type(content_type),
                    max_bytes=max_bytes,
                )
            )
        )
        if stored.key != checked_key or stored.size < 0 or stored.size > max_bytes:
            raise ValueError("artifact store returned an invalid streamed artifact")
        self.written[checked_key] = (stored.model_copy(deep=True), content_type)
        return stored

    async def get(self, key: str) -> bytes:
        self._key(key)
        raise RuntimeError("execution engine artifact collection is write-only")

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        del chunk_size
        self._key(key)
        raise RuntimeError("execution engine artifact collection is write-only")

    async def get_url(self, key: str, *, ttl_s: int = 3600) -> str:
        del ttl_s
        self._key(key)
        raise RuntimeError("execution engine artifact collection is write-only")

    async def delete(self, key: str) -> None:
        self._key(key)
        raise RuntimeError("execution engine artifact collection cannot delete objects")


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
    # test identifiers remain accepted here for trusted persisted checkpoints;
    # new public runs are denied at the REST/LangGraph authorization boundaries.
    "loadrunner": frozenset({"abortive_stop", "test_id", "test_instance_id"}),
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
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (datetime.now(UTC) - started).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _connection_reservation_affinity(
    entry: JsonDict, handle: EngineHandle
) -> tuple[str | None, datetime | None]:
    """Revalidate the checkpointed execution affinity before provider I/O."""

    if entry.get("engine_connection_affinity_staged") is not True:
        if get_settings().is_locked_down:
            # A connection id alone is not a durable provider binding: operators
            # can edit that row to point at another endpoint while an old run is
            # still live.  Legacy checkpoints have no runtime generation to fence
            # such edits, so every provider operation must fail closed instead of
            # resolving today's contents of the row (or today's default).
            raise EngineConnectionAffinityMissingError
        # Compatibility for handles checkpointed before explicit connection
        # generations were introduced. The existing engine-run row still provides
        # the terminal-state CAS/lease; omitting affinity columns preserves it.
        return None, None
    connection_id = entry.get("engine_connection_id")
    if connection_id != handle.connection_id:
        raise RuntimeError("checkpointed execution handle does not match its connection affinity")
    persisted = entry.get("engine_connection_persisted") is True
    raw_version = entry.get("engine_connection_version")
    if raw_version is None:
        if persisted:
            raise RuntimeError("persisted execution affinity has no checkpointed version")
        return handle.connection_id, None
    if not persisted or not isinstance(raw_version, str):
        raise RuntimeError("checkpointed execution connection version is inconsistent")
    try:
        version = datetime.fromisoformat(raw_version)
    except ValueError as exc:
        raise RuntimeError("checkpointed execution connection version is malformed") from exc
    if version.tzinfo is None:
        raise RuntimeError("checkpointed execution connection version has no timezone")
    return handle.connection_id, version


def _artifact_reservation_affinity(entry: JsonDict, connection_id: str) -> datetime | None:
    """Return the checkpointed artifact-store runtime generation."""

    persisted = entry.get("artifact_store_connection_persisted") is True
    raw_version = entry.get("artifact_store_connection_version")
    if raw_version is None:
        if persisted:
            raise RuntimeError("persisted artifact-store affinity has no checkpointed version")
        return None
    if not persisted or not isinstance(raw_version, str):
        raise RuntimeError("checkpointed artifact-store connection version is inconsistent")
    try:
        version = datetime.fromisoformat(raw_version)
    except ValueError as exc:
        raise RuntimeError("checkpointed artifact-store connection version is malformed") from exc
    if version.tzinfo is None:
        raise RuntimeError("checkpointed artifact-store connection version has no timezone")
    if not connection_id:
        raise RuntimeError("checkpointed artifact-store connection id is missing")
    return version


def _model_payload(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="python") if callable(dump) else value


def _validated_engine_status(value: Any) -> EngineRunStatus:
    """Revalidate even model instances returned by provider adapters."""

    return EngineRunStatus.model_validate(_model_payload(value))


def _validated_engine_summary(
    value: Any, *, expected_engine: str | None = None
) -> TestResultSummary:
    summary = TestResultSummary.model_validate(_model_payload(value))
    if expected_engine is not None and summary.engine != expected_engine:
        raise ValueError("engine summary provider does not match the checkpointed engine")
    return summary


def _validated_engine_handle(value: Any) -> EngineHandle:
    return EngineHandle.model_validate(_model_payload(value))


def _validated_engine_report(value: Any) -> ValidationReport:
    return ValidationReport.model_validate(_model_payload(value))


def _validated_started_handle(value: Any, trusted: EngineHandle) -> EngineHandle:
    """Accept bounded provider-owned start output without permitting affinity drift."""

    candidate = _validated_engine_handle(value)
    return candidate.model_copy(
        update={
            "engine": trusted.engine,
            "connection_id": trusted.connection_id,
            "idempotency_key": trusted.idempotency_key,
        }
    )


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
    resolver = _make_resolver()
    resolve_with_metadata = getattr(resolver, "resolve_with_metadata", None)
    if resolve_with_metadata is not None:
        return await resolve_with_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id=connection_id or cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
            project_id=cfg.project_id,
            expected_provider=cfg.engine,
            options_overlay=engine_options,
        )
    # Compatibility seam for narrow test resolvers and downstream extensions that
    # have not yet adopted structured resolution metadata.
    adapter, _resolved_connection_id = await resolver.resolve_with_connection_id(
        PortKind.EXECUTION_ENGINE,
        connection_id=connection_id or cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
        project_id=cfg.project_id,
        expected_provider=cfg.engine,
        options_overlay=engine_options,
    )
    return adapter


async def _resolve_engine_for_runtime_io(
    cfg: PipelineConfigurable,
    engine_options: JsonDict,
    entry: JsonDict,
    handle: EngineHandle,
    *,
    connection_id: str | None,
    connection_version: datetime | None,
) -> Any:
    """Resolve the exact checkpointed adapter generation before provider I/O.

    Pinning only the connection id is insufficient because a connection row can
    be edited in place.  Structured resolver metadata closes the transaction-to-
    adapter TOCTOU window left by the durable DB reservation barriers.
    """

    resolved = await _resolve_engine(
        _config_for_handle(cfg, handle),
        engine_options,
        connection_id=connection_id or handle.connection_id,
    )
    adapter = resolved.adapter if isinstance(resolved, ResolvedAdapter) else resolved
    try:
        expected_connection_id = connection_id or handle.connection_id
        staged = entry.get("engine_connection_affinity_staged") is True
        expected_persisted = entry.get("engine_connection_persisted") is True
        if isinstance(resolved, ResolvedAdapter):
            if (
                expected_connection_id is not None
                and resolved.connection_id != expected_connection_id
            ):
                raise RuntimeError(
                    "execution resolver did not honor checkpointed connection affinity"
                )
            if staged and (
                resolved.persisted is not expected_persisted
                or resolved.connection_version != connection_version
            ):
                raise RuntimeError(
                    "execution connection changed after its checkpointed affinity reservation"
                )
        else:
            # Narrow test/downstream resolvers predate ResolvedAdapter.  Validate
            # any metadata they expose, but never permit metadata-less resolution
            # for a persisted production generation.
            actual_connection_id = getattr(adapter, "_apex_resolved_connection_id", None)
            actual_connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
            if (
                actual_connection_id is not None
                and expected_connection_id is not None
                and actual_connection_id != expected_connection_id
            ):
                raise RuntimeError(
                    "execution resolver did not honor checkpointed connection affinity"
                )
            if actual_connection_version is not None and (
                not staged or actual_connection_version != connection_version
            ):
                raise RuntimeError(
                    "execution connection changed after its checkpointed affinity reservation"
                )
            if (
                staged
                and expected_persisted
                and get_settings().is_locked_down
                and (actual_connection_id is None or actual_connection_version is None)
            ):
                raise RuntimeError("execution resolver returned no connection-generation metadata")
        return adapter
    except BaseException:
        await _close_resource(adapter)
        raise


def _config_for_handle(cfg: PipelineConfigurable, handle: EngineHandle) -> PipelineConfigurable:
    """Pin adapter resolution to the durable handle while preserving test seams."""
    if not handle.connection_id:
        return cfg
    connections = dict(cfg.connections)
    connections[PortKind.EXECUTION_ENGINE.value] = handle.connection_id
    return cfg.model_copy(update={"connections": connections, "engine": handle.engine})


async def _resolve_artifact_store(
    cfg: PipelineConfigurable, *, connection_id: str | None = None
) -> ResolvedAdapter:
    return await _make_resolver().resolve_with_metadata(
        PortKind.ARTIFACT_STORE,
        connection_id=connection_id or cfg.connections.get(PortKind.ARTIFACT_STORE.value),
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
    return _verified_stamped_target(
        cfg,
        target.base_url,
        target.version,
        current_app_id=target.app_id,
    )


def _verified_stamped_target(
    cfg: PipelineConfigurable,
    current_target: str,
    current_version: int,
    *,
    current_app_id: str,
) -> str:
    if cfg.environment_target is None or cfg.environment_target_version is None:
        raise ValueError(
            "environment target was not authorized and stamped at run creation; "
            "select environment_id in the run configuration"
        )
    if cfg.app_id is None or cfg.app_id != current_app_id:
        raise ValueError(
            "approved environment target application scope does not match the run configuration"
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
    except Exception as exc:  # noqa: BLE001 - resource cleanup is best-effort
        logger.warning("execution.adapter_close_failed", error=bounded_diagnostic(exc))


async def _teardown_after_confirmed_abort(adapter: Any, handle: EngineHandle) -> None:
    """Release provider resources before the durable lease may become terminal.

    ``teardown`` is an idempotent port operation. Propagating failures keeps the
    cleanup intent and execution connection lease nonterminal so a later cleanup
    superstep can retry the same handle instead of leaking provider resources.
    """

    await adapter.teardown(handle)


async def _confirm_abort(
    adapter: Any, trusted_handle: EngineHandle, reason: str
) -> tuple[EngineRunPhase, EngineHandle]:
    provider_handle = trusted_handle.model_copy(deep=True)
    await adapter.abort(provider_handle, reason=reason)
    candidate = _validated_engine_handle(provider_handle)
    # Some legacy engines (including sim) communicate abort state through extras
    # for the immediately-following status check. Preserve only that bounded,
    # revalidated provider-owned field; every durable identity remains trusted.
    followup_handle = candidate.model_copy(
        update={
            "engine": trusted_handle.engine,
            "connection_id": trusted_handle.connection_id,
            "external_run_id": trusted_handle.external_run_id,
            "idempotency_key": trusted_handle.idempotency_key,
        }
    )
    try:
        status = _validated_engine_status(
            await adapter.get_status(followup_handle.model_copy(deep=True))
        )
    except EngineProviderRunNotFoundError:
        # A definitive not-found means the provider has already discarded it.
        return EngineRunPhase.ABORTED, followup_handle
    if status.phase not in TERMINAL_ENGINE_PHASES:
        raise RuntimeError(f"abort accepted but external run remains {status.phase.value}")
    return status.phase, followup_handle


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
        sample["message"] = bounded_diagnostic(status.message)
    return sample


def _validated_engine_artifacts(
    raw_refs: list[Any],
    handle: EngineHandle,
    *,
    written: dict[str, tuple[StoredArtifact, str]] | None = None,
) -> list[JsonDict]:
    """Validate adapter refs before granting them durable ownership/state visibility."""

    if not isinstance(raw_refs, list):
        raise ValueError("engine artifact refs must be a list")
    if len(raw_refs) > MAX_ENGINE_ARTIFACT_REFS:
        raise ValueError(
            f"engine returned {len(raw_refs)} artifact refs; limit is {MAX_ENGINE_ARTIFACT_REFS}"
        )
    namespace = engine_artifact_namespace(handle.idempotency_key)
    prefix = f"{namespace}/"
    validated: list[JsonDict] = []
    seen_keys: set[str] = set()
    for index, raw_ref in enumerate(raw_refs):
        if not isinstance(raw_ref, dict):
            raise ValueError(f"engine artifact ref {index} must be an object")
        ref = ArtifactRef.model_validate(raw_ref)
        if ref.kind not in _ENGINE_ARTIFACT_KINDS:
            raise ValueError(f"engine artifact ref {index} has unsupported kind {ref.kind!r}")
        key = ref.key
        if not key or not key.startswith(prefix) or key == prefix:
            raise ValueError(f"engine artifact ref {index} key must be beneath {namespace!r}")
        if key in seen_keys:
            raise ValueError(f"engine artifact ref {index} duplicates key {key!r}")
        if written is not None:
            stored = written.get(key)
            if stored is None:
                raise ValueError(
                    f"engine artifact ref {index} was not written through the scoped store"
                )
            stored_artifact, content_type = stored
            if ref.uri != stored_artifact.uri:
                raise ValueError(
                    f"engine artifact ref {index} URI does not match the stored object"
                )
            if ref.media_type != content_type:
                raise ValueError(
                    f"engine artifact ref {index} media type does not match the stored object"
                )
        seen_keys.add(key)
        validated.append(ref.model_dump(mode="json"))
    return validated


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
    # `script_refs` is excluded from public overrides, but old trusted assistant
    # checkpoints legitimately contain it. Remove it before provider-option
    # validation so the compatibility branch below is reachable on resume.
    legacy_script_refs = overrides.get("script_refs")
    engine_options = {
        k: v for k, v in overrides.items() if k not in _SPEC_OVERRIDE_FIELDS and k != "script_refs"
    }
    _validate_engine_options(engine, engine_options)
    base.update(spec_overrides)
    # The script-scenario phase is model-authored input. Provider workload IDs are
    # security-sensitive because their stored definitions may target other hosts.
    base.pop("script_refs", None)
    if isinstance(legacy_script_refs, list):
        base["script_refs"] = legacy_script_refs
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
        detail = bounded_diagnostic(exc)
        return Command(
            goto="finalize",
            update=_update(
                attempt,
                status=PhaseStatus.FAILED.value,
                engine=cfg.engine,
                errors=[bounded_diagnostic(f"load_test validation failed: {detail}")],
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
    """Validate and provision idempotently; checkpoint the handle before start.

    Connection affinity is stamped in a separate superstep before provider I/O.
    Ambiguous validate/provision/handle-projection failures retain the nonterminal
    reservation and retry the same idempotency key instead of leaking a remotely
    created resource behind a terminal graph checkpoint.
    """
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    thread_id = _thread_id(config)
    spec = LoadTestSpec.model_validate(entry.get("load_test_spec") or {})
    engine_options = _engine_options(entry)

    def _retry_provision(exc: Exception) -> Command[str]:
        failures = int(entry.get("engine_provision_failures") or 0) + 1
        detail = bounded_diagnostic(exc, max_chars=2_048)
        message = bounded_diagnostic(
            f"engine provisioning failed ({failures}/{MAX_ENGINE_PROVISION_ATTEMPTS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_provision_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "failure": failures,
                "error": detail,
            }
        )
        blocked = failures >= MAX_ENGINE_PROVISION_ATTEMPTS
        return Command(
            goto="engine_provision_blocked" if blocked else "engine_provision",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_provision_required=True,
                engine_provision_blocked=blocked,
                engine_provision_failures=failures,
                engine_provision_last_error=message,
            ),
        )

    raw_connection_id = entry.get("engine_connection_id")
    if raw_connection_id is not None and (
        not isinstance(raw_connection_id, str)
        or not raw_connection_id
        or raw_connection_id != raw_connection_id.strip()
        or len(raw_connection_id) > 256
    ):
        return _retry_provision(
            _DefinitiveProvisionError("checkpointed execution connection id is malformed")
        )
    connection_id = raw_connection_id if isinstance(raw_connection_id, str) else None

    if entry.get("engine_connection_affinity_staged") is not True:

        async def _stage_connection_affinity() -> tuple[str, str | None, bool]:
            resolved = await _resolve_engine(
                cfg,
                engine_options,
                connection_id=connection_id,
            )
            adapter = resolved.adapter if isinstance(resolved, ResolvedAdapter) else resolved
            try:
                if isinstance(resolved, ResolvedAdapter):
                    resolved_connection_id = resolved.connection_id
                    connection_version = resolved.connection_version
                    persisted = resolved.persisted
                else:
                    resolved_connection_id = (
                        getattr(adapter, "_apex_resolved_connection_id", None)
                        or connection_id
                        or cfg.connections.get(PortKind.EXECUTION_ENGINE.value)
                    )
                    connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
                    persisted = connection_version is not None
                if (
                    not isinstance(resolved_connection_id, str)
                    or not resolved_connection_id
                    or resolved_connection_id != resolved_connection_id.strip()
                    or len(resolved_connection_id) > 256
                ):
                    raise RuntimeError("resolved execution connection has no valid durable id")
                if connection_id is not None and resolved_connection_id != connection_id:
                    raise RuntimeError(
                        "execution resolver did not honor checkpointed connection affinity"
                    )
                if connection_version is not None and not isinstance(connection_version, datetime):
                    raise RuntimeError("resolved execution connection has an invalid version")
                if persisted and connection_version is None:
                    raise RuntimeError(
                        f"persisted execution connection {resolved_connection_id!r} has no version"
                    )
                return (
                    resolved_connection_id,
                    connection_version.isoformat() if connection_version is not None else None,
                    persisted,
                )
            finally:
                await _close_resource(adapter)

        try:
            staged_connection_id, staged_version, staged_persisted = asyncio.run(
                _stage_connection_affinity()
            )
        except Exception as exc:  # noqa: BLE001 - bounded same-key recovery
            return _retry_provision(exc)
        # No provider validation/provision call is allowed in this superstep. A
        # retry therefore cannot drift to a newly selected project/global default.
        return Command(
            goto="engine_provision",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_connection_id=staged_connection_id,
                engine_connection_version=staged_version,
                engine_connection_persisted=staged_persisted,
                engine_connection_affinity_staged=True,
                engine_provision_required=True,
                engine_provision_blocked=False,
                engine_provision_failures=int(entry.get("engine_provision_failures") or 0),
            ),
        )

    if connection_id is None:
        return _retry_provision(
            _DefinitiveProvisionError("staged execution connection affinity is missing")
        )
    expected_version = entry.get("engine_connection_version")
    if expected_version is not None and not isinstance(expected_version, str):
        return _retry_provision(
            _DefinitiveProvisionError("staged execution connection version is malformed")
        )
    expected_persisted = entry.get("engine_connection_persisted") is True

    async def _provision() -> tuple[list[str], EngineHandle | None, bool]:
        resolved = await _resolve_engine(
            cfg,
            engine_options,
            connection_id=connection_id,
        )
        adapter = resolved.adapter if isinstance(resolved, ResolvedAdapter) else resolved
        try:
            if isinstance(resolved, ResolvedAdapter):
                resolved_connection_id = resolved.connection_id
                connection_version = resolved.connection_version
                persisted = resolved.persisted
            else:
                # Compatibility for test/during-upgrade resolvers. Production
                # ConnectionResolver always returns structured metadata.
                resolved_connection_id = (
                    getattr(adapter, "_apex_resolved_connection_id", None) or connection_id
                )
                connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
                persisted = connection_version is not None
            actual_version = (
                connection_version.isoformat() if isinstance(connection_version, datetime) else None
            )
            if (
                resolved_connection_id != connection_id
                or persisted is not expected_persisted
                or actual_version != expected_version
            ):
                raise _DefinitiveProvisionError(
                    "execution connection changed after its checkpointed affinity reservation"
                )
            if persisted and connection_version is None:
                raise _DefinitiveProvisionError(
                    f"persisted execution connection {connection_id!r} has no version"
                )
            # Insert the first provider lease without updating an existing row.
            # If a prior worker committed its full handle but lost the graph
            # checkpoint, recover that handle and skip every provider call.
            recovered = engine_runs.prepare_engine_provision_sync(
                thread_id,
                attempt,
                cfg.engine,
                {
                    "engine": cfg.engine,
                    "connection_id": connection_id,
                    "idempotency_key": spec.idempotency_key,
                    "extras": {},
                },
                project_id=cfg.project_id,
                app_id=cfg.app_id,
                artifact_namespace=engine_artifact_namespace(spec.idempotency_key),
                connection_id=connection_id,
                connection_version=connection_version,
            )
            if recovered is not None:
                candidate = _validated_engine_handle(recovered)
                handle = candidate.model_copy(
                    update={
                        "engine": cfg.engine,
                        "connection_id": connection_id,
                        "idempotency_key": spec.idempotency_key,
                    }
                )
                return [], handle, True
            # Provider methods receive isolated models. Validation/provisioning may
            # inspect but never rewrite the checkpointed target or idempotency key.
            report = _validated_engine_report(await adapter.validate(spec.model_copy(deep=True)))
            if not report.ok:
                return list(report.issues), None, False
            provisioned = _validated_engine_handle(
                await adapter.provision(spec.model_copy(deep=True))
            )
            # Identity/affinity fields come from the trusted resolver and the
            # checkpointed spec, never from a plugin response. Preserve only the
            # provider-issued run id/extras while preventing a handle from
            # redirecting start/status/abort to another adapter or namespace.
            handle = provisioned.model_copy(
                update={
                    "engine": cfg.engine,
                    "connection_id": connection_id,
                    "idempotency_key": spec.idempotency_key,
                }
            )
            return [], handle, False
        finally:
            await _close_resource(adapter)

    try:
        issues, handle, already_projected = asyncio.run(_provision())
    except engine_runs.EngineRunReservationRejectedError:
        raise
    except _DefinitiveProvisionError as exc:
        detail = bounded_diagnostic(exc)
        issues, handle, already_projected = (
            [bounded_diagnostic(f"engine provisioning failed: {detail}")],
            None,
            False,
        )
    except Exception as exc:  # noqa: BLE001 - preserve ambiguous provider outcome
        return _retry_provision(exc)
    if handle is None:
        errors = [
            bounded_diagnostic(f"engine spec validation failed: {bounded_diagnostic(issue)}")
            for issue in issues
        ]
        engine_runs.record_engine_run_sync(
            thread_id,
            attempt,
            cfg.engine,
            {},
            EngineRunPhase.FAILED.value,
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            artifact_namespace=engine_artifact_namespace(spec.idempotency_key),
            required=True,
        )
        update = _update(
            attempt,
            status=PhaseStatus.FAILED.value,
            errors=errors,
            engine_provision_required=False,
            engine_provision_blocked=False,
            engine_provision_last_error=None,
        )
        return Command(goto="finalize", update=update)

    handle_json = handle.model_dump(mode="json")
    if not already_projected:
        try:
            engine_runs.record_engine_run_sync(
                thread_id,
                attempt,
                handle.engine,
                handle_json,
                EngineRunPhase.READY.value,
                project_id=cfg.project_id,
                app_id=cfg.app_id,
                external_run_id=handle.external_run_id,
                artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
                required=True,
            )
        except engine_runs.EngineRunReservationRejectedError:
            # A terminal winner makes this graph checkpoint stale. Do not replace
            # its outcome or issue more provider calls from the losing worker.
            raise
        except Exception as exc:  # noqa: BLE001 - create/commit outcome may be ambiguous
            return _retry_provision(exc)
    update = _update(
        attempt,
        engine=handle.engine,
        engine_connection_id=handle.connection_id,
        engine_handle=handle_json,
        engine_provision_required=False,
        engine_provision_blocked=False,
        engine_provision_failures=0,
        engine_provision_last_error=None,
        engine_provision_completed_at=utcnow_iso(),
    )
    update["engine_handle"] = handle_json
    return Command(goto="engine_start", update=update)


def engine_provision_blocked(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Pause after bounded ambiguous creates while retaining the provider lease."""

    entry = _entry(state)
    detail = bounded_diagnostic(
        entry.get("engine_provision_last_error") or "engine provisioning unavailable"
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_provision_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "error": detail,
            "message": (
                "Engine provisioning exhausted its retry budget. Resume to reconcile "
                "the same idempotency key on the pinned execution connection."
            ),
        }
    )
    return Command(
        goto="engine_provision",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_provision_required=True,
            engine_provision_blocked=False,
            engine_provision_failures=0,
            engine_provision_last_error=None,
        ),
    )


def engine_provision_resume(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Open a fresh bounded same-key provision retry window on a later graph run."""

    del config
    entry = _entry(state)
    return Command(
        goto="engine_provision",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_provision_required=True,
            engine_provision_blocked=False,
            engine_provision_failures=0,
            engine_provision_last_error=None,
        ),
    )


def engine_start(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Start a previously checkpointed handle, then checkpoint before status IO."""
    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)
    # Required pre-I/O lock: reject terminal/stale workers before provider start,
    # without overwriting a RUNNING handle whose DB commit acknowledgement was
    # lost before the graph checkpoint. In that recovery case, skip start and
    # checkpoint the already-durable provider-owned handle fields.
    recovered_handle = engine_runs.prepare_engine_start_sync(
        _thread_id(config),
        attempt,
        handle.engine,
        handle.model_dump(mode="json"),
        project_id=cfg.project_id,
        app_id=cfg.app_id,
        connection_id=reservation_connection_id,
        connection_version=connection_version,
    )
    if recovered_handle is not None:
        handle = _validated_started_handle(recovered_handle, handle)
        handle_json = handle.model_dump(mode="json")
        update = _update(
            attempt,
            engine_handle=handle_json,
            engine_started_at=entry.get("engine_started_at") or utcnow_iso(),
        )
        update["engine_handle"] = handle_json
        return Command(goto="engine_status", update=update)

    trusted_handle_json = handle.model_dump(mode="json")

    async def _start() -> tuple[str | None, JsonDict]:
        adapter = await _resolve_engine_for_runtime_io(
            cfg,
            engine_options,
            entry,
            handle,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
        )
        provider_handle = handle.model_copy(deep=True)
        try:
            start_error: str | None = None
            try:
                await adapter.start(provider_handle)
            except Exception as exc:  # noqa: BLE001
                start_error = bounded_diagnostic(exc)
            try:
                started = _validated_started_handle(provider_handle, handle)
            except Exception as exc:  # noqa: BLE001 - reject malformed plugin mutation
                mutation_error = bounded_diagnostic(exc)
                detail = bounded_diagnostic(
                    f"provider returned an invalid start handle: {mutation_error}"
                )
                if start_error is not None:
                    detail = bounded_diagnostic(f"{start_error}; {detail}")
                return detail, trusted_handle_json
            return start_error, started.model_dump(mode="json")
        finally:
            await _close_resource(adapter)

    try:
        error, handle_json = asyncio.run(_start())
    except Exception as exc:  # resolver/build failures are also terminal
        error, handle_json = bounded_diagnostic(exc), trusted_handle_json
    handle = EngineHandle.model_validate(handle_json)
    if error is not None:
        reason = bounded_diagnostic(f"start failed: {error}")
        update = _update(
            attempt,
            engine_handle=handle_json,
            engine_cleanup_required=True,
            engine_cleanup_reason=reason,
            engine_cleanup_final_error=bounded_diagnostic(
                f"execution engine start failed: {error}"
            ),
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
        connection_id=reservation_connection_id,
        connection_version=connection_version,
        required=True,
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
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)

    async def _status() -> EngineRunStatus:
        adapter = await _resolve_engine_for_runtime_io(
            cfg,
            engine_options,
            entry,
            handle,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
        )
        try:
            return _validated_engine_status(await adapter.get_status(handle.model_copy(deep=True)))
        finally:
            await _close_resource(adapter)

    try:
        status = asyncio.run(_status())
    except Exception as exc:  # noqa: BLE001 - poll loop owns bounded recovery
        failures = int(entry.get("engine_poll_errors") or 0) + 1
        detail = bounded_diagnostic(exc)
        message = bounded_diagnostic(
            "execution engine initial status failed "
            f"({failures}/{MAX_CONSECUTIVE_POLL_ERRORS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_poll_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "error": detail,
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
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)

    async def _poll() -> EngineRunStatus:
        adapter = await _resolve_engine_for_runtime_io(
            cfg,
            engine_options,
            entry,
            handle,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
        )
        try:
            return _validated_engine_status(await adapter.get_status(handle.model_copy(deep=True)))
        finally:
            await _close_resource(adapter)

    try:
        status = await _poll()
    except Exception as exc:  # noqa: BLE001 - bounded retry before remote cleanup
        failures = int(entry.get("engine_poll_errors") or 0) + 1
        detail = bounded_diagnostic(exc)
        message = bounded_diagnostic(
            f"execution engine poll failed ({failures}/{MAX_CONSECUTIVE_POLL_ERRORS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_poll_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "error": detail,
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
    max_poll_cycles = math.ceil(cfg.limits.poll_timeout_s / cfg.limits.poll_interval_s)
    wall_clock_timeout = elapsed is not None and elapsed > cfg.limits.poll_timeout_s
    cycle_timeout = poll_count >= max_poll_cycles
    if wall_clock_timeout or cycle_timeout:
        reason = f"poll timeout after {cfg.limits.poll_timeout_s}s"
        observed = f"{elapsed:.1f}s" if elapsed is not None else "an unavailable start timestamp"
        fallback = (
            f"; durable poll-cycle budget {max_poll_cycles} exhausted" if cycle_timeout else ""
        )
        error = (
            f"execution engine run {handle.external_run_id} timed out after {observed} "
            f"(limits.poll_timeout_s={cfg.limits.poll_timeout_s}{fallback})"
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
    reason = bounded_diagnostic(entry.get("engine_cleanup_reason") or "execution cleanup required")
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)

    recovered_phase: EngineRunPhase | None = None
    if reservation_connection_id is not None and connection_version is not None:
        recovered_status = engine_runs.recover_engine_completion_sync(
            _thread_id(config),
            attempt,
            handle.engine,
            handle.model_dump(mode="json"),
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            external_run_id=handle.external_run_id,
            artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
            connection_id=reservation_connection_id,
            connection_version=connection_version,
            completion_kind=engine_runs.COMPLETION_CLEANUP_TEARDOWN,
            expected_statuses=frozenset(TERMINAL_ENGINE_PHASES),
        )
        if recovered_status is not None:
            recovered_phase = EngineRunPhase(recovered_status)

    async def _cleanup() -> EngineRunPhase:
        adapter = await _resolve_engine_for_runtime_io(
            cfg,
            engine_options,
            entry,
            handle,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
        )
        try:
            # Sim and some legacy engines communicate an accepted abort to their
            # immediately-following status call through bounded handle extras. Keep
            # one isolated copy across this provider transaction, then discard it.
            observed_phase, followup_handle = await _confirm_abort(adapter, handle, reason)
            await _teardown_after_confirmed_abort(adapter, followup_handle)
            return observed_phase
        finally:
            await _close_resource(adapter)

    try:
        observed_phase = recovered_phase if recovered_phase is not None else await _cleanup()
    except Exception as exc:  # noqa: BLE001 - durable retry is the safety contract
        failures = int(entry.get("engine_cleanup_failures") or 0) + 1
        detail = bounded_diagnostic(exc)
        logger.warning(
            "execution.cleanup_retry",
            external_run_id=handle.external_run_id,
            failures=failures,
            error=detail,
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
                engine_cleanup_last_error=detail,
            ),
        )

    if recovered_phase is None:
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
            connection_id=reservation_connection_id,
            connection_version=connection_version,
            completion_kind=(
                engine_runs.COMPLETION_CLEANUP_TEARDOWN
                if reservation_connection_id is not None and connection_version is not None
                else None
            ),
            required=True,
        )
    final_error = bounded_diagnostic(entry.get("engine_cleanup_final_error") or reason)
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
            errors=[bounded_diagnostic(f"{final_error}; {outcome}")],
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
    if _entry(state).get("engine_collection_settle_required"):
        return "engine_collection_settle_resume"
    if _entry(state).get("engine_collection_staged"):
        return "engine_collection_settle"
    if _entry(state).get("engine_collection_settled"):
        next_node = _entry(state).get("engine_collection_next")
        if next_node in {"open_output_gate", "finalize"}:
            return str(next_node)
        raise RuntimeError("settled engine collection has no valid continuation")
    if _entry(state).get("engine_collection_required"):
        return "engine_collection_resume"
    if _entry(state).get("engine_provision_required"):
        return "engine_provision_resume"
    return "prepare"


def engine_collect(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Collect artifacts + summary with a checkpointed, bounded retry lifecycle.

    The node only stages collected output. A sync LangGraph checkpoint must commit
    that output before ``engine_collection_settle`` can project a terminal engine
    status, tear down provider resources, and continue to the gate/finalizer.
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

    # This must precede even artifact-store resolution: a legacy provider-local
    # run id without execution affinity cannot be collected from today's default.
    reservation_connection_id, reservation_connection_version = _connection_reservation_affinity(
        entry, handle
    )

    def _retry_collection(exc: Exception) -> Command[str]:
        failures = int(entry.get("engine_collection_failures") or 0) + 1
        detail = bounded_diagnostic(exc, max_chars=2_048)
        message = (
            f"engine collection failed ({failures}/{MAX_ENGINE_COLLECTION_ATTEMPTS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_collection_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "external_run_id": handle.external_run_id,
                "failure": failures,
                "error": detail,
            }
        )
        blocked = failures >= MAX_ENGINE_COLLECTION_ATTEMPTS
        return Command(
            goto="engine_collection_blocked" if blocked else "engine_collect",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_collection_required=True,
                engine_collection_blocked=blocked,
                engine_collection_failures=failures,
                engine_collection_last_error=message,
            ),
        )

    raw_artifact_connection_id = entry.get("artifact_store_connection_id")
    artifact_connection_id: str | None
    if raw_artifact_connection_id is None:
        artifact_connection_id = None
    elif (
        not isinstance(raw_artifact_connection_id, str)
        or not raw_artifact_connection_id
        or raw_artifact_connection_id != raw_artifact_connection_id.strip()
        or len(raw_artifact_connection_id) > 256
    ):
        return _retry_collection(
            ValueError("checkpointed artifact-store connection id is malformed")
        )
    else:
        artifact_connection_id = raw_artifact_connection_id

    if artifact_connection_id is None:

        async def _reserve_artifact_store() -> tuple[str, str | None, bool]:
            store: Any | None = None
            try:
                resolved = await _resolve_artifact_store(cfg)
                store = resolved.adapter
                resolved_connection_id = resolved.connection_id
                if (
                    not resolved_connection_id
                    or resolved_connection_id != resolved_connection_id.strip()
                    or len(resolved_connection_id) > 256
                ):
                    raise RuntimeError("resolved artifact-store connection has no valid durable id")
                if resolved.persisted and resolved.connection_version is None:
                    raise RuntimeError(
                        f"persisted artifact-store connection {resolved_connection_id!r} "
                        "has no version"
                    )
                return (
                    resolved_connection_id,
                    (
                        resolved.connection_version.isoformat()
                        if resolved.connection_version is not None
                        else None
                    ),
                    resolved.persisted,
                )
            finally:
                if store is not None:
                    await _close_resource(store)

        try:
            (
                artifact_connection_id,
                artifact_connection_version,
                artifact_connection_persisted,
            ) = asyncio.run(_reserve_artifact_store())
        except Exception as exc:  # noqa: BLE001 - use the same bounded retry lifecycle
            return _retry_collection(exc)

        # Persist exact artifact-store affinity in its own superstep before the
        # adapter can write a byte. A retry or process restart must never follow a
        # newly selected project/global default to a different store.
        return Command(
            goto="engine_collect",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                artifact_store_connection_id=artifact_connection_id,
                artifact_store_connection_version=artifact_connection_version,
                artifact_store_connection_persisted=artifact_connection_persisted,
                engine_collection_required=True,
            ),
        )

    try:
        artifact_connection_version = _artifact_reservation_affinity(entry, artifact_connection_id)
    except Exception as exc:  # noqa: BLE001 - checkpointed affinity fails closed
        return _retry_collection(exc)

    async def _collect() -> tuple[list[JsonDict], TestResultSummary, str]:
        refs: list[JsonDict] = []
        resolved_artifact_connection_id = ""
        adapter: Any | None = None
        store: Any | None = None
        resolved: Any | None = None
        try:
            resolved = await _resolve_engine(_config_for_handle(cfg, handle), engine_options)
            adapter = resolved.adapter if isinstance(resolved, ResolvedAdapter) else resolved
            if adapter is None:
                raise RuntimeError("resolved execution adapter is unavailable")
            if isinstance(resolved, ResolvedAdapter):
                execution_connection_id = resolved.connection_id
                connection_version = resolved.connection_version
                if resolved.persisted and connection_version is None:
                    raise RuntimeError(
                        f"persisted execution connection {execution_connection_id!r} has no version"
                    )
            else:
                execution_connection_id = (
                    getattr(adapter, "_apex_resolved_connection_id", None) or handle.connection_id
                )
                connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
            if not execution_connection_id:
                raise RuntimeError("resolved execution connection has no durable id")

            resolved_store = await _resolve_artifact_store(
                cfg, connection_id=artifact_connection_id
            )
            store = resolved_store.adapter
            resolved_artifact_connection_id = resolved_store.connection_id
            if resolved_artifact_connection_id != artifact_connection_id:
                raise _RequiredArtifactIndexError(
                    "artifact-store resolver did not honor checkpointed connection affinity"
                )
            if (
                resolved_store.connection_version != artifact_connection_version
                or resolved_store.persisted
                is not (entry.get("artifact_store_connection_persisted") is True)
            ):
                raise _RequiredArtifactIndexError(
                    "artifact-store connection changed after its checkpointed reservation"
                )
            if reservation_connection_id is not None and (
                execution_connection_id != reservation_connection_id
                or connection_version != reservation_connection_version
            ):
                raise RuntimeError(
                    "execution connection changed after its checkpointed reservation"
                )
            # COLLECTING is deliberately nonterminal. It keeps the pinned execution
            # FK lease while also reserving the artifact store before any adapter PUT.
            engine_runs.record_engine_run_sync(
                _thread_id(config),
                attempt,
                handle.engine,
                handle.model_dump(mode="json"),
                EngineRunPhase.COLLECTING.value,
                project_id=cfg.project_id,
                app_id=cfg.app_id,
                external_run_id=handle.external_run_id,
                artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
                artifact_connection_id=resolved_artifact_connection_id,
                artifact_connection_version=artifact_connection_version,
                connection_id=execution_connection_id,
                connection_version=connection_version,
                required=True,
            )
            provider_handle = handle.model_copy(deep=True)
            store_view = _EngineArtifactStoreView(
                store,
                engine_artifact_namespace(handle.idempotency_key),
            )
            collected = await adapter.collect_artifacts(
                provider_handle,
                store_view,
            )
            refs = _validated_engine_artifacts(
                collected,
                handle,
                written=store_view.written,
            )
            if refs:
                try:
                    await record_artifact_references(
                        [
                            ArtifactReferenceInput(
                                artifact_key=str(ref["key"]),
                                kind=str(ref["kind"]),
                            )
                            for ref in refs
                        ],
                        connection_id=resolved_artifact_connection_id,
                        thread_id=_thread_id(config),
                        project_id=cfg.project_id,
                        app_id=cfg.app_id,
                    )
                except Exception as exc:
                    # The ownership transaction may have committed before an
                    # exception/cancellation was observed. Deterministic object keys
                    # and idempotent batch indexing make retry the only safe action.
                    raise _RequiredArtifactIndexError(
                        "required engine artifact indexing failed"
                    ) from exc
            summary = _validated_engine_summary(
                await adapter.fetch_summary(handle.model_copy(deep=True)),
                expected_engine=handle.engine,
            )
        finally:
            if adapter is not None:
                await _close_resource(adapter)
            if store is not None:
                await _close_resource(store)
        return refs, summary, resolved_artifact_connection_id

    try:
        refs, summary, artifact_connection_id = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001 - checkpoint a bounded exact retry
        return _retry_collection(exc)

    artifacts: list[JsonDict] = []
    for index, raw_ref in enumerate(refs):
        ref = dict(raw_ref)
        # Deterministic ids: re-execution after a crash must not duplicate refs
        # under the append-unique-by-id artifacts reducer.
        ref["id"] = f"{_PHASE.value}-a{attempt}-engine-artifact-{index}"
        ref["artifact_connection_id"] = artifact_connection_id
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
        "status": PhaseStatus.RUNNING.value,
        "summary": summary_text,
        "test_summary": summary_json,
        "artifact_ids": [artifact["id"] for artifact in artifacts],
        "artifact_namespace": engine_artifact_namespace(handle.idempotency_key),
        "artifact_store_connection_id": artifact_connection_id,
        "engine_collection_required": False,
        "engine_collection_blocked": False,
        "engine_collection_failures": 0,
        "engine_collection_last_error": None,
        "engine_collection_completed_at": utcnow_iso(),
        "engine_collection_staged": True,
    }
    goto = "open_output_gate"
    final_status: str | None = None
    if engine_phase is EngineRunPhase.ABORTED:
        final_status = PhaseStatus.ABORTED.value
        fields["errors"] = [
            f"engine run {handle.external_run_id} was aborted",
        ]
        goto = "finalize"
    elif engine_phase is EngineRunPhase.FAILED or not summary.passed:
        final_status = PhaseStatus.FAILED.value
        phase_errors = list(summary.sla_breaches)
        if not phase_errors and not state_errors:
            phase_errors = [bounded_diagnostic(last.get("message") or "engine run failed")]
        fields["errors"] = phase_errors
        goto = "finalize"

    projected_phase = (
        EngineRunPhase.FAILED if final_status == PhaseStatus.FAILED.value else engine_phase
    )
    fields["engine_collection_projected_phase"] = projected_phase.value
    fields["engine_collection_final_status"] = final_status
    fields["engine_collection_next"] = goto
    update = _update(attempt, **fields)
    update["artifacts"] = artifacts
    # The sync LangGraph checkpoint between this node and settle is the durability
    # boundary: terminal projection and potentially destructive teardown must not
    # run until every collected output is recoverable from graph state.
    return Command(goto="engine_collection_settle", update=update)


def engine_collection_settle(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Replay-safe terminal projection and teardown after the collection checkpoint."""

    cfg = PipelineConfigurable.from_config(config)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry)
    if entry.get("engine_collection_staged") is not True:
        raise RuntimeError("engine collection settle requires checkpointed collection output")

    try:
        projected_phase = EngineRunPhase(str(entry.get("engine_collection_projected_phase")))
    except ValueError as exc:
        raise RuntimeError("staged engine collection has an invalid terminal phase") from exc
    if projected_phase not in TERMINAL_ENGINE_PHASES:
        raise RuntimeError("staged engine collection phase is not terminal")

    next_node = entry.get("engine_collection_next")
    final_status = entry.get("engine_collection_final_status")
    expected: tuple[str | None, str]
    if projected_phase is EngineRunPhase.ABORTED:
        expected = (PhaseStatus.ABORTED.value, "finalize")
    elif projected_phase is EngineRunPhase.FAILED:
        expected = (PhaseStatus.FAILED.value, "finalize")
    else:
        expected = (None, "open_output_gate")
    if (final_status, next_node) != expected:
        raise RuntimeError("staged engine collection outcome is inconsistent")
    if not isinstance(next_node, str):  # narrowed for the typed Command destination
        raise RuntimeError("staged engine collection has no continuation")

    summary = _validated_engine_summary(entry.get("test_summary"), expected_engine=handle.engine)
    artifact_connection_id = entry.get("artifact_store_connection_id")
    if (
        not isinstance(artifact_connection_id, str)
        or not artifact_connection_id
        or artifact_connection_id != artifact_connection_id.strip()
        or len(artifact_connection_id) > 256
    ):
        raise RuntimeError("staged engine collection has no valid artifact-store affinity")
    artifact_connection_version = _artifact_reservation_affinity(entry, artifact_connection_id)
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)
    completion_witness_available = (
        reservation_connection_id is not None and connection_version is not None
    ) or artifact_connection_version is not None

    recovered_terminal = False
    if completion_witness_available:
        recovered_status = engine_runs.recover_engine_completion_sync(
            _thread_id(config),
            attempt,
            handle.engine,
            handle.model_dump(mode="json"),
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            external_run_id=handle.external_run_id,
            artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
            artifact_connection_id=artifact_connection_id,
            artifact_connection_version=artifact_connection_version,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
            completion_kind=engine_runs.COMPLETION_COLLECTION_TEARDOWN,
            expected_statuses=frozenset({projected_phase.value}),
        )
        recovered_terminal = recovered_status is not None

    async def _teardown() -> None:
        adapter = await _resolve_engine_for_runtime_io(
            cfg,
            engine_options,
            entry,
            handle,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
        )
        try:
            await adapter.teardown(handle.model_copy(deep=True))
        finally:
            await _close_resource(adapter)

    try:
        if not recovered_terminal:
            asyncio.run(_teardown())
    except Exception as exc:  # noqa: BLE001 - collection is already durably recoverable
        detail = bounded_diagnostic(exc)
        failures = int(entry.get("engine_collection_settle_failures") or 0) + 1
        message = bounded_diagnostic(
            f"engine teardown failed ({failures}/{MAX_ENGINE_SETTLE_ATTEMPTS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_collection_settle_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "external_run_id": handle.external_run_id,
                "failure": failures,
                "error": detail,
            }
        )
        blocked = failures >= MAX_ENGINE_SETTLE_ATTEMPTS
        return Command(
            goto=("engine_collection_settle_blocked" if blocked else "engine_collection_settle"),
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_collection_settle_required=True,
                engine_collection_settle_blocked=blocked,
                engine_collection_settle_failures=failures,
                engine_collection_settle_last_error=message,
            ),
        )

    # This may commit and then lose the response (for example during engine
    # disposal). A retry re-enters this settle node directly and the monotonic
    # upsert explicitly permits the exact same terminal status.
    if not recovered_terminal:
        engine_runs.record_engine_run_sync(
            _thread_id(config),
            attempt,
            handle.engine,
            handle.model_dump(mode="json"),
            projected_phase.value,
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            external_run_id=handle.external_run_id,
            summary=summary.model_dump(mode="json"),
            artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
            artifact_connection_id=artifact_connection_id,
            artifact_connection_version=artifact_connection_version,
            connection_id=reservation_connection_id,
            connection_version=connection_version,
            completion_kind=(
                engine_runs.COMPLETION_COLLECTION_TEARDOWN if completion_witness_available else None
            ),
            required=True,
        )

    settle_fields: JsonDict = {
        # Keep the phase nonterminal until the already-checkpointed destination
        # (gate/finalize) commits. A later recovery run can therefore re-enter this
        # attempt instead of plan_resolver incorrectly incrementing it.
        "status": PhaseStatus.RUNNING.value,
        "engine_collection_staged": False,
        "engine_collection_settled": True,
        "engine_collection_projected_phase": None,
        "engine_collection_settle_required": False,
        "engine_collection_settle_blocked": False,
        "engine_collection_settle_failures": 0,
        "engine_collection_settle_last_error": None,
        "engine_collection_settled_at": utcnow_iso(),
    }
    update = _update(attempt, **settle_fields)
    return Command(goto=next_node, update=update)


def engine_collection_settle_blocked(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Pause after bounded teardown retries without releasing the provider lease."""

    entry = _entry(state)
    detail = bounded_diagnostic(
        entry.get("engine_collection_settle_last_error") or "engine teardown unavailable"
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_collection_settle_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "error": detail,
            "message": (
                "Engine teardown exhausted its retry budget. Collected results and the "
                "execution lease remain durable; resume to retry teardown."
            ),
        }
    )
    return Command(
        goto="engine_collection_settle",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_settle_required=True,
            engine_collection_settle_blocked=False,
            engine_collection_settle_failures=0,
            engine_collection_settle_last_error=None,
        ),
    )


def engine_collection_settle_resume(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Open a fresh bounded teardown retry window on a later graph run."""

    del config
    entry = _entry(state)
    if entry.get("engine_collection_staged") is not True:
        raise RuntimeError("engine teardown resume requires staged collection output")
    return Command(
        goto="engine_collection_settle",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_settle_required=True,
            engine_collection_settle_blocked=False,
            engine_collection_settle_failures=0,
            engine_collection_settle_last_error=None,
        ),
    )


def engine_collection_blocked(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Interrupt after bounded retries while preserving a resumable intent."""

    entry = _entry(state)
    detail = bounded_diagnostic(
        entry.get("engine_collection_last_error") or "engine collection unavailable"
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_collection_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "error": detail,
            "message": (
                "Engine result collection exhausted its retry budget. Resume to retry "
                "the same deterministic artifact keys and pinned connections."
            ),
        }
    )
    return Command(
        goto="engine_collect",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_required=True,
            engine_collection_blocked=False,
            engine_collection_failures=0,
            engine_collection_last_error=None,
        ),
    )


def engine_collection_resume(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Start a fresh bounded retry window on an operator-triggered thread run."""

    del config
    entry = _entry(state)
    return Command(
        goto="engine_collect",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_required=True,
            engine_collection_blocked=False,
            engine_collection_failures=0,
            engine_collection_last_error=None,
        ),
    )


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
        "engine_provision",
        engine_provision,
        destinations=(
            "engine_provision",
            "engine_provision_blocked",
            "engine_start",
            "finalize",
        ),
    )
    builder.add_node(
        "engine_provision_blocked",
        engine_provision_blocked,
        destinations=("engine_provision",),
    )
    builder.add_node(
        "engine_provision_resume",
        engine_provision_resume,
        destinations=("engine_provision",),
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
        "engine_collect",
        engine_collect,
        destinations=(
            "engine_collect",
            "engine_collection_blocked",
            "engine_collection_settle",
        ),
    )
    builder.add_node(
        "engine_collection_settle",
        engine_collection_settle,
        destinations=(
            "engine_collection_settle",
            "engine_collection_settle_blocked",
            "open_output_gate",
            "finalize",
        ),
    )
    builder.add_node(
        "engine_collection_settle_blocked",
        engine_collection_settle_blocked,
        destinations=("engine_collection_settle",),
    )
    builder.add_node(
        "engine_collection_settle_resume",
        engine_collection_settle_resume,
        destinations=("engine_collection_settle",),
    )
    builder.add_node(
        "engine_collection_blocked",
        engine_collection_blocked,
        destinations=("engine_collect",),
    )
    builder.add_node(
        "engine_collection_resume",
        engine_collection_resume,
        destinations=("engine_collect",),
    )
    builder.add_edge("agent", "engine_reserve")


__all__ = [
    "SPINE_SUPERSTEPS",
    "add_execution_engine_nodes",
    "engine_cleanup",
    "engine_collect",
    "engine_collection_blocked",
    "engine_collection_resume",
    "engine_collection_settle",
    "engine_collection_settle_blocked",
    "engine_collection_settle_resume",
    "engine_poll",
    "engine_provision",
    "engine_provision_blocked",
    "engine_provision_resume",
    "engine_reserve",
    "engine_start",
    "engine_status",
    "execution_idempotency_key",
    "recommended_recursion_limit",
    "route_execution_entry",
]
