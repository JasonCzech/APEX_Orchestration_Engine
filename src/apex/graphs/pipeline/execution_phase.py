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
- engine_collect persists normalized artifacts + summary into graph state;
  engine_collection_index then records exact ownership without provider/store I/O,
  and only the checkpoint-gated settle node may tear down/project the run terminal.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from types import GetSetDescriptorType, MemberDescriptorType
from typing import Any, TypeGuard

import structlog
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from apex.adapters.registry import PortKind
from apex.domain.diagnostics import bounded_diagnostic, contains_credential_material
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
from apex.ports.artifact_store import (
    StoredArtifact,
    canonical_artifact_uri,
    engine_artifact_namespace,
    validate_stored_artifact_ack,
)
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
# nodes, agent alias, reserve/start/collect/index/settle, finalize) plus parent-spine slack.
# Includes execution/artifact-store affinity reservations, bounded provision and
# collection retries, the durable collection-settle split, explicit blocked nodes,
# and the resume paths used by later operator-triggered runs.
SPINE_SUPERSTEPS = 36
MAX_CONSECUTIVE_POLL_ERRORS = 3
MAX_ENGINE_PROVISION_ATTEMPTS = 3
MAX_ENGINE_COLLECTION_ATTEMPTS = 3
MAX_ENGINE_SETTLE_ATTEMPTS = 3
MAX_ENGINE_ARTIFACT_REFS = 32
MAX_ENGINE_ARTIFACT_WRITES = 32
# Preserve the built-in LoadRunner contract (512 MiB per report, 1 GiB total)
# while imposing a provider-independent hard ceiling at the graph boundary.
MAX_ENGINE_ARTIFACT_BYTES_PER_OBJECT = 512 * 1024 * 1024
MAX_ENGINE_ARTIFACT_BYTES_TOTAL = 1024 * 1024 * 1024
_ENGINE_ARTIFACT_KINDS = frozenset({"engine_results", "engine_report"})


class _RequiredArtifactIndexError(RuntimeError):
    """Collected objects cannot become checkpoint-visible without exact ownership."""


class _DefinitiveProvisionError(RuntimeError):
    """Provisioning cannot safely continue with the checkpointed reservation."""


async def _await_cleanup_task_definitively(task: asyncio.Task[None]) -> None:
    """Observe a cleanup task's final outcome despite repeated caller cancellation."""

    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    error: BaseException | None = None
    try:
        task.result()
    except BaseException as exc:  # retrieve every child outcome before returning
        error = exc
    if error is not None:
        raise error
    if cancelled:
        raise asyncio.CancelledError from None


async def _close_owned_resolution(adapter: Any | None, resolver: Any) -> None:
    """Release a leaf checkout and every resolver-owned nested generation."""

    async def close_resolver() -> None:
        close = getattr(resolver, "close", None)
        if not callable(close):
            return
        result = close()
        if isawaitable(result):
            await result

    tasks = [asyncio.create_task(close_resolver(), name="execution-resolver-close")]
    if adapter is not None:
        tasks.insert(
            0,
            asyncio.create_task(
                close_adapter(adapter),
                name="execution-adapter-checkout-close",
            ),
        )
    cancelled = False
    error: BaseException | None = None
    for task in tasks:
        try:
            await _await_cleanup_task_definitively(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error
    if cancelled:
        raise asyncio.CancelledError from None


@dataclass(frozen=True)
class _OwnedResolvedAdapter(ResolvedAdapter):
    """Resolution metadata whose close owns both leaf and resolver cache."""

    resolver: Any

    async def close(self) -> None:
        await _close_owned_resolution(self.adapter, self.resolver)

    async def aclose(self) -> None:
        await self.close()


class _OwnedAdapter:
    """Compatibility wrapper for resolvers that do not expose metadata."""

    def __init__(self, adapter: Any, resolver: Any) -> None:
        self.adapter = adapter
        self.resolver = resolver

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    async def close(self) -> None:
        await _close_owned_resolution(self.adapter, self.resolver)

    async def aclose(self) -> None:
        await self.close()


def _own_resolution(value: Any, resolver: Any) -> Any:
    """Attach a throwaway resolver when it has an explicit close contract."""

    if not callable(getattr(resolver, "close", None)):
        return value
    if isinstance(value, ResolvedAdapter):
        return _OwnedResolvedAdapter(
            adapter=value.adapter,
            connection_id=value.connection_id,
            connection_version=value.connection_version,
            persisted=value.persisted,
            resolver=resolver,
        )
    return _OwnedAdapter(value, resolver)


class _EngineArtifactStoreView:
    """Attempt-scoped, write-only facade for execution-engine artifact output.

    Provider-selected names are logical hints only.  Physical object keys are
    canonical ordinal slots beneath the attempt namespace.  A process can die
    after an object-store acknowledgement but before the graph checkpoints the
    returned refs; assigning the same finite slots on every replay prevents a
    provider that changes names/order from stranding an unbounded set of keys.
    """

    def __init__(self, store: Any, namespace: str) -> None:
        self._store = store
        self._namespace = namespace
        self._prefix = f"{namespace}/"
        self.written: dict[str, tuple[StoredArtifact, str]] = {}
        self.attempted: dict[str, int] = {}
        self._requested: set[str] = set()
        self._compensated: set[str] = set()
        self._reserved_bytes = 0

    def _slot_key(self, slot: int) -> str:
        return f"{self._namespace}/artifact-{slot:04d}"

    def _all_slot_keys(self) -> tuple[str, ...]:
        return tuple(self._slot_key(slot) for slot in range(MAX_ENGINE_ARTIFACT_WRITES))

    def _key(self, key: str) -> str:
        if (
            type(key) is not str
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
            type(content_type) is not str
            or not content_type
            or len(content_type) > 255
            or any(char in content_type for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("execution engine artifact content type is invalid")
        return content_type

    def _reserve(self, requested_key: str, size: int) -> str:
        if requested_key in self._requested:
            raise ValueError("execution engine artifact keys may be attempted only once")
        if len(self.attempted) >= MAX_ENGINE_ARTIFACT_WRITES:
            raise ValueError("execution engine artifact write count exceeds the hard limit")
        if size < 0 or size > MAX_ENGINE_ARTIFACT_BYTES_PER_OBJECT:
            raise ValueError("execution engine artifact exceeds the per-object byte limit")
        if size > MAX_ENGINE_ARTIFACT_BYTES_TOTAL - self._reserved_bytes:
            raise ValueError("execution engine artifacts exceed the aggregate byte limit")
        # Reserve count and logical bytes before provider IO. Ambiguous failures do
        # not refund the reservation, so a plugin cannot catch errors and continue
        # issuing unbounded writes through the same facade.
        storage_key = self._slot_key(len(self.attempted))
        self._requested.add(requested_key)
        self.attempted[storage_key] = size
        self._reserved_bytes += size
        return storage_key

    async def _cleanup_keys(self, keys: tuple[str, ...]) -> None:
        delete: Any = getattr(self._store, "delete", None)
        if not callable(delete):
            raise RuntimeError("artifact store cannot compensate an incomplete engine write")
        cleanup_failed = False
        for key in keys:
            try:
                cleanup_result: Any = delete(key)
                await cleanup_result
            except (KeyError, FileNotFoundError):
                pass
            except BaseException:
                cleanup_failed = True
                continue
            self._compensated.add(key)
            self.written.pop(key, None)
        if cleanup_failed:
            raise RuntimeError(
                "artifact store could not compensate an incomplete engine write"
            ) from None

    async def _cleanup_definitively(self, keys: tuple[str, ...]) -> None:
        pending = tuple(key for key in keys if key not in self._compensated)
        if not pending:
            return
        task = asyncio.create_task(
            self._cleanup_keys(pending),
            name="engine-artifact-compensation",
        )
        await _await_cleanup_task_definitively(task)

    async def _cleanup_key(self, key: str) -> None:
        await self._cleanup_definitively((key,))

    async def cleanup_except(self, retained_keys: set[str]) -> None:
        """Delete every finite replay slot that cannot receive a durable reference.

        Enumerating the whole bounded slot set also removes slots left by a prior
        process that died before it could checkpoint its in-memory write manifest.
        """

        await self._cleanup_definitively(
            tuple(key for key in self._all_slot_keys() if key not in retained_keys)
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        requested_key = self._key(key)
        checked_content_type = self._content_type(content_type)
        if type(data) is not bytes:
            raise ValueError("execution engine artifact data must be bytes")
        storage_key = self._reserve(requested_key, len(data))
        try:
            acknowledgement = validate_stored_artifact_ack(
                await self._store.put(
                    storage_key,
                    data,
                    content_type=checked_content_type,
                ),
                storage_key,
                expected_size=len(data),
            )
        except BaseException:
            await self._cleanup_key(storage_key)
            raise
        stored = acknowledgement.model_copy(
            update={"uri": canonical_artifact_uri(storage_key)},
            deep=True,
        )
        self.written[storage_key] = (stored.model_copy(deep=True), checked_content_type)
        return stored

    async def put_stream(
        self,
        key: str,
        data: AsyncIterable[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> StoredArtifact:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("execution engine artifact max_bytes must be a positive integer")
        requested_key = self._key(key)
        checked_content_type = self._content_type(content_type)
        storage_key = self._reserve(requested_key, max_bytes)
        consumed_bytes = 0
        exhausted = False

        async def counted_stream() -> AsyncIterator[bytes]:
            nonlocal consumed_bytes, exhausted
            async for chunk in data:
                if type(chunk) is not bytes:
                    raise ValueError("execution engine artifact stream must yield bytes")
                if len(chunk) > max_bytes - consumed_bytes:
                    raise ValueError("execution engine artifact stream exceeds max_bytes")
                consumed_bytes += len(chunk)
                yield chunk
            exhausted = True

        try:
            acknowledgement = validate_stored_artifact_ack(
                await self._store.put_stream(
                    storage_key,
                    counted_stream(),
                    content_type=checked_content_type,
                    max_bytes=max_bytes,
                ),
                storage_key,
                expected_size=consumed_bytes,
                max_size=max_bytes,
            )
            if not exhausted:
                raise RuntimeError("artifact store did not consume the complete artifact stream")
        except BaseException:
            await self._cleanup_key(storage_key)
            raise
        self._reserved_bytes -= max_bytes - consumed_bytes
        self.attempted[storage_key] = consumed_bytes
        stored = acknowledgement.model_copy(
            update={"uri": canonical_artifact_uri(storage_key)},
            deep=True,
        )
        self.written[storage_key] = (stored.model_copy(deep=True), checked_content_type)
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
    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    entry = results.get(_PHASE.value)
    if entry is None:
        entry = {}
    if type(entry) is not dict:
        raise ValueError("checkpointed execution phase is invalid")
    return entry


def _attempt(entry: JsonDict) -> int:
    return _checkpoint_int(entry.get("attempt"), label="execution attempt", default=1, minimum=1)


def _checkpoint_int(
    value: Any,
    *,
    label: str,
    default: int = 0,
    minimum: int = 0,
    maximum: int = 1_000_000,
) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"checkpointed {label} is invalid")
    return value


def _checkpoint_flag(entry: JsonDict, field: str) -> bool:
    value = entry.get(field)
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError(f"checkpointed {field} flag is invalid")
    return value


def _checkpoint_timestamp_or_now(value: Any) -> str:
    """Reuse one bounded, credential-free ISO timestamp or replace it safely."""

    if (
        type(value) is str
        and 1 <= len(value) <= 64
        and "\x00" not in value
        and not contains_credential_material(value)
    ):
        try:
            parsed = datetime.fromisoformat(value)
        except (OverflowError, ValueError):
            pass
        else:
            if parsed.tzinfo is not None:
                return value
    return utcnow_iso()


def _checkpoint_diagnostic_text(value: Any, *, default: str) -> str:
    if type(value) is not str or not value:
        return default
    return bounded_diagnostic(value)


def _safe_connection_id(value: Any) -> TypeGuard[str]:
    return (
        type(value) is str
        and 1 <= len(value) <= 256
        and value == value.strip()
        and "\x00" not in value
        and not contains_credential_material(value)
    )


def _update(attempt: int, **fields: Any) -> JsonDict:
    # Carries the attempt so the phase_results reducer merges instead of clobbers.
    return {"phase_results": {_PHASE.value: {"attempt": attempt, **fields}}}


def _thread_id(config: RunnableConfig | None) -> str:
    if config is not None and type(config) is not dict:
        raise ValueError("execution phase configuration is invalid")
    configurable = (config if config is not None else {}).get("configurable")
    if configurable is None:
        configurable = {}
    if type(configurable) is not dict:
        raise ValueError("execution phase configuration is invalid")
    thread_id = configurable.get("thread_id")
    if (
        type(thread_id) is not str
        or not thread_id
        or thread_id != thread_id.strip()
        or len(thread_id) > 255
        or "\x00" in thread_id
        or contains_credential_material(thread_id)
    ):
        raise ValueError(
            "execution phase requires a safe durable thread_id; stateless execution is not allowed"
        )
    return thread_id


def _engine_options(entry: JsonDict, engine: str) -> JsonDict:
    options = entry.get("engine_options")
    if options is None:
        options = {}
    if (
        type(options) is not dict
        or len(options) > 16
        or any(type(key) is not str or not 1 <= len(key) <= 64 or "\x00" in key for key in options)
    ):
        raise ValueError("checkpointed engine options are invalid")
    for value in options.values():
        if value is None or type(value) is bool:
            continue
        if type(value) is int and value.bit_length() <= 128:
            continue
        if type(value) is float and math.isfinite(value):
            continue
        if type(value) is str and len(value) <= 2_048 and "\x00" not in value:
            continue
        raise ValueError("checkpointed engine options are invalid")
    if contains_credential_material(options, max_nodes=64, max_total_chars=32_768):
        raise ValueError("checkpointed engine options contain credential material")
    normalized = dict(options)
    _validate_engine_options(engine, normalized)
    return normalized


def _handle_from(state: PipelineState, entry: JsonDict) -> EngineHandle:
    top_level_raw = state.get("engine_handle")
    entry_raw = entry.get("engine_handle")
    if top_level_raw is None and entry_raw is None:
        raise ValueError(
            "execution phase: engine_handle missing from state (engine_start must run first)"
        )
    top_level = _validated_engine_handle(top_level_raw) if top_level_raw is not None else None
    nested = _validated_engine_handle(entry_raw) if entry_raw is not None else None
    if top_level is not None and nested is not None and top_level != nested:
        # Both channels are written in the same execution superstep. A mismatch
        # can only be a malformed/poisoned checkpoint; preferring either side
        # could redirect poll/abort/collection to another provider-owned run.
        raise ValueError("checkpointed execution handles are inconsistent")
    if nested is not None:
        return nested
    if top_level is None:  # pragma: no cover - guarded by the missing check above
        raise ValueError("checkpointed execution handle is missing")
    return top_level


def _elapsed_s(started_at_iso: str | None) -> float | None:
    if type(started_at_iso) is not str or not started_at_iso:
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

    affinity_staged = entry.get("engine_connection_affinity_staged")
    if affinity_staged is not None and type(affinity_staged) is not bool:
        raise RuntimeError("checkpointed execution affinity flag is invalid")
    if affinity_staged is not True:
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
    if connection_id is not None and not _safe_connection_id(connection_id):
        raise RuntimeError("checkpointed execution connection id is invalid")
    if connection_id != handle.connection_id:
        raise RuntimeError("checkpointed execution handle does not match its connection affinity")
    raw_persisted = entry.get("engine_connection_persisted")
    if raw_persisted is not None and type(raw_persisted) is not bool:
        raise RuntimeError("checkpointed execution persistence flag is invalid")
    persisted = raw_persisted is True
    if get_settings().is_locked_down and not persisted:
        # Static/unversioned providers are a development compatibility seam. A
        # locked deployment must be able to fence an in-place connection edit.
        raise EngineConnectionAffinityMissingError
    raw_version = entry.get("engine_connection_version")
    if raw_version is None:
        if persisted:
            raise RuntimeError("persisted execution affinity has no checkpointed version")
        return handle.connection_id, None
    if (
        not persisted
        or type(raw_version) is not str
        or not 1 <= len(raw_version) <= 64
        or "\x00" in raw_version
        or contains_credential_material(raw_version)
    ):
        raise RuntimeError("checkpointed execution connection version is inconsistent")
    version: datetime | None = None
    try:
        version = datetime.fromisoformat(raw_version)
    except (OverflowError, ValueError):
        pass
    if version is None:
        raise RuntimeError("checkpointed execution connection version is malformed")
    if version.tzinfo is None:
        raise RuntimeError("checkpointed execution connection version has no timezone")
    return handle.connection_id, version


def _artifact_reservation_affinity(entry: JsonDict, connection_id: str) -> datetime | None:
    """Return the checkpointed artifact-store runtime generation."""

    if not _safe_connection_id(connection_id):
        raise RuntimeError("checkpointed artifact-store connection id is invalid")

    raw_persisted = entry.get("artifact_store_connection_persisted")
    if raw_persisted is not None and type(raw_persisted) is not bool:
        raise RuntimeError("checkpointed artifact-store persistence flag is invalid")
    persisted = raw_persisted is True
    if get_settings().is_locked_down and not persisted:
        raise RuntimeError(
            "checkpointed artifact-store affinity has no durable connection generation"
        )
    raw_version = entry.get("artifact_store_connection_version")
    if raw_version is None:
        if persisted:
            raise RuntimeError("persisted artifact-store affinity has no checkpointed version")
        return None
    if (
        not persisted
        or type(raw_version) is not str
        or not 1 <= len(raw_version) <= 64
        or "\x00" in raw_version
        or contains_credential_material(raw_version)
    ):
        raise RuntimeError("checkpointed artifact-store connection version is inconsistent")
    version: datetime | None = None
    try:
        version = datetime.fromisoformat(raw_version)
    except (OverflowError, ValueError):
        pass
    if version is None:
        raise RuntimeError("checkpointed artifact-store connection version is malformed")
    if version.tzinfo is None:
        raise RuntimeError("checkpointed artifact-store connection version has no timezone")
    if not connection_id:
        raise RuntimeError("checkpointed artifact-store connection id is missing")
    return version


def _exact_provider_payload(
    value: Any,
    *,
    model_type: type[Any],
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> JsonDict:
    """Read one tiny provider schema without invoking serializers or bulk copies."""

    payload: JsonDict | None = None
    try:
        if type(value) is model_type:
            state_descriptor = type.__getattribute__(BaseModel, "__dict__")["__dict__"]
            extras_descriptor = type.__getattribute__(BaseModel, "__dict__")["__pydantic_extra__"]
            if (
                type(state_descriptor) is not GetSetDescriptorType
                or type(extras_descriptor) is not MemberDescriptorType
            ):
                raise TypeError("provider model slots are unavailable")
            source = state_descriptor.__get__(value, model_type)
            extras = extras_descriptor.__get__(value, model_type)
            if type(source) is not dict or (
                extras is not None and (type(extras) is not dict or len(extras) > 0)
            ):
                raise ValueError("provider model has extras")
        elif type(value) is dict:
            source = value
        else:
            raise TypeError("provider value is not an object")
        iterator = iter(source)
        keys: list[str] = []
        for _ in range(len(allowed) + 1):
            try:
                key = next(iterator)
            except StopIteration:
                break
            if type(key) is not str or key not in allowed or key in keys:
                raise ValueError("provider fields are invalid")
            keys.append(key)
        if len(keys) > len(allowed) or not required.issubset(keys):
            raise ValueError("provider fields are invalid")
        payload = {key: source[key] for key in keys}
    except Exception:
        pass
    if payload is None:
        raise ValueError(f"{label} is invalid")
    return payload


def _provider_text(value: Any, *, max_chars: int, optional: bool = False) -> bool:
    return (optional and value is None) or (
        type(value) is str and len(value) <= max_chars and "\x00" not in value
    )


def _provider_number(value: Any) -> bool:
    if type(value) is int:
        return value.bit_length() <= 128
    return type(value) is float and math.isfinite(value)


def _bounded_provider_mapping(
    value: Any,
    *,
    max_items: int,
    max_key_chars: int,
    label: str,
) -> JsonDict:
    """Copy at most a fixed dynamic mapping window after bounded key inspection."""

    payload: JsonDict | None = None
    try:
        if type(value) is not dict:
            raise TypeError("provider field is not a mapping")
        iterator = iter(value)
        keys: list[str] = []
        for _ in range(max_items + 1):
            try:
                key = next(iterator)
            except StopIteration:
                break
            if (
                type(key) is not str
                or not key
                or len(key) > max_key_chars
                or "\x00" in key
                or key in keys
            ):
                raise ValueError("provider mapping key is invalid")
            keys.append(key)
        if len(keys) > max_items:
            raise ValueError("provider mapping is oversized")
        payload = {key: value[key] for key in keys}
    except Exception:
        pass
    if payload is None:
        raise ValueError(f"{label} is invalid")
    return payload


def _validated_engine_status(value: Any) -> EngineRunStatus:
    """Revalidate even model instances returned by provider adapters."""

    payload = _exact_provider_payload(
        value,
        model_type=EngineRunStatus,
        allowed=frozenset({"phase", "progress_pct", "live_stats", "message"}),
        required=frozenset({"phase"}),
        label="engine status",
    )
    phase = payload.get("phase")
    progress = payload.get("progress_pct", 0.0)
    message = payload.get("message")
    if (
        not (type(phase) is EngineRunPhase or _provider_text(phase, max_chars=32))
        or not _provider_number(progress)
        or not _provider_text(message, max_chars=4_096, optional=True)
    ):
        raise ValueError("engine status is invalid") from None
    live_stats = payload.get("live_stats")
    if live_stats is not None:
        stats = _exact_provider_payload(
            live_stats,
            model_type=LiveStats,
            allowed=frozenset({"vusers", "tps", "error_rate", "p95_ms"}),
            required=frozenset(),
            label="engine status",
        )
        if any(not _provider_number(metric) for metric in stats.values()):
            raise ValueError("engine status is invalid") from None
        payload["live_stats"] = stats
    if contains_credential_material(payload, max_nodes=16, max_total_chars=8_192):
        raise ValueError("engine status must not contain credential material") from None
    validated: EngineRunStatus | None = None
    try:
        validated = EngineRunStatus.model_validate(payload)
    except Exception:
        pass
    if validated is None:
        raise ValueError("engine status is invalid")
    return validated


def _validated_engine_summary(
    value: Any, *, expected_engine: str | None = None
) -> TestResultSummary:
    payload = _exact_provider_payload(
        value,
        model_type=TestResultSummary,
        allowed=frozenset({"engine", "passed", "kpis", "sla_breaches", "notes"}),
        required=frozenset({"engine", "passed"}),
        label="engine summary",
    )
    if (
        not _provider_text(payload.get("engine"), max_chars=64)
        or type(payload.get("passed")) is not bool
        or not _provider_text(payload.get("notes"), max_chars=20_000, optional=True)
    ):
        raise ValueError("engine summary is invalid") from None
    kpis = _bounded_provider_mapping(
        payload.get("kpis", {}),
        max_items=64,
        max_key_chars=64,
        label="engine summary",
    )
    if any(not _provider_number(metric) for metric in kpis.values()):
        raise ValueError("engine summary is invalid") from None
    breaches = payload.get("sla_breaches", [])
    if (
        type(breaches) is not list
        or len(breaches) > 128
        or any(not _provider_text(breach, max_chars=2_048) for breach in breaches)
    ):
        raise ValueError("engine summary is invalid") from None
    payload["kpis"] = kpis
    payload["sla_breaches"] = list(breaches)
    if contains_credential_material(payload, max_nodes=256, max_total_chars=300_000):
        raise ValueError("engine summary must not contain credential material") from None
    summary: TestResultSummary | None = None
    try:
        summary = TestResultSummary.model_validate(payload)
    except Exception:
        pass
    if summary is None:
        raise ValueError("engine summary is invalid")
    if expected_engine is not None and summary.engine != expected_engine:
        raise ValueError("engine summary provider does not match the checkpointed engine")
    return summary


def _validated_engine_handle(
    value: Any, *, allow_credential_material: bool = False
) -> EngineHandle:
    payload = _exact_provider_payload(
        value,
        model_type=EngineHandle,
        allowed=frozenset(
            {"engine", "connection_id", "external_run_id", "idempotency_key", "extras"}
        ),
        required=frozenset({"engine"}),
        label="engine handle",
    )
    if (
        not _provider_text(payload.get("engine"), max_chars=64)
        or not _provider_text(payload.get("connection_id"), max_chars=256, optional=True)
        or not _provider_text(payload.get("external_run_id"), max_chars=255, optional=True)
        or not _provider_text(payload.get("idempotency_key", ""), max_chars=256)
    ):
        raise ValueError("engine handle is invalid") from None
    extras = _bounded_provider_mapping(
        payload.get("extras", {}),
        max_items=32,
        max_key_chars=64,
        label="engine handle",
    )
    if any(not _provider_text(item, max_chars=2_048) for item in extras.values()):
        raise ValueError("engine handle is invalid") from None
    payload["extras"] = extras
    has_credentials = contains_credential_material(payload, max_nodes=64, max_total_chars=32_768)
    if has_credentials and not allow_credential_material:
        raise ValueError("engine handle must not contain credential material") from None
    if has_credentials:
        # Provision already created a remote side effect. Retain this bounded,
        # structurally checked identity only in memory so compensation can target
        # it; it must never cross a checkpoint/projection boundary.
        return EngineHandle.model_construct(**payload)
    validated: EngineHandle | None = None
    try:
        validated = EngineHandle.model_validate(payload)
    except Exception:
        pass
    if validated is None:
        raise ValueError("engine handle is invalid")
    return validated


def _validated_engine_report(value: Any) -> ValidationReport:
    payload = _exact_provider_payload(
        value,
        model_type=ValidationReport,
        allowed=frozenset({"ok", "issues"}),
        required=frozenset(),
        label="engine validation report",
    )
    issues = payload.get("issues", [])
    if (
        type(payload.get("ok", True)) is not bool
        or type(issues) is not list
        or len(issues) > 128
        or any(not _provider_text(issue, max_chars=2_048) for issue in issues)
    ):
        raise ValueError("engine validation report is invalid") from None
    payload["issues"] = list(issues)
    if contains_credential_material(payload, max_nodes=160, max_total_chars=300_000):
        raise ValueError("engine validation report must not contain credential material") from None
    validated: ValidationReport | None = None
    try:
        validated = ValidationReport.model_validate(payload)
    except Exception:
        pass
    if validated is None:
        raise ValueError("engine validation report is invalid")
    return validated


def _validated_started_handle(
    value: Any,
    trusted: EngineHandle,
    *,
    allow_credential_material: bool = False,
) -> EngineHandle:
    """Accept bounded provider-owned start output without permitting affinity drift."""

    candidate = _validated_engine_handle(
        value,
        allow_credential_material=allow_credential_material,
    )
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
    try:
        resolve_with_metadata = getattr(resolver, "resolve_with_metadata", None)
        if resolve_with_metadata is not None:
            resolved = await resolve_with_metadata(
                PortKind.EXECUTION_ENGINE,
                connection_id=connection_id or cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
                project_id=cfg.project_id,
                expected_provider=cfg.engine,
                options_overlay=engine_options,
            )
            return _own_resolution(resolved, resolver)
        # Compatibility seam for narrow test resolvers and downstream extensions that
        # have not yet adopted structured resolution metadata.
        adapter, _resolved_connection_id = await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE,
            connection_id=connection_id or cfg.connections.get(PortKind.EXECUTION_ENGINE.value),
            project_id=cfg.project_id,
            expected_provider=cfg.engine,
            options_overlay=engine_options,
        )
        return _own_resolution(adapter, resolver)
    except BaseException:
        # Adapter construction can already have resolved a nested SECRETS
        # generation before a later provider build fails. The resolver remains
        # the only owner capable of retiring that generation.
        await _close_owned_resolution(None, resolver)
        raise


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
        if expected_connection_id is not None and not _safe_connection_id(expected_connection_id):
            raise RuntimeError("checkpointed execution connection id is invalid")
        staged = _checkpoint_flag(entry, "engine_connection_affinity_staged")
        expected_persisted = _checkpoint_flag(entry, "engine_connection_persisted")
        if isinstance(resolved, ResolvedAdapter):
            resolved_connection_id = resolved.connection_id
            if not _safe_connection_id(resolved_connection_id):
                raise RuntimeError("execution resolver returned an invalid connection id")
            if (
                expected_connection_id is not None
                and resolved_connection_id != expected_connection_id
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
            if actual_connection_id is not None and not _safe_connection_id(actual_connection_id):
                raise RuntimeError("execution resolver returned an invalid connection id")
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
        return resolved
    except BaseException:
        await _close_resources_definitively(resolved)
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
    resolver = _make_resolver()
    try:
        resolved = await resolver.resolve_with_metadata(
            PortKind.ARTIFACT_STORE,
            connection_id=connection_id or cfg.connections.get(PortKind.ARTIFACT_STORE.value),
            project_id=cfg.project_id,
        )
        return _own_resolution(resolved, resolver)
    except BaseException:
        await _close_owned_resolution(None, resolver)
        raise


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


async def _close_resources_definitively(*resources: Any) -> None:
    """Retire every throwaway resource before propagating repeated cancellation."""

    tasks = [
        asyncio.create_task(_close_resource(resource), name="execution-adapter-close")
        for resource in resources
        if resource is not None
    ]
    cancelled = False
    error: BaseException | None = None
    for task in tasks:
        try:
            await _await_cleanup_task_definitively(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error
    if cancelled:
        raise asyncio.CancelledError from None


async def _teardown_after_confirmed_abort(adapter: Any, handle: EngineHandle) -> None:
    """Release provider resources before the durable lease may become terminal.

    ``teardown`` is an idempotent port operation. Propagating failures keeps the
    cleanup intent and execution connection lease nonterminal so a later cleanup
    superstep can retry the same handle instead of leaking provider resources.
    """

    await adapter.teardown(handle)


async def _compensate_unsafe_provision(adapter: Any, handle: EngineHandle) -> None:
    """Abort an unsafe post-create/start handle without ever making it durable.

    The handle is structurally valid but contains credential-shaped executable
    identity. It may be used only in memory to compensate the just-created run.
    Every error is replaced with a fixed message so exception chains cannot retain
    the provider value. A failed confirmation leaves the PROVISIONING reservation
    retryable with the same idempotency key.
    """

    failed = False
    try:
        await adapter.abort(handle, reason="provider returned an unsafe durable handle")
        try:
            status = _validated_engine_status(
                await adapter.get_status(handle.model_copy(deep=True))
            )
        except EngineProviderRunNotFoundError:
            status = None
        if status is not None and status.phase not in TERMINAL_ENGINE_PHASES:
            raise RuntimeError("compensating abort did not reach a terminal state")
        await adapter.teardown(handle)
    except Exception:
        failed = True
    if failed:
        raise RuntimeError("unsafe engine handle compensation could not be confirmed")


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

    if type(raw_refs) is not list:
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
        payload = None
        try:
            payload = _exact_provider_payload(
                raw_ref,
                model_type=ArtifactRef,
                allowed=frozenset(
                    {
                        "id",
                        "kind",
                        "name",
                        "uri",
                        "key",
                        "artifact_connection_id",
                        "media_type",
                        "summary",
                        "created_at",
                    }
                ),
                required=frozenset({"kind", "name", "uri"}),
                label="engine artifact ref",
            )
        except Exception:
            pass
        if payload is None:
            raise ValueError(f"engine artifact ref {index} is invalid")
        # Provider adapters never own durable IDs, store affinity, or timestamps.
        # Ignore those fields even on manufactured Pydantic instances and stage
        # only the bounded object identity/content that the server can revalidate.
        payload = {
            field: payload[field]
            for field in ("kind", "name", "uri", "key", "media_type", "summary")
            if field in payload
        }
        limits = {
            "kind": (64, False),
            "name": (512, False),
            "uri": (4_096, False),
            "key": (1_024, True),
            "media_type": (255, False),
            "summary": (4_000, True),
        }
        if any(
            not _provider_text(
                payload.get(field),
                max_chars=max_chars,
                optional=optional,
            )
            for field, (max_chars, optional) in limits.items()
            if field in payload
        ):
            raise ValueError(f"engine artifact ref {index} is invalid") from None
        if contains_credential_material(payload, max_nodes=16, max_total_chars=16_384):
            raise ValueError(f"engine artifact ref {index} must not contain credential material")
        ref: ArtifactRef | None = None
        try:
            ref = ArtifactRef.model_validate(payload)
        except Exception:
            pass
        if ref is None:
            raise ValueError(f"engine artifact ref {index} is invalid")
        if ref.kind not in _ENGINE_ARTIFACT_KINDS:
            raise ValueError(f"engine artifact ref {index} has an unsupported kind")
        key = ref.key
        if not key or not key.startswith(prefix) or key == prefix:
            raise ValueError(f"engine artifact ref {index} key must be beneath {namespace!r}")
        if ref.uri != canonical_artifact_uri(key):
            raise ValueError(f"engine artifact ref {index} URI does not match its canonical object")
        if key in seen_keys:
            raise ValueError(f"engine artifact ref {index} duplicates an artifact key")
        if written is not None:
            stored = written.get(key)
            if stored is None:
                raise ValueError(
                    f"engine artifact ref {index} was not written through the scoped store"
                )
            stored_artifact, content_type = stored
            if ref.uri != stored_artifact.uri:
                raise ValueError(f"engine artifact ref {index} URI does not match its object")
            if ref.media_type != content_type:
                raise ValueError(
                    f"engine artifact ref {index} media type does not match its object"
                )
        seen_keys.add(key)
        normalized = ref.model_dump(mode="json")
        validated.append(
            {
                field: normalized[field]
                for field in ("kind", "name", "uri", "key", "media_type", "summary")
            }
        )
    return validated


# ── nodes ──────────────────────────────────────────────────────────────────────


def _validated_checkpoint_spec(value: Any) -> LoadTestSpec:
    """Validate a replayed spec without invoking coercion or serializer hooks."""

    def finite_number(number: Any) -> bool:
        if type(number) is int:
            return number.bit_length() <= 128
        return type(number) is float and math.isfinite(number)

    allowed = {
        "idempotency_key",
        "title",
        "script_refs",
        "vusers",
        "ramp_s",
        "duration_s",
        "slas",
        "target_environment",
    }
    if (
        type(value) is not dict
        or len(value) > len(allowed)
        or any(type(key) is not str or key not in allowed for key in value)
    ):
        raise ValueError("checkpointed load-test spec is invalid")
    title = value.get("title")
    idempotency_key = value.get("idempotency_key")
    script_refs = value.get("script_refs", [])
    vusers = value.get("vusers", 10)
    ramp_s = value.get("ramp_s", 5.0)
    duration_s = value.get("duration_s", 2.0)
    slas = value.get("slas", {})
    target_environment = value.get("target_environment")
    if (
        type(title) is not str
        or not 1 <= len(title) <= 1_000
        or "\x00" in title
        or type(idempotency_key) is not str
        or not 1 <= len(idempotency_key) <= 256
        or "\x00" in idempotency_key
        or type(script_refs) is not list
        or len(script_refs) > 100
        or any(
            type(ref) is not str or not 1 <= len(ref) <= 2_048 or not ref.strip() or "\x00" in ref
            for ref in script_refs
        )
        or type(vusers) is not int
        or not 1 <= vusers <= 10_000
        or not finite_number(ramp_s)
        or not 0 <= ramp_s <= 86_400
        or not finite_number(duration_s)
        or not 0 < duration_s <= 86_400
        or type(slas) is not dict
        or len(slas) > 32
        or (
            target_environment is not None
            and (
                type(target_environment) is not str
                or len(target_environment) > 2_048
                or "\x00" in target_environment
            )
        )
    ):
        raise ValueError("checkpointed load-test spec is invalid")
    for name, threshold in slas.items():
        if (
            type(name) is not str
            or not 1 <= len(name) <= 64
            or not name.strip()
            or "\x00" in name
            or not finite_number(threshold)
            or not 0 <= threshold <= 1_000_000_000_000
        ):
            raise ValueError("checkpointed load-test spec is invalid")
    if contains_credential_material(
        value,
        max_nodes=256,
        max_total_chars=300_000,
    ):
        raise ValueError("checkpointed load-test spec contains credential material")
    validated: LoadTestSpec | None = None
    try:
        validated = LoadTestSpec.model_validate(value)
    except Exception:
        pass
    if validated is None:
        raise ValueError("checkpointed load-test spec is invalid")
    return validated


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
    results = state.get("phase_results")
    if results is None:
        results = {}
    if type(results) is not dict:
        raise ValueError("checkpointed phase results are invalid")
    upstream = results.get(Phase.SCRIPT_SCENARIO.value)
    if upstream is None:
        upstream = {}
    if type(upstream) is not dict:
        raise ValueError("checkpointed script-scenario phase is invalid")
    raw = upstream.get("load_test_spec")
    base: JsonDict
    if raw is not None and type(raw) is not dict:
        raise ValueError("checkpointed load-test spec is invalid")
    if type(raw) is dict and len(raw) > 0:
        base = dict(raw)
    else:
        title = state.get("title")
        if title is None:
            title = "untitled run"
        if type(title) is not str or len(title) > 500 or "\x00" in title:
            raise ValueError("checkpointed pipeline title is invalid")
        base = {
            "title": f"{title} load test",
            "vusers": 10,
            "ramp_s": 1.0,
        }
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
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
    if type(legacy_script_refs) is list:
        base["script_refs"] = legacy_script_refs
    # Ignore any caller-seeded upstream target; only the auth-resolved immutable
    # run target may reach an execution adapter.
    base["target_environment"] = target_environment
    base["idempotency_key"] = execution_idempotency_key(_thread_id(config), attempt)
    spec = _validated_checkpoint_spec(base)
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
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
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
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    thread_id = _thread_id(config)
    spec = _validated_checkpoint_spec(entry.get("load_test_spec"))
    engine_options = _engine_options(entry, cfg.engine)

    def _retry_provision(exc: Exception, **checkpoint_fields: Any) -> Command[str]:
        failures = (
            _checkpoint_int(
                entry.get("engine_provision_failures"),
                label="engine provision failure count",
            )
            + 1
        )
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
                **checkpoint_fields,
            ),
        )

    raw_connection_id = entry.get("engine_connection_id")
    if raw_connection_id is not None and not _safe_connection_id(raw_connection_id):
        return _retry_provision(
            _DefinitiveProvisionError("checkpointed execution connection id is malformed"),
            engine_connection_id=None,
            engine_connection_version=None,
            engine_connection_persisted=False,
            engine_connection_affinity_staged=False,
        )
    connection_id = raw_connection_id if type(raw_connection_id) is str else None

    if not _checkpoint_flag(entry, "engine_connection_affinity_staged"):

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
                    resolved_connection_id = getattr(adapter, "_apex_resolved_connection_id", None)
                    if resolved_connection_id is None:
                        resolved_connection_id = connection_id
                    if resolved_connection_id is None:
                        resolved_connection_id = cfg.connections.get(
                            PortKind.EXECUTION_ENGINE.value
                        )
                    connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
                    persisted = connection_version is not None
                if not _safe_connection_id(resolved_connection_id):
                    raise RuntimeError("resolved execution connection has no valid durable id")
                if connection_id is not None and resolved_connection_id != connection_id:
                    raise RuntimeError(
                        "execution resolver did not honor checkpointed connection affinity"
                    )
                if connection_version is not None and type(connection_version) is not datetime:
                    raise RuntimeError("resolved execution connection has an invalid version")
                if persisted and connection_version is None:
                    raise RuntimeError(
                        f"persisted execution connection {resolved_connection_id!r} has no version"
                    )
                if get_settings().is_locked_down and not persisted:
                    raise RuntimeError("locked execution requires a durable connection generation")
                return (
                    resolved_connection_id,
                    connection_version.isoformat() if connection_version is not None else None,
                    persisted,
                )
            finally:
                await _close_resources_definitively(resolved)

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
                engine_provision_failures=_checkpoint_int(
                    entry.get("engine_provision_failures"),
                    label="engine provision failure count",
                ),
            ),
        )

    if connection_id is None:
        return _retry_provision(
            _DefinitiveProvisionError("staged execution connection affinity is missing")
        )
    expected_version = entry.get("engine_connection_version")
    if expected_version is not None:
        valid_version = (
            type(expected_version) is str
            and 1 <= len(expected_version) <= 64
            and "\x00" not in expected_version
            and not contains_credential_material(expected_version)
        )
        if valid_version:
            try:
                parsed_version = datetime.fromisoformat(expected_version)
            except (OverflowError, ValueError):
                valid_version = False
            else:
                valid_version = parsed_version.tzinfo is not None
        if not valid_version:
            return _retry_provision(
                _DefinitiveProvisionError("staged execution connection version is malformed"),
                engine_connection_id=None,
                engine_connection_version=None,
                engine_connection_persisted=False,
                engine_connection_affinity_staged=False,
            )
    expected_persisted = _checkpoint_flag(entry, "engine_connection_persisted")
    if get_settings().is_locked_down and not expected_persisted:
        return _retry_provision(
            _DefinitiveProvisionError(
                "locked execution affinity has no durable connection generation"
            ),
            engine_connection_id=None,
            engine_connection_version=None,
            engine_connection_persisted=False,
            engine_connection_affinity_staged=False,
        )

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
                resolved_connection_id = getattr(adapter, "_apex_resolved_connection_id", None)
                if resolved_connection_id is None:
                    resolved_connection_id = connection_id
                connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
                persisted = connection_version is not None
            actual_version = (
                connection_version.isoformat() if type(connection_version) is datetime else None
            )
            if (
                not _safe_connection_id(resolved_connection_id)
                or resolved_connection_id != connection_id
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
            raw_provisioned = await adapter.provision(spec.model_copy(deep=True))
            provisioned = _validated_engine_handle(
                raw_provisioned,
                allow_credential_material=True,
            )
            if contains_credential_material(provisioned.model_dump(mode="json")):
                unsafe_handle = provisioned.model_copy(
                    update={
                        "engine": cfg.engine,
                        "connection_id": connection_id,
                        "idempotency_key": spec.idempotency_key,
                    }
                )
                await _compensate_unsafe_provision(adapter, unsafe_handle)
                raise _DefinitiveProvisionError(
                    "execution engine returned an unsafe handle; remote run was compensated"
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
            await _close_resources_definitively(resolved)

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
        _checkpoint_diagnostic_text(
            entry.get("engine_provision_last_error"),
            default="engine provisioning unavailable",
        )
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_provision_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "actions": ["retry"],
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
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
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
            engine_started_at=_checkpoint_timestamp_or_now(entry.get("engine_started_at")),
        )
        update["engine_handle"] = handle_json
        return Command(goto="engine_status", update=update)

    trusted_handle_json = handle.model_dump(mode="json")

    async def _start() -> tuple[str | None, JsonDict, bool]:
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
                try:
                    unsafe_started = _validated_started_handle(
                        provider_handle,
                        handle,
                        allow_credential_material=True,
                    )
                except Exception:
                    unsafe_started = None
                if unsafe_started is not None and contains_credential_material(
                    unsafe_started.model_dump(mode="json")
                ):
                    try:
                        await _compensate_unsafe_provision(adapter, unsafe_started)
                    except Exception:
                        compensation_detail = (
                            "unsafe start handle compensation could not be confirmed"
                        )
                    else:
                        compensation_detail = "unsafe start handle was compensated"
                        if start_error is not None:
                            detail = bounded_diagnostic(f"{start_error}; {detail}")
                        return (
                            bounded_diagnostic(f"{detail}; {compensation_detail}"),
                            trusted_handle_json,
                            True,
                        )
                    detail = bounded_diagnostic(f"{detail}; {compensation_detail}")
                if start_error is not None:
                    detail = bounded_diagnostic(f"{start_error}; {detail}")
                return detail, trusted_handle_json, False
            return start_error, started.model_dump(mode="json"), False
        finally:
            await _close_resources_definitively(adapter)

    try:
        error, handle_json, unsafe_start_compensated = asyncio.run(_start())
    except Exception as exc:  # resolver/build failures are also terminal
        error, handle_json, unsafe_start_compensated = (
            bounded_diagnostic(exc),
            trusted_handle_json,
            False,
        )
    handle = EngineHandle.model_validate(handle_json)
    if unsafe_start_compensated:
        detail = "execution engine returned an unsafe start handle; remote run was compensated"
        engine_runs.record_engine_run_sync(
            _thread_id(config),
            attempt,
            handle.engine,
            handle_json,
            EngineRunPhase.FAILED.value,
            project_id=cfg.project_id,
            app_id=cfg.app_id,
            artifact_namespace=engine_artifact_namespace(handle.idempotency_key),
            connection_id=reservation_connection_id,
            connection_version=connection_version,
            required=True,
        )
        update = _update(
            attempt,
            status=PhaseStatus.FAILED.value,
            engine_handle=handle_json,
            engine_cleanup_required=False,
            engine_cleanup_reason=None,
            engine_cleanup_final_error=None,
            engine_cleanup_failures=0,
            errors=[detail],
        )
        update["engine_handle"] = handle_json
        return Command(goto="finalize", update=update)
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
    """Fetch and count the initial status observation before entering the poll loop.

    A fast provider may already be terminal by the time this node runs.  Counting
    this read here keeps the durable observation shape identical whether the run
    completes before or after the first dedicated ``engine_poll`` superstep.
    """
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)
    prior_poll_count = _checkpoint_int(
        entry.get("engine_poll_count"),
        label="engine poll count",
        maximum=MAX_RECOMMENDED_RECURSION_LIMIT - 1,
    )

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
            await _close_resources_definitively(adapter)

    try:
        status = asyncio.run(_status())
    except Exception as exc:  # noqa: BLE001 - poll loop owns bounded recovery
        failures = (
            _checkpoint_int(
                entry.get("engine_poll_errors"),
                label="engine poll error count",
            )
            + 1
        )
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
                engine_poll_count=prior_poll_count,
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
        engine_poll_count=prior_poll_count + 1,
        engine_poll_errors=0,
    )
    goto = "engine_collect" if status.phase in TERMINAL_ENGINE_PHASES else "engine_poll"
    return Command(goto=goto, update=update)


async def _engine_poll_async(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """One poll cycle per superstep; self-loops until the engine is terminal.

    The observation count rides the phase entry and is derived from the
    checkpointed value, so node re-execution after a crash never double-counts a
    successful provider read. ``engine_status`` records observation one.
    """
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
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
            await _close_resources_definitively(adapter)

    try:
        status = await _poll()
    except Exception as exc:  # noqa: BLE001 - bounded retry before remote cleanup
        failures = (
            _checkpoint_int(
                entry.get("engine_poll_errors"),
                label="engine poll error count",
            )
            + 1
        )
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

    poll_count = (
        _checkpoint_int(
            entry.get("engine_poll_count"),
            label="engine poll count",
            maximum=MAX_RECOMMENDED_RECURSION_LIMIT,
        )
        + 1
    )
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

    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
    reason = _checkpoint_diagnostic_text(
        entry.get("engine_cleanup_reason"),
        default="execution cleanup required",
    )
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
            await _close_resources_definitively(adapter)

    try:
        observed_phase = recovered_phase if recovered_phase is not None else await _cleanup()
    except Exception as exc:  # noqa: BLE001 - durable retry is the safety contract
        failures = (
            _checkpoint_int(
                entry.get("engine_cleanup_failures"),
                label="engine cleanup failure count",
                maximum=MAX_RECOMMENDED_RECURSION_LIMIT,
            )
            + 1
        )
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
            return Command(
                goto="engine_cleanup_blocked",
                update=_update(
                    attempt,
                    status=PhaseStatus.RUNNING.value,
                    engine_cleanup_required=True,
                    engine_cleanup_blocked=True,
                    engine_cleanup_failures=failures,
                    engine_cleanup_last_error=detail,
                ),
            )
        await asyncio.sleep(cfg.limits.poll_interval_s)
        return Command(
            goto="engine_cleanup",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_cleanup_required=True,
                engine_cleanup_blocked=False,
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
    final_error = _checkpoint_diagnostic_text(
        entry.get("engine_cleanup_final_error"),
        default=reason,
    )
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
            engine_cleanup_blocked=False,
            engine_cleanup_completed_at=utcnow_iso(),
            engine_cleanup_last_error=None,
        ),
    )


def engine_cleanup(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Synchronous compatibility wrapper for local ``graph.invoke`` callers."""

    return asyncio.run(_engine_cleanup_async(state, config))


def engine_cleanup_blocked(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Expose exhausted abort confirmation as an exact resumable interrupt."""

    entry = _entry(state)
    detail = bounded_diagnostic(
        _checkpoint_diagnostic_text(
            entry.get("engine_cleanup_last_error"),
            default="external engine abort remains unconfirmed",
        )
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_cleanup_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "actions": ["retry"],
            "error": detail,
            "message": (
                "Engine abort confirmation exhausted its retry budget. The exact "
                "provider handle remains durable; resume to retry abort and teardown."
            ),
        }
    )
    return Command(
        goto="engine_cleanup",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_cleanup_required=True,
            engine_cleanup_blocked=False,
            engine_cleanup_failures=0,
            engine_cleanup_last_error=None,
        ),
    )


def route_execution_entry(
    state: PipelineState,
    config: RunnableConfig | None = None,
) -> str:
    """Resume an unfinished kill before any gate or engine side effect.

    A cleanup self-loop can exhaust a run's recursion budget while the provider
    is unavailable. A later run on the same checkpoint must continue that kill,
    not pass through prompt gates or reserve/start another remote execution.
    """

    if config is not None:
        PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    if _checkpoint_flag(entry, "engine_cleanup_required"):
        return "engine_cleanup"
    if _checkpoint_flag(entry, "engine_collection_settle_required"):
        return "engine_collection_settle_resume"
    if _checkpoint_flag(entry, "engine_collection_staged"):
        return "engine_collection_settle"
    if _checkpoint_flag(entry, "engine_collection_settled"):
        next_node = entry.get("engine_collection_next")
        if type(next_node) is str and next_node in {"open_output_gate", "finalize"}:
            return next_node
        raise RuntimeError("settled engine collection has no valid continuation")
    if _checkpoint_flag(entry, "engine_collection_index_required"):
        return "engine_collection_resume"
    if _checkpoint_flag(entry, "engine_collection_required"):
        return "engine_collection_resume"
    if _checkpoint_flag(entry, "engine_provision_required"):
        return "engine_provision_resume"
    return "prepare"


def engine_collect(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Collect artifacts + summary with a checkpointed, bounded retry lifecycle.

    The node only stages collected output. A sync LangGraph checkpoint must commit
    that output before ``engine_collection_index`` can grant durable ownership;
    another checkpoint then gates terminal projection and provider teardown.
    """
    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
    raw_last = entry.get("engine_poll_last")
    if raw_last is None:
        raw_last = {}
    last = raw_last if type(raw_last) is dict else {}
    state_errors: list[str] = []
    raw_engine_phase = last.get("status")
    invalid_collection_state = False
    if type(raw_engine_phase) is not str or len(raw_engine_phase) > 32:
        engine_phase = EngineRunPhase.FAILED
        invalid_collection_state = True
        state_errors.append(
            "execution collection state is invalid: a terminal engine status is missing "
            "or malformed"
        )
    else:
        try:
            engine_phase = EngineRunPhase(raw_engine_phase)
        except ValueError:
            engine_phase = EngineRunPhase.FAILED
            invalid_collection_state = True
            state_errors.append(
                "execution collection state is invalid: a terminal engine status is missing "
                "or malformed"
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

    def _retry_collection(exc: Exception, **checkpoint_fields: Any) -> Command[str]:
        failures = (
            _checkpoint_int(
                entry.get("engine_collection_failures"),
                label="engine collection failure count",
            )
            + 1
        )
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
                **checkpoint_fields,
            ),
        )

    raw_artifact_connection_id = entry.get("artifact_store_connection_id")
    artifact_connection_id: str | None
    if raw_artifact_connection_id is None:
        artifact_connection_id = None
    elif not _safe_connection_id(raw_artifact_connection_id):
        return _retry_collection(
            ValueError("checkpointed artifact-store connection id is malformed"),
            artifact_store_connection_id=None,
            artifact_store_connection_version=None,
            artifact_store_connection_persisted=False,
        )
    else:
        artifact_connection_id = raw_artifact_connection_id

    if artifact_connection_id is None:

        async def _reserve_artifact_store() -> tuple[str, str | None, bool]:
            resolved: Any | None = None
            try:
                resolved = await _resolve_artifact_store(cfg)
                resolved_connection_id = resolved.connection_id
                if not _safe_connection_id(resolved_connection_id):
                    raise RuntimeError("resolved artifact-store connection has no valid durable id")
                if (
                    resolved.connection_version is not None
                    and type(resolved.connection_version) is not datetime
                ):
                    raise RuntimeError("resolved artifact-store connection has an invalid version")
                if resolved.persisted and resolved.connection_version is None:
                    raise RuntimeError(
                        f"persisted artifact-store connection {resolved_connection_id!r} "
                        "has no version"
                    )
                if get_settings().is_locked_down and not resolved.persisted:
                    raise RuntimeError(
                        "locked artifact collection requires a durable connection generation"
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
                if resolved is not None:
                    await _close_resources_definitively(resolved)

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
        return _retry_collection(
            exc,
            artifact_store_connection_id=None,
            artifact_store_connection_version=None,
            artifact_store_connection_persisted=False,
        )

    async def _collect() -> tuple[list[JsonDict], TestResultSummary, str]:
        refs: list[JsonDict] = []
        resolved_artifact_connection_id = ""
        adapter: Any | None = None
        store: Any | None = None
        resolved: Any | None = None
        resolved_store: Any | None = None
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
                execution_connection_id = getattr(adapter, "_apex_resolved_connection_id", None)
                if execution_connection_id is None:
                    execution_connection_id = handle.connection_id
                connection_version = getattr(adapter, "_apex_resolved_connection_version", None)
            if not _safe_connection_id(execution_connection_id):
                raise RuntimeError("resolved execution connection has no durable id")

            resolved_store = await _resolve_artifact_store(
                cfg, connection_id=artifact_connection_id
            )
            store = resolved_store.adapter
            resolved_artifact_connection_id = resolved_store.connection_id
            if not _safe_connection_id(resolved_artifact_connection_id):
                raise _RequiredArtifactIndexError(
                    "artifact-store resolver returned an invalid connection id"
                )
            if resolved_artifact_connection_id != artifact_connection_id:
                raise _RequiredArtifactIndexError(
                    "artifact-store resolver did not honor checkpointed connection affinity"
                )
            if (
                resolved_store.connection_version != artifact_connection_version
                or resolved_store.persisted
                is not _checkpoint_flag(entry, "artifact_store_connection_persisted")
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
            try:
                collected = await adapter.collect_artifacts(
                    provider_handle,
                    store_view,
                )
                refs = _validated_engine_artifacts(
                    collected,
                    handle,
                    written=store_view.written,
                )
                retained_keys = {ref["key"] for ref in refs}
                await store_view.cleanup_except(retained_keys)
                # Fetch/validate the summary before indexing. Once ownership may
                # have committed, retries use the checkpointed normalized payload
                # below and never rewrite or delete those live objects.
                summary = _validated_engine_summary(
                    await adapter.fetch_summary(handle.model_copy(deep=True)),
                    expected_engine=handle.engine,
                )
            except BaseException:
                await store_view.cleanup_except(set())
                raise
        finally:
            # The owned resolution closes the leaf checkout and its complete
            # resolver tree (including nested SECRETS generations). Test seams
            # that return a raw adapter remain compatible with the same helper.
            await _close_resources_definitively(resolved, resolved_store)
        return refs, summary, resolved_artifact_connection_id

    pending_refs_raw = entry.get("engine_collection_pending_refs")
    pending_summary_raw = entry.get("engine_collection_pending_summary")
    if pending_refs_raw is not None or pending_summary_raw is not None:
        # A crash/retry after collection must resume at the exact staged index
        # payload and never re-enter provider/store writes.
        return Command(goto="engine_collection_index")

    try:
        refs, summary, artifact_connection_id = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001 - checkpoint a bounded exact retry
        return _retry_collection(exc)

    # Commit normalized refs + summary in their own sync checkpoint before the
    # first ownership-index transaction. Index retries can then be exact and can
    # never overwrite/delete an object whose commit acknowledgement was lost.
    return Command(
        goto="engine_collection_index",
        update=_update(
            attempt,
            status=PhaseStatus.RUNNING.value,
            engine_collection_pending_refs=refs,
            engine_collection_pending_summary=summary.model_dump(mode="json"),
            engine_collection_pending_connection_id=artifact_connection_id,
            engine_collection_index_required=True,
            engine_collection_index_failures=0,
            engine_collection_index_last_error=None,
        ),
    )


def engine_collection_index(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Index one checkpointed collection batch without provider or object-store I/O."""

    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)

    def _retry_index(exc: Exception) -> Command[str]:
        failures = (
            _checkpoint_int(
                entry.get("engine_collection_index_failures"),
                label="engine collection index failure count",
            )
            + 1
        )
        detail = bounded_diagnostic(exc, max_chars=2_048)
        message = (
            f"engine artifact indexing failed "
            f"({failures}/{MAX_ENGINE_COLLECTION_ATTEMPTS}): {detail}"
        )
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "engine_collection_index_error",
                "phase": _PHASE.value,
                "attempt": attempt,
                "external_run_id": handle.external_run_id,
                "failure": failures,
                "error": detail,
            }
        )
        blocked = failures >= MAX_ENGINE_COLLECTION_ATTEMPTS
        return Command(
            goto="engine_collection_blocked" if blocked else "engine_collection_index",
            update=_update(
                attempt,
                status=PhaseStatus.RUNNING.value,
                engine_collection_required=True,
                engine_collection_blocked=blocked,
                # Preserve the public retry diagnostics while also separating the
                # no-provider indexing stage for exact recovery routing.
                engine_collection_failures=failures,
                engine_collection_last_error=message,
                engine_collection_index_required=True,
                engine_collection_index_failures=failures,
                engine_collection_index_last_error=message,
            ),
        )

    try:
        raw_refs = entry.get("engine_collection_pending_refs")
        raw_summary = entry.get("engine_collection_pending_summary")
        pending_connection_id = entry.get("engine_collection_pending_connection_id")
        artifact_connection_id = entry.get("artifact_store_connection_id")
        if not _safe_connection_id(pending_connection_id) or not _safe_connection_id(
            artifact_connection_id
        ):
            raise ValueError("checkpointed engine artifact affinity is invalid")
        if pending_connection_id != artifact_connection_id:
            raise ValueError("checkpointed engine artifact affinity is invalid")
        if type(raw_refs) is not list:
            raise ValueError("checkpointed engine artifact refs are invalid")
        refs = _validated_engine_artifacts(raw_refs, handle)
        summary = _validated_engine_summary(raw_summary, expected_engine=handle.engine)
        last = entry.get("engine_poll_last")
        if type(last) is not dict:
            raise ValueError("checkpointed engine terminal status is invalid")
        raw_engine_phase = last.get("status")
        if type(raw_engine_phase) is not str or len(raw_engine_phase) > 32:
            raise ValueError("checkpointed engine terminal status is invalid")
        engine_phase: EngineRunPhase | None = None
        invalid_engine_phase = False
        try:
            engine_phase = EngineRunPhase(raw_engine_phase)
        except ValueError:
            invalid_engine_phase = True
        if invalid_engine_phase or engine_phase is None:
            raise ValueError("checkpointed engine terminal status is invalid")
        if engine_phase not in TERMINAL_ENGINE_PHASES:
            raise ValueError("checkpointed engine terminal status is invalid")
    except Exception as exc:  # noqa: BLE001 - malformed durable stage enters bounded recovery
        return _retry_index(exc)

    async def _index() -> None:
        await record_artifact_references(
            [
                ArtifactReferenceInput(
                    artifact_key=ref["key"],
                    kind=ref["kind"],
                )
                for ref in refs
            ],
            connection_id=pending_connection_id,
            thread_id=_thread_id(config),
            project_id=cfg.project_id,
            app_id=cfg.app_id,
        )

    try:
        asyncio.run(_index())
    except Exception:  # noqa: BLE001 - exact idempotent batch retry is required
        return _retry_index(_RequiredArtifactIndexError("required engine artifact indexing failed"))

    artifacts: list[JsonDict] = []
    for index, raw_ref in enumerate(refs):
        ref = ArtifactRef.model_validate(raw_ref).model_dump(mode="json")
        ref["id"] = f"{_PHASE.value}-a{attempt}-engine-artifact-{index}"
        ref["artifact_connection_id"] = pending_connection_id
        artifacts.append(ref)

    summary_json = summary.model_dump(mode="json")
    kpi_text = ", ".join(
        f"{key}={value:g}" if type(value) in {int, float} else f"{key}={value}"
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
        "artifact_store_connection_id": pending_connection_id,
        "engine_collection_required": False,
        "engine_collection_blocked": False,
        "engine_collection_failures": 0,
        "engine_collection_last_error": None,
        "engine_collection_index_required": False,
        "engine_collection_index_failures": 0,
        "engine_collection_index_last_error": None,
        "engine_collection_pending_refs": None,
        "engine_collection_pending_summary": None,
        "engine_collection_pending_connection_id": None,
        "engine_collection_completed_at": utcnow_iso(),
        "engine_collection_staged": True,
    }
    goto = "open_output_gate"
    final_status: str | None = None
    if engine_phase is EngineRunPhase.ABORTED:
        final_status = PhaseStatus.ABORTED.value
        fields["errors"] = [f"engine run {handle.external_run_id} was aborted"]
        goto = "finalize"
    elif engine_phase is EngineRunPhase.FAILED or not summary.passed:
        final_status = PhaseStatus.FAILED.value
        phase_errors = list(summary.sla_breaches)
        if not phase_errors:
            phase_errors = [
                _checkpoint_diagnostic_text(
                    last.get("message"),
                    default="engine run failed",
                )
            ]
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
    return Command(goto="engine_collection_settle", update=update)


def engine_collection_settle(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Replay-safe terminal projection and teardown after the collection checkpoint."""

    cfg = PipelineConfigurable.from_state_for_phase(state, config, _PHASE)
    entry = _entry(state)
    attempt = _attempt(entry)
    handle = _handle_from(state, entry)
    engine_options = _engine_options(entry, cfg.engine)
    if not _checkpoint_flag(entry, "engine_collection_staged"):
        raise RuntimeError("engine collection settle requires checkpointed collection output")

    raw_projected_phase = entry.get("engine_collection_projected_phase")
    if type(raw_projected_phase) is not str or len(raw_projected_phase) > 32:
        raise RuntimeError("staged engine collection has an invalid terminal phase")
    projected_phase: EngineRunPhase | None = None
    try:
        projected_phase = EngineRunPhase(raw_projected_phase)
    except ValueError:
        pass
    if projected_phase is None:
        raise RuntimeError("staged engine collection has an invalid terminal phase")
    if projected_phase not in TERMINAL_ENGINE_PHASES:
        raise RuntimeError("staged engine collection phase is not terminal")

    next_node = entry.get("engine_collection_next")
    final_status = entry.get("engine_collection_final_status")
    if type(next_node) is not str or next_node not in {"finalize", "open_output_gate"}:
        raise RuntimeError("staged engine collection has no continuation")
    if final_status is not None and (
        type(final_status) is not str
        or final_status not in {PhaseStatus.ABORTED.value, PhaseStatus.FAILED.value}
    ):
        raise RuntimeError("staged engine collection has an invalid final status")
    expected: tuple[str | None, str]
    if projected_phase is EngineRunPhase.ABORTED:
        expected = (PhaseStatus.ABORTED.value, "finalize")
    elif projected_phase is EngineRunPhase.FAILED:
        expected = (PhaseStatus.FAILED.value, "finalize")
    else:
        expected = (None, "open_output_gate")
    if (final_status, next_node) != expected:
        raise RuntimeError("staged engine collection outcome is inconsistent")

    summary = _validated_engine_summary(entry.get("test_summary"), expected_engine=handle.engine)
    artifact_connection_id = entry.get("artifact_store_connection_id")
    if not _safe_connection_id(artifact_connection_id):
        raise RuntimeError("staged engine collection has no valid artifact-store affinity")
    reservation_connection_id, connection_version = _connection_reservation_affinity(entry, handle)
    artifact_connection_version = _artifact_reservation_affinity(entry, artifact_connection_id)
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
            await _close_resources_definitively(adapter)

    try:
        if not recovered_terminal:
            asyncio.run(_teardown())
    except Exception as exc:  # noqa: BLE001 - collection is already durably recoverable
        detail = bounded_diagnostic(exc)
        failures = (
            _checkpoint_int(
                entry.get("engine_collection_settle_failures"),
                label="engine collection settle failure count",
            )
            + 1
        )
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
        _checkpoint_diagnostic_text(
            entry.get("engine_collection_settle_last_error"),
            default="engine teardown unavailable",
        )
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_collection_settle_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "actions": ["retry"],
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
    if not _checkpoint_flag(entry, "engine_collection_staged"):
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
    indexing = _checkpoint_flag(entry, "engine_collection_index_required")
    selected_error = (
        entry.get("engine_collection_index_last_error")
        if indexing
        else entry.get("engine_collection_last_error")
    )
    detail = _checkpoint_diagnostic_text(
        selected_error,
        default="engine collection unavailable",
    )
    interrupt(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": "engine_collection_retry",
            "phase": _PHASE.value,
            "attempt": _attempt(entry),
            "thread_id": _thread_id(config),
            "actions": ["retry"],
            "error": detail,
            "message": (
                "Engine result collection exhausted its retry budget. Resume to retry "
                "the exact checkpointed batch and pinned connections."
            ),
        }
    )
    return Command(
        goto="engine_collection_index" if indexing else "engine_collect",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_required=True,
            engine_collection_blocked=False,
            engine_collection_failures=0,
            engine_collection_last_error=None,
            engine_collection_index_failures=0,
            engine_collection_index_last_error=None,
        ),
    )


def engine_collection_resume(state: PipelineState, config: RunnableConfig) -> Command[str]:
    """Start a fresh bounded retry window on an operator-triggered thread run."""

    del config
    entry = _entry(state)
    indexing = _checkpoint_flag(entry, "engine_collection_index_required")
    return Command(
        goto="engine_collection_index" if indexing else "engine_collect",
        update=_update(
            _attempt(entry),
            status=PhaseStatus.RUNNING.value,
            engine_collection_required=True,
            engine_collection_blocked=False,
            engine_collection_failures=0,
            engine_collection_last_error=None,
            engine_collection_index_failures=0,
            engine_collection_index_last_error=None,
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
        destinations=("engine_cleanup", "engine_cleanup_blocked", "finalize"),
    )
    builder.add_node(
        "engine_cleanup_blocked",
        engine_cleanup_blocked,
        destinations=("engine_cleanup",),
    )
    builder.add_node(
        "engine_collect",
        engine_collect,
        destinations=(
            "engine_collect",
            "engine_collection_blocked",
            "engine_collection_index",
        ),
    )
    builder.add_node(
        "engine_collection_index",
        engine_collection_index,
        destinations=(
            "engine_collection_index",
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
        destinations=("engine_collect", "engine_collection_index"),
    )
    builder.add_node(
        "engine_collection_resume",
        engine_collection_resume,
        destinations=("engine_collect", "engine_collection_index"),
    )
    builder.add_edge("agent", "engine_reserve")


__all__ = [
    "SPINE_SUPERSTEPS",
    "add_execution_engine_nodes",
    "engine_cleanup",
    "engine_cleanup_blocked",
    "engine_collect",
    "engine_collection_blocked",
    "engine_collection_index",
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
