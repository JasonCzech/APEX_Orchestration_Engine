"""Dashboard-shaped read model + gate CAS resume over the loopback LangGraph API.

The facade never touches LangGraph storage directly: every call goes through the
SDK client created with the caller's forwarded API key, so the @auth.on handlers
scope visibility server-side. Verified against langgraph_sdk 0.4.2:

- ``threads.search`` returns ``Thread`` dicts with ``values`` and
  ``interrupts: dict[task_id, list[{id, value}]]`` inline — no per-thread N+1.
- ``threads.get_state`` returns ``tasks[].interrupts[].{id, value}`` (plus a
  flattened top-level ``interrupts`` list) — the interrupt id used for CAS.
- ``runs.create(..., multitask_strategy="reject")`` conflicts raise
  ``langgraph_sdk.errors.ConflictError`` (HTTP 409 from the server).
"""

import asyncio
import hashlib
import json
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import structlog
from langgraph_sdk.errors import APIStatusError, ConflictError, NotFoundError

from apex.domain.diagnostics import contains_credential_material, safe_type_name
from apex.domain.input_limits import (
    MAX_DESCRIPTION_CHARS,
    MAX_SCOPE_ID_CHARS,
    validate_json_object,
)
from apex.domain.pipeline import (
    PHASE_ORDER,
    TERMINAL_PHASE_STATUSES,
    ExternalResults,
    Phase,
    PhaseStatus,
    utcnow_iso,
)
from apex.graphs.pipeline.configurable import (
    MAX_RECOMMENDED_RECURSION_LIMIT,
    PipelineConfigurable,
)
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.services.langgraph_client import (
    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
    delete_native_thread_definitively,
)
from apex.services.launch_locks import LaunchLockManager
from apex.services.pipeline_public import (
    MAX_PUBLIC_PIPELINE_STATE_BYTES,
    MAX_PUBLIC_PIPELINE_STATE_NODES,
    public_gate,
    public_pipeline_state,
    public_prompt_review,
    public_text,
)
from apex.services.prompts import (
    prompt_review_from_resolved,
    resolve_phase_prompt_no_catalog,
    resolve_phase_prompt_sync,
)
from apex.services.public_projection import (
    native_run_stream_url,
    public_engine_handle_summary,
    validated_native_identifier,
    validated_native_mapping_page,
)
from apex.services.run_validation import (
    validate_context_packets,
    validate_gate_payload,
    validate_prompt_parts,
)

JsonDict = dict[str, Any]

PIPELINE_GRAPH_ID = "pipeline"
MAX_PIPELINE_QUERY_CHARS = 256
PIPELINE_TEXT_SCAN_PAGE_SIZE = 100
MAX_PIPELINE_TEXT_SCAN_RECORDS = 1_000
MAX_PUBLIC_PENDING_GATES = 100
MAX_PUBLIC_PENDING_GATES_BYTES = MAX_PUBLIC_PIPELINE_STATE_BYTES
MAX_PUBLIC_PENDING_GATES_NODES = MAX_PUBLIC_PIPELINE_STATE_NODES
MAX_LAUNCH_TITLE_CHARS = 500
MAX_LAUNCH_ASSISTANT_ID_CHARS = 256
MAX_LAUNCH_IDEMPOTENCY_KEY_CHARS = 128
PIPELINE_SUMMARY_SELECT = (
    "thread_id",
    "metadata",
    "status",
    "created_at",
    "updated_at",
    "interrupts",
)
PIPELINE_SUMMARY_EXTRACT = {
    "title": "values.title",
    "current_phase": "values.current_phase",
    "phase_results": "values.phase_results",
    "engine_handle": "values.engine_handle",
}
_AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES = frozenset({408, 409, 425, 429})
_RUN_LIST_PAGE_SIZE = 100
_MAX_LAUNCH_RUN_RECONCILE_RECORDS = 1_000
_MAX_ABORT_ACTIVE_RUNS = 1000
_MAX_RERUN_RECONCILE_RECORDS = 1_000
_ACTIVE_RUN_STABILITY_ATTEMPTS = 4
_ABORT_CANCEL_ROUNDS = 3
_UNSAFE_DURABLE_REPLAY_LOAD_TEST_FIELDS = frozenset({"script_refs", "test_id", "test_instance_id"})
logger = structlog.get_logger(__name__)


class LangGraphClientLike(Protocol):
    """Structural slice of langgraph_sdk LangGraphClient used by this service."""

    threads: Any
    runs: Any


class GateSupersededError(Exception):
    """The targeted interrupt is no longer pending (resolved or replaced)."""

    def __init__(self, thread_id: str, interrupt_id: str, pending_gate: JsonDict | None) -> None:
        self.thread_id = thread_id
        self.interrupt_id = interrupt_id
        self.pending_gate = public_gate(pending_gate, include_payload=False)
        super().__init__(f"gate {interrupt_id!r} is no longer pending on thread {thread_id!r}")


class InvalidGateActionError(Exception):
    """The requested action is not in the gate payload's allowed actions."""

    def __init__(self, action: str, allowed: list[str]) -> None:
        self.action = action
        self.allowed = allowed
        super().__init__(f"action {action!r} not allowed; expected one of {sorted(allowed)}")


class NoActiveRunError(Exception):
    """Abort requested but the thread has no pending or running run."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"thread {thread_id!r} has no pending or running run")


class TooManyActiveRunsError(Exception):
    """A legacy queued-run backlog exceeds the bounded abort work budget."""

    def __init__(self, thread_id: str, limit: int = _MAX_ABORT_ACTIVE_RUNS) -> None:
        self.thread_id = thread_id
        self.limit = limit
        super().__init__(
            f"thread {thread_id!r} has more than {limit} active runs; "
            "repeat cleanup with an operator runbook"
        )


class ActiveRunSnapshotUnstableError(Exception):
    """The volatile active-run collection never produced a stable bounded view."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"active runs for thread {thread_id!r} changed throughout the abort snapshot"
        )


class LaunchIdempotencyConflictError(Exception):
    """An idempotency key was reused for a different scoped request."""


class LaunchProviderError(Exception):
    """The native runtime failed or returned malformed launch data."""


class PromptReviewConflictError(Exception):
    """A prompt draft can no longer be edited at the observed checkpoint."""


class RerunConfigurationConflictError(Exception):
    """The durable rerun snapshot is missing, malformed, or ownership-drifted."""


class RerunIdempotencyConflictError(Exception):
    """A rerun idempotency claim was reused for different overrides."""


class RerunActiveRunConflictError(Exception):
    """The native runtime rejected a rerun because another run is active."""


async def _thread_and_state_definitively(
    client: LangGraphClientLike, thread_id: str
) -> tuple[Any, Any]:
    """Read a native thread snapshot without abandoning the sibling request.

    ``asyncio.gather`` leaves other awaitables running when one raises. These
    reads execute while a cross-request mutation lock is held, so a detached
    sibling could keep consuming loopback I/O after the operation and lock scope
    have exited. Cancel and retrieve both tasks on every exceptional exit,
    including repeated parent cancellation.
    """

    tasks = [
        asyncio.create_task(client.threads.get(thread_id)),
        asyncio.create_task(client.threads.get_state(thread_id)),
    ]
    try:
        results = await asyncio.gather(*tasks)
        return results[0], results[1]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        async def settle() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)

        waiter = asyncio.create_task(settle())
        interrupted = False
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                interrupted = True
        waiter.result()
        if interrupted:
            raise asyncio.CancelledError from None


# ── Pure mapping helpers ─────────────────────────────────────────────────────


def build_phase_strip(values: JsonDict | None) -> list[JsonDict]:
    """Canonical-order strip from state ``phase_results``; absent phases -> "none"."""
    public_values = public_pipeline_state(values or {})
    results = public_values.get("phase_results") or {}
    strip: list[JsonDict] = []
    for phase in PHASE_ORDER:
        entry = results.get(phase.value)
        if isinstance(entry, dict):
            strip.append(
                {
                    "phase": phase.value,
                    "status": entry.get("status") or "none",
                    "attempt": entry.get("attempt"),
                }
            )
        else:
            strip.append({"phase": phase.value, "status": "none", "attempt": None})
    return strip


def _validated_run_create_id(value: Any, *, label: str) -> str:
    """Extract one loopback run id without invoking attacker-defined mapping hooks."""

    if type(value) is not dict:
        raise RuntimeError(f"{label} returned an invalid response")
    run_id = value.get("run_id")
    if type(run_id) is not str:
        raise RuntimeError(f"{label} returned an invalid identifier")
    return validated_native_identifier(run_id, label=label)


def _validated_thread_create_fields(value: Any, *, label: str) -> tuple[str, JsonDict | None]:
    """Extract bounded launch fields without trusting provider mapping hooks."""

    if type(value) is not dict:
        raise RuntimeError(f"{label} returned an invalid response")
    thread_id = validated_native_identifier(value.get("thread_id"), label=label)
    metadata = value.get("metadata")
    if metadata is None:
        return thread_id, None
    if (
        type(metadata) is not dict
        or len(metadata) > 64
        or any(type(key) is not str or not 1 <= len(key) <= 255 for key in metadata)
    ):
        raise RuntimeError(f"{label} returned invalid metadata")
    return thread_id, dict(metadata)


def _validated_launch_text(
    value: Any,
    *,
    label: str,
    max_chars: int,
    allow_empty: bool,
    canonical_whitespace: bool = False,
) -> str:
    """Require one exact bounded service-layer launch string."""

    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value) > max_chars
        or "\x00" in value
        or (canonical_whitespace and value != value.strip())
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _validated_optional_launch_text(
    value: Any,
    *,
    label: str,
    max_chars: int,
    canonical_whitespace: bool = False,
) -> str | None:
    if value is None:
        return None
    return _validated_launch_text(
        value,
        label=label,
        max_chars=max_chars,
        allow_empty=False,
        canonical_whitespace=canonical_whitespace,
    )


def engine_info_from_values(values: JsonDict | None) -> JsonDict | None:
    """Tiny engine summary from state ``engine_handle`` (None when absent/malformed)."""
    return public_engine_handle_summary((values or {}).get("engine_handle"))


def _gate_info(interrupt: Any) -> JsonDict:
    if type(interrupt) is not dict:
        return {}
    value = interrupt.get("value")
    payload: JsonDict = value if type(value) is dict else {}
    return {
        "interrupt_id": interrupt.get("id"),
        "kind": payload.get("kind"),
        "phase": payload.get("phase"),
        "payload": payload,
    }


def _extend_pending_gates(gates: list[JsonDict], interrupts: Any) -> bool:
    """Append in provider order and stop at the aggregate public gate budget."""

    if type(interrupts) is not list or len(interrupts) > MAX_PUBLIC_PENDING_GATES:
        return False
    for interrupt in interrupts:
        if len(gates) >= MAX_PUBLIC_PENDING_GATES:
            return True
        info = _gate_info(interrupt)
        if info:
            gates.append(info)
    return len(gates) >= MAX_PUBLIC_PENDING_GATES


def pending_gates_from_thread(thread: JsonDict) -> list[JsonDict]:
    """Gate infos from a Thread's ``interrupts`` mapping (task_id -> interrupts)."""
    gates: list[JsonDict] = []
    if type(thread) is not dict:
        return gates
    mapping = thread.get("interrupts")
    if type(mapping) is not dict or len(mapping) > MAX_PUBLIC_PENDING_GATES:
        return gates
    for interrupts in mapping.values():
        if _extend_pending_gates(gates, interrupts):
            break
    return gates


def pending_gates_from_state(state: JsonDict) -> list[JsonDict]:
    """Gate infos from a ThreadState: tasks[].interrupts, else top-level interrupts."""
    gates: list[JsonDict] = []
    if type(state) is not dict:
        return gates
    tasks = state.get("tasks")
    if type(tasks) is list and len(tasks) <= MAX_PUBLIC_PENDING_GATES:
        for task in tasks:
            if type(task) is not dict:
                continue
            interrupts = task.get("interrupts")
            if _extend_pending_gates(gates, interrupts):
                return gates
    if not gates:
        interrupts = state.get("interrupts")
        _extend_pending_gates(gates, interrupts)
    return gates


def _bounded_json_cost(
    value: JsonDict,
    *,
    max_bytes: int,
    max_nodes: int,
) -> tuple[int, int] | None:
    """Measure one already-projected object without encoding an aggregate copy."""

    stack: list[Any] = [value]
    nodes = 0
    while stack:
        current = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            return None
        if type(current) is dict:
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
        elif current is None or type(current) in {str, bool, int, float}:
            continue
        else:
            return None

    encoded_bytes = 0
    try:
        chunks = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).iterencode(value)
        for chunk in chunks:
            encoded_bytes += len(chunk.encode("utf-8"))
            if encoded_bytes > max_bytes:
                return None
    except (OverflowError, RecursionError, TypeError, ValueError):
        return None
    return encoded_bytes, nodes


def _project_pending_gates(gates: list[JsonDict]) -> list[JsonDict]:
    """Project the provider-ordered prefix within one aggregate response budget.

    Each candidate is individually bounded by ``public_gate``. Cost is then
    measured incrementally, so preflight never builds or serializes the possible
    100-gate aggregate before deciding it is too large.
    """

    projected_gates: list[JsonDict] = []
    used_bytes = 2  # JSON list brackets
    used_nodes = 1  # JSON list container
    for gate in gates:
        projected = public_gate(gate, include_payload=True)
        if projected is None:
            continue
        separator_bytes = 1 if projected_gates else 0
        remaining_bytes = MAX_PUBLIC_PENDING_GATES_BYTES - used_bytes - separator_bytes
        remaining_nodes = MAX_PUBLIC_PENDING_GATES_NODES - used_nodes
        if remaining_bytes < 1 or remaining_nodes < 1:
            break
        cost = _bounded_json_cost(
            projected,
            max_bytes=remaining_bytes,
            max_nodes=remaining_nodes,
        )
        if cost is None:
            break
        encoded_bytes, nodes = cost
        projected_gates.append(projected)
        used_bytes += separator_bytes + encoded_bytes
        used_nodes += nodes
    return projected_gates


def _public_gate(gate: JsonDict | None) -> JsonDict | None:
    return public_gate(gate, include_payload=False)


def phase_by_name(name: str) -> Phase:
    for phase in PHASE_ORDER:
        if phase.value == name:
            return phase
    # Phase names can originate in legacy checkpoints as well as route input.
    # Keep the traced/public diagnostic opaque instead of reflecting either.
    raise ValueError("unknown pipeline phase")


def _application_override_content(values: JsonDict, app_id: str | None) -> str | None:
    """Run-scoped, app-wide application prompt override content, if set."""
    if not app_id:
        return None
    reviews = values.get("application_reviews")
    if type(reviews) is not dict:
        return None
    override = reviews.get(app_id)
    if type(override) is not dict:
        return None
    content = override.get("content")
    return content if type(content) is str else None


def _with_application_override(review: JsonDict, values: JsonDict, app_id: str | None) -> JsonDict:
    override = _application_override_content(values, app_id)
    if override is not None:
        return {**review, "application": override}
    return review


def map_thread_summary(thread: JsonDict) -> JsonDict:
    """Thread dict (search/get shape) -> dashboard pipeline summary."""
    raw_values = thread.get("values")
    if isinstance(raw_values, dict):
        values = raw_values
    else:
        extracted = thread.get("extracted")
        values = dict(extracted) if isinstance(extracted, dict) else {}
    public_values = public_pipeline_state(values)
    raw_metadata = thread.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    gates = pending_gates_from_thread(thread)
    return {
        "thread_id": public_text(thread.get("thread_id"), 256, allow_empty=False) or "",
        "title": public_values.get("title") or public_text(metadata.get("title"), 500),
        "project_id": public_text(metadata.get("project_id"), 255),
        "app_id": public_text(metadata.get("app_id"), 255),
        "thread_status": public_text(thread.get("status"), 32),
        "current_phase": public_values.get("current_phase"),
        "phase_strip": build_phase_strip(public_values),
        "engine": engine_info_from_values(public_values),
        "created_at": public_text(thread.get("created_at"), 64),
        "updated_at": public_text(thread.get("updated_at"), 64),
        "pending_gate": _public_gate(gates[0] if gates else None),
    }


def _auto_gates(phases: list[str] | None) -> JsonDict:
    """Auto (un-gated) policy for the selected phases — the default for headless runs."""
    targets = phases or [phase.value for phase in PHASE_ORDER]
    return {name: {"prompt_review": "auto", "output_review": "auto"} for name in targets}


# ── Service ──────────────────────────────────────────────────────────────────


class PipelineReadService:
    """Facade over the loopback client; constructed per-request with the caller's key."""

    def __init__(
        self, client: LangGraphClientLike, launch_locks: LaunchLockManager | None = None
    ) -> None:
        self._client = client
        self._launch_locks = launch_locks or LaunchLockManager()

    async def start_run(
        self,
        *,
        title: str,
        request: str = "",
        idempotency_key: str | None = None,
        assistant_id: str | None = None,
        project_id: str | None = None,
        app_id: str | None = None,
        configurable: JsonDict | None = None,
        phases: list[str] | None = None,
        gates: JsonDict | None = None,
        agent_backend: str | None = None,
        model_by_phase: JsonDict | None = None,
        external_results: JsonDict | None = None,
        context_packets: list[JsonDict] | None = None,
        principal_id: str | None = None,
    ) -> JsonDict:
        """Create a thread and start a pipeline run; returns {thread_id, run_id, stream_url}.

        Convenience entrypoint for external clients (e.g. a results-analysis dashboard):
        wraps thread-create + run-start over the loopback API so callers don't drive the
        raw LangGraph surface. Gates default to "auto" for the selected phases so an
        unattended analysis run completes without an operator resuming gates; pass an
        explicit `gates` map for interactive runs. Raises ValueError on unknown phases.
        """
        title = _validated_launch_text(
            title,
            label="pipeline title",
            max_chars=MAX_LAUNCH_TITLE_CHARS,
            allow_empty=False,
        )
        request = _validated_launch_text(
            request,
            label="pipeline request",
            max_chars=MAX_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        assistant_id = _validated_optional_launch_text(
            assistant_id,
            label="pipeline assistant_id",
            max_chars=MAX_LAUNCH_ASSISTANT_ID_CHARS,
        )
        project_id = _validated_optional_launch_text(
            project_id,
            label="pipeline project_id",
            max_chars=MAX_SCOPE_ID_CHARS,
            canonical_whitespace=True,
        )
        app_id = _validated_optional_launch_text(
            app_id,
            label="pipeline app_id",
            max_chars=MAX_SCOPE_ID_CHARS,
            canonical_whitespace=True,
        )
        idempotency_key = _validated_optional_launch_text(
            idempotency_key,
            label="pipeline idempotency_key",
            max_chars=MAX_LAUNCH_IDEMPOTENCY_KEY_CHARS,
        )
        principal_id = _validated_optional_launch_text(
            principal_id,
            label="pipeline principal_id",
            max_chars=MAX_SCOPE_ID_CHARS,
        )
        agent_backend = _validated_optional_launch_text(
            agent_backend,
            label="pipeline agent_backend",
            max_chars=32,
        )
        if configurable is None:
            run_configurable: JsonDict = {}
        else:
            if type(configurable) is not dict:
                raise ValueError("pipeline configurable is invalid")
            validate_json_object(configurable, label="pipeline configurable")
            run_configurable = dict(configurable)
        if phases is not None and (
            type(phases) is not list
            or len(phases) > len(PHASE_ORDER)
            or any(type(name) is not str for name in phases)
        ):
            raise ValueError("pipeline phases are invalid")
        if gates is not None:
            if type(gates) is not dict:
                raise ValueError("pipeline gates are invalid")
            validate_json_object(gates, label="pipeline gates")
        if model_by_phase is not None:
            if type(model_by_phase) is not dict:
                raise ValueError("pipeline model_by_phase is invalid")
            validate_json_object(model_by_phase, label="pipeline model_by_phase")
        if external_results is not None:
            if type(external_results) is not dict:
                raise ValueError("pipeline external_results are invalid")
            validate_json_object(external_results, label="pipeline external_results")

        selected_assistant_id = assistant_id or PIPELINE_GRAPH_ID
        run_configurable["assistant_id"] = selected_assistant_id
        configured_phases = run_configurable.get("phases")
        requested_phases = phases
        if requested_phases is None and type(configured_phases) is list:
            requested_phases = configured_phases
        if requested_phases:
            known = {phase.value for phase in PHASE_ORDER}
            if any(type(name) is not str or name not in known for name in requested_phases) or len(
                set(requested_phases)
            ) != len(requested_phases):
                raise ValueError("unknown pipeline phase")

        if project_id:
            run_configurable["project_id"] = project_id
        if app_id:
            run_configurable["app_id"] = app_id
        if phases:
            run_configurable["phases"] = phases
        if agent_backend:
            run_configurable["agent_backend"] = agent_backend
        if model_by_phase:
            run_configurable["model_by_phase"] = model_by_phase
        inherited_gates = run_configurable.get("gates")
        resolved_gates = (
            gates
            if gates is not None
            else inherited_gates
            if type(inherited_gates) is dict
            else _auto_gates(requested_phases)
        )
        if resolved_gates:
            run_configurable["gates"] = resolved_gates

        # Validate the sparse layer without replacing it with model defaults: the
        # selected assistant's pinned configuration must remain able to fill fields
        # the caller omitted. Graph execution validates the final merged layer again.
        validated_config = PipelineConfigurable.model_validate(run_configurable)
        recursion_limit = (
            recommended_recursion_limit(validated_config.limits)
            if "limits" in run_configurable
            else MAX_RECOMMENDED_RECURSION_LIMIT
        )

        run_input: JsonDict = {"title": title, "request": request}
        if external_results is not None:
            run_input["external_results"] = ExternalResults.model_validate(
                external_results
            ).model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        if context_packets is not None:
            validated_packets = validate_context_packets(context_packets)
            if validated_packets:
                run_input["context_packets"] = validated_packets

        # This facade intentionally uses a trusted loopback identity after its
        # own scope checks. Reapply the native durable-write credential barrier
        # here before either the thread metadata or graph checkpoint can exist.
        if contains_credential_material({"input": run_input, "configurable": run_configurable}):
            raise ValueError("pipeline launch must not contain credential material")

        metadata: JsonDict = {"title": title}
        deterministic_thread_id: str | None = None
        request_fingerprint: str | None = None
        if idempotency_key:
            idempotency_scope = {
                "principal_id": principal_id or "unknown",
                "project_id": project_id,
                "app_id": app_id,
                "idempotency_key": idempotency_key,
            }
            deterministic_thread_id = str(
                uuid5(
                    NAMESPACE_URL,
                    "apex-launch:"
                    + json.dumps(idempotency_scope, sort_keys=True, separators=(",", ":")),
                )
            )
            request_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "assistant_id": selected_assistant_id,
                        "input": run_input,
                        "configurable": run_configurable,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            metadata["launch_idempotency_fingerprint"] = request_fingerprint
        if project_id:
            metadata["project_id"] = project_id
        if app_id:
            metadata["app_id"] = app_id
        if deterministic_thread_id is not None:
            async with self._launch_locks.hold(deterministic_thread_id):
                return await self._create_or_get_run(
                    metadata=metadata,
                    deterministic_thread_id=deterministic_thread_id,
                    request_fingerprint=request_fingerprint,
                    idempotency_key=idempotency_key,
                    selected_assistant_id=selected_assistant_id,
                    run_input=run_input,
                    run_configurable=run_configurable,
                    recursion_limit=recursion_limit,
                )
        return await self._create_or_get_run(
            metadata=metadata,
            deterministic_thread_id=None,
            request_fingerprint=None,
            idempotency_key=None,
            selected_assistant_id=selected_assistant_id,
            run_input=run_input,
            run_configurable=run_configurable,
            recursion_limit=recursion_limit,
        )

    async def _create_or_get_run(
        self,
        *,
        metadata: JsonDict,
        deterministic_thread_id: str | None,
        request_fingerprint: str | None,
        idempotency_key: str | None,
        selected_assistant_id: str,
        run_input: JsonDict,
        run_configurable: JsonDict,
        recursion_limit: int,
    ) -> JsonDict:
        """Create/adopt the run while the idempotency scope lock is held."""
        thread: JsonDict | None = None
        thread_id: str | None = None
        thread_metadata: JsonDict | None = None
        thread_error_type: str | None = None
        try:
            thread_response = await self._client.threads.create(
                metadata=metadata,
                **(
                    {"thread_id": deterministic_thread_id, "if_exists": "do_nothing"}
                    if deterministic_thread_id is not None
                    else {}
                ),
            )
            thread_id, thread_metadata = _validated_thread_create_fields(
                thread_response,
                label="pipeline thread creation",
            )
            thread = thread_response
        except Exception as exc:  # noqa: BLE001 - provider boundary normalization
            thread_error_type = safe_type_name(exc)
        if thread_error_type is not None:
            logger.warning(
                "pipeline.thread_create_failed",
                deterministic=deterministic_thread_id is not None,
                error_type=thread_error_type,
            )
            raise LaunchProviderError("pipeline runtime thread creation failed")
        if thread is None or thread_id is None:  # pragma: no cover - client contract invariant
            raise LaunchProviderError("pipeline runtime thread creation failed")
        if deterministic_thread_id is not None and thread_id != deterministic_thread_id:
            raise LaunchIdempotencyConflictError(
                "the deterministic thread response did not match the idempotency claim"
            )

        if idempotency_key:
            existing_fingerprint = (
                thread_metadata.get("launch_idempotency_fingerprint")
                if thread_metadata is not None
                else None
            )
            # A deterministic id is guessable by design. Only a thread stamped
            # atomically by this launch path is adoptable; a same-scope native
            # thread with missing metadata must not become someone else's run.
            if type(existing_fingerprint) is not str or existing_fingerprint != request_fingerprint:
                raise LaunchIdempotencyConflictError(
                    "idempotency_key was already used for a different request"
                )
            existing_run = await self._find_launch_root_run(
                thread_id,
                request_fingerprint=request_fingerprint,
            )
            if existing_run is not None:
                existing_run_id = validated_native_identifier(
                    existing_run.get("run_id"),
                    label="pipeline launch reconciliation",
                )
                return {
                    "thread_id": thread_id,
                    "run_id": existing_run_id,
                    "stream_url": native_run_stream_url(thread_id, existing_run_id),
                }

        run_id: str | None = None
        create_conflict = False
        provider_failure = False
        provider_failure_definitive = False
        provider_status_code: int | None = None
        provider_error_type: str | None = None
        try:
            run = await self._client.runs.create(
                thread_id,
                selected_assistant_id,
                input=run_input,
                config={
                    "configurable": run_configurable,
                    # An assistant may supply limits omitted by the sparse caller
                    # layer. Use the hard safe ceiling in that case so inherited
                    # long-running polls are not launched with the default budget.
                    "recursion_limit": recursion_limit,
                },
                stream_mode="custom",
                stream_subgraphs=True,
                stream_resumable=True,
                **(
                    {
                        "metadata": {
                            LAUNCH_ROOT_FINGERPRINT_METADATA_KEY: request_fingerprint,
                        }
                    }
                    if idempotency_key and request_fingerprint is not None
                    else {}
                ),
                durability="sync",
                multitask_strategy="reject",
            )
            run_id = _validated_run_create_id(run, label="pipeline run creation")
        except ConflictError:
            create_conflict = True
        except APIStatusError as exc:
            provider_failure = True
            provider_status_code = exc.status_code
            provider_error_type = safe_type_name(exc)
            provider_failure_definitive = (
                400 <= exc.status_code < 500
                and exc.status_code not in _AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary normalization
            provider_failure = True
            provider_error_type = safe_type_name(exc)
        if create_conflict:
            if idempotency_key:
                # A concurrent caller won the run-create race on the deterministic
                # thread. Wait briefly for its run row to become visible.
                for delay in (0.0, 0.02, 0.05, 0.1):
                    if delay:
                        await asyncio.sleep(delay)
                    existing_run = await self._find_launch_root_run(
                        thread_id,
                        request_fingerprint=request_fingerprint,
                    )
                    if existing_run is not None:
                        existing_run_id = validated_native_identifier(
                            existing_run.get("run_id"),
                            label="pipeline launch reconciliation",
                        )
                        return {
                            "thread_id": thread_id,
                            "run_id": existing_run_id,
                            "stream_url": native_run_stream_url(thread_id, existing_run_id),
                        }
            raise LaunchIdempotencyConflictError(
                "pipeline launch was rejected because the thread already has an active run"
            )
        if provider_failure:
            if provider_failure_definitive and deterministic_thread_id is None:
                # The server definitively rejected this non-idempotent launch, so
                # its freshly-created random thread cannot contain a committed run.
                # Deterministic threads are durable idempotency claims and remain.
                try:
                    await delete_native_thread_definitively(self._client, thread_id)
                except Exception as cleanup_exc:  # cleanup failure must not mask the launch error
                    logger.warning(
                        "pipeline.rejected_launch_thread_cleanup_failed",
                        thread_id=thread_id,
                        status_code=provider_status_code,
                        error_type=safe_type_name(cleanup_exc),
                    )
            else:
                # A 5xx can be returned after the server committed the run.
                logger.warning(
                    "pipeline.run_create_ambiguous",
                    thread_id=thread_id,
                    status_code=provider_status_code,
                    error_type=provider_error_type,
                )
            raise LaunchProviderError("pipeline runtime run creation failed")
        if run_id is None:  # pragma: no cover - client contract invariant
            raise LaunchProviderError("pipeline runtime run creation failed")
        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "stream_url": native_run_stream_url(thread_id, run_id),
        }

    async def _find_launch_root_run(
        self,
        thread_id: str,
        *,
        request_fingerprint: str | None,
    ) -> JsonDict | None:
        """Find the original run for an idempotent launch without adopting a resume.

        New runs carry a server-owned root fingerprint. For launch threads from
        before that marker existed, the oldest run is the only safe fallback, and
        only after a complete bounded scan proves the full history was observed.
        Exceeding the bound fails closed so a retry can never create or adopt an
        arbitrary run from a partial page.
        """
        offset = 0
        legacy_oldest: JsonDict | None = None
        saw_root_marker = False
        while offset < _MAX_LAUNCH_RUN_RECONCILE_RECORDS:
            limit = min(
                _RUN_LIST_PAGE_SIZE,
                _MAX_LAUNCH_RUN_RECONCILE_RECORDS - offset,
            )
            page: list[JsonDict] | None = None
            lookup_error_type: str | None = None
            try:
                page = validated_native_mapping_page(
                    await self._client.runs.list(
                        thread_id,
                        limit=limit,
                        offset=offset,
                        select=["run_id", "status", "metadata", "created_at"],
                    ),
                    requested_limit=limit,
                    label="pipeline launch run search",
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary normalization
                lookup_error_type = safe_type_name(exc)
            if lookup_error_type is not None or page is None:
                logger.warning(
                    "pipeline.launch_reconciliation_failed",
                    thread_id=thread_id,
                    error_type=lookup_error_type,
                )
                raise LaunchProviderError("pipeline launch reconciliation failed")
            for candidate in page:
                candidate_metadata = candidate.get("metadata")
                if type(candidate_metadata) is dict and (
                    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY in candidate_metadata
                ):
                    saw_root_marker = True
                    marker = candidate_metadata.get(LAUNCH_ROOT_FINGERPRINT_METADATA_KEY)
                    if type(marker) is str and marker == request_fingerprint:
                        return candidate
                legacy_oldest = candidate
            offset += len(page)
            if len(page) < limit:
                if saw_root_marker:
                    raise LaunchIdempotencyConflictError(
                        "launch root fingerprint does not match the idempotent request"
                    )
                return legacy_oldest

        # A full page at the scan boundary does not prove there are no more runs.
        # Never fall back to the page-local oldest candidate or start another run.
        raise LaunchIdempotencyConflictError(
            "launch run history exceeds the bounded reconciliation limit"
        )

    async def list_pipelines(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[JsonDict]:
        """Search threads and map them to pipeline summaries.

        Auth scoping is enforced server-side via the forwarded key. The `project`
        filter passes through as a metadata filter. `q` is applied client-side to
        the returned page (title/thread_id substring) because thread search has no
        free-text filter — matches within the page only, documented contract quirk.
        """
        metadata = {"project_id": project} if project else None
        if not q:
            threads = validated_native_mapping_page(
                await self._client.threads.search(
                    metadata=metadata,
                    status=status,
                    limit=limit,
                    offset=offset,
                    sort_by="updated_at",
                    sort_order="desc",
                    select=list(PIPELINE_SUMMARY_SELECT),
                    extract=dict(PIPELINE_SUMMARY_EXTRACT),
                ),
                requested_limit=limit,
                label="pipeline thread search",
            )
            return [map_thread_summary(thread) for thread in threads]

        # LangGraph has no free-text search. Scan a fixed, recent prefix before
        # applying the dashboard offset. The hard aggregate cap prevents one
        # no-match request from issuing hundreds of full thread searches.
        if len(q) > MAX_PIPELINE_QUERY_CHARS:
            raise ValueError(
                f"pipeline query must not exceed {MAX_PIPELINE_QUERY_CHARS} characters"
            )
        needle = q.casefold()
        matches: list[JsonDict] = []
        scan_offset = 0
        while scan_offset < MAX_PIPELINE_TEXT_SCAN_RECORDS:
            scan_limit = min(
                PIPELINE_TEXT_SCAN_PAGE_SIZE,
                MAX_PIPELINE_TEXT_SCAN_RECORDS - scan_offset,
            )
            threads = validated_native_mapping_page(
                await self._client.threads.search(
                    metadata=metadata,
                    status=status,
                    limit=scan_limit,
                    offset=scan_offset,
                    sort_by="updated_at",
                    sort_order="desc",
                    select=list(PIPELINE_SUMMARY_SELECT),
                    extract=dict(PIPELINE_SUMMARY_EXTRACT),
                ),
                requested_limit=scan_limit,
                label="pipeline text-search thread page",
            )
            page = [map_thread_summary(thread) for thread in threads]
            matches.extend(
                item
                for item in page
                if needle in (item["title"] or "").casefold()
                or needle in (item["thread_id"] or "").casefold()
            )
            if len(matches) >= offset + limit:
                break
            if len(page) < scan_limit:
                break
            scan_offset += scan_limit
        return matches[offset : offset + limit]

    async def get_pipeline(self, thread_id: str) -> JsonDict:
        """Thread summary plus an explicit public checkpoint/gate projection."""
        thread = await self._client.threads.get(thread_id)
        state = await self._client.threads.get_state(thread_id)
        if type(thread) is not dict or type(state) is not dict:
            raise RuntimeError("pipeline read provider returned an invalid response")
        gates = _project_pending_gates(pending_gates_from_state(state))
        summary = map_thread_summary(thread)
        summary["pending_gate"] = _public_gate(gates[0] if gates else None)
        return {
            **summary,
            "values": public_pipeline_state(state.get("values")),
            "interrupts": gates,
        }

    async def get_phase_prompt_review(self, thread_id: str, phase_name: str) -> JsonDict:
        """Effective prompt-review draft for a phase, with old-run fallback.

        The application prompt is layered from the run-scoped, app-wide override
        so every phase reports the same application text.
        """
        phase = phase_by_name(phase_name)
        thread = await self._client.threads.get(thread_id)
        state = await self._client.threads.get_state(thread_id)
        raw_values = state.get("values") or {}
        values = public_pipeline_state(raw_values)
        metadata = thread.get("metadata") if isinstance(thread.get("metadata"), dict) else {}
        app_id = public_text(metadata.get("app_id"), 255)

        entry = (values.get("phase_results") or {}).get(phase.value) or {}
        prompt = entry.get("resolved_prompt")
        status = entry.get("status")
        # Once a phase has executed, its immutable resolved prompt is the historical
        # record. Never let a later mutable draft make the API claim different input
        # was used for a terminal result.
        if (
            status in TERMINAL_PHASE_STATUSES
            and isinstance(prompt, dict)
            and (prompt.get("system") or prompt.get("user"))
        ):
            source = entry.get("resolved_prompt_source")
            public_review = public_prompt_review(
                {
                    "system": prompt.get("system") or "",
                    "phase_prompt": prompt.get("user") or "",
                    "application": prompt.get("application"),
                    "additional_context": "",
                    "source": (dict(source) if isinstance(source, dict) else {"origin": "catalog"}),
                    "updated_at": utcnow_iso(),
                    "updated_by": "system",
                }
            )
            if public_review is not None:
                return public_review

        review = (values.get("prompt_reviews") or {}).get(phase.value)
        if isinstance(review, dict):
            public_review = public_prompt_review(
                _with_application_override(dict(review), values, app_id)
            )
            if public_review is not None:
                return public_review

        if isinstance(prompt, dict) and (prompt.get("system") or prompt.get("user")):
            source = entry.get("resolved_prompt_source")
            public_review = public_prompt_review(
                _with_application_override(
                    {
                        "system": prompt.get("system") or "",
                        "phase_prompt": prompt.get("user") or "",
                        "application": prompt.get("application"),
                        "additional_context": "",
                        "source": (
                            dict(source) if isinstance(source, dict) else {"origin": "catalog"}
                        ),
                        "updated_at": utcnow_iso(),
                        "updated_by": "system",
                    },
                    values,
                    app_id,
                )
            )
            if public_review is not None:
                return public_review

        safe_project_id = public_text(metadata.get("project_id"), 255)
        cfg = PipelineConfigurable(project_id=safe_project_id, app_id=app_id)
        variables = {
            "title": (
                values.get("title") or public_text(metadata.get("title"), 500) or "untitled run"
            ),
            "request": public_text(raw_values.get("request"), 20_000) or "(no request provided)",
        }
        try:
            resolved = resolve_phase_prompt_sync(phase, cfg, variables=variables)
        except Exception:
            resolved = resolve_phase_prompt_no_catalog(phase, cfg, variables=variables)
        projected = public_prompt_review(
            _with_application_override(dict(prompt_review_from_resolved(resolved)), values, app_id)
        )
        if projected is None:
            raise ValueError("prompt review state is malformed")
        return projected

    async def update_phase_prompt_review(
        self,
        thread_id: str,
        phase_name: str,
        body: JsonDict,
        *,
        actor: str,
    ) -> JsonDict:
        """Patch one phase's run-scoped prompt review draft without starting a run.

        Per-phase fields (system / phase prompt / additional context) are stored
        under prompt_reviews[phase]. The application prompt is app-wide: when it
        changes it is written once under application_reviews[app_id] so the edit
        propagates to every phase of the run.
        """
        allowed_fields = {"system", "phase_prompt", "application", "additional_context"}
        if (
            type(body) is not dict
            or len(body) > len(allowed_fields)
            or any(type(key) is not str or key not in allowed_fields for key in body)
            or type(actor) is not str
            or public_text(actor, 255, allow_empty=False) != actor
        ):
            raise ValueError("prompt review fields are invalid")

        def required_text(field: str) -> str:
            value = body.get(field)
            if value is None:
                return ""
            if type(value) is not str:
                raise ValueError("prompt review fields are invalid")
            return value

        system = required_text("system")
        phase_prompt = required_text("phase_prompt")
        additional_context = required_text("additional_context")
        raw_application = body.get("application")
        if raw_application is not None and type(raw_application) is not str:
            raise ValueError("prompt review fields are invalid")
        body_application = raw_application
        validate_prompt_parts(
            system=system,
            user=phase_prompt,
            application=body_application,
            additional_context=additional_context,
        )
        if contains_credential_material(
            {
                "system": system,
                "phase_prompt": phase_prompt,
                "application": body_application,
                "additional_context": additional_context,
                "actor": actor,
            }
        ):
            raise ValueError("prompt review must not contain credential material")
        phase = phase_by_name(phase_name)
        async with self._launch_locks.hold(f"prompt-review:{thread_id}"):
            thread = await self._client.threads.get(thread_id)
            if type(thread) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            metadata = thread.get("metadata")
            if metadata is None:
                metadata = {}
            if type(metadata) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            app_id = metadata.get("app_id")
            if app_id is not None and public_text(app_id, 255, allow_empty=False) != app_id:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            state = await self._client.threads.get_state(thread_id)
            if type(state) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            raw_values = state.get("values")
            if raw_values is None:
                raw_values = {}
            if type(raw_values) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            values = public_pipeline_state(raw_values)

            raw_results = raw_values.get("phase_results")
            if raw_results is None:
                raw_results = {}
            if type(raw_results) is not dict or len(raw_results) > len(PHASE_ORDER):
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            raw_entry = raw_results.get(phase.value)
            if raw_entry is None:
                raw_entry = {}
            if type(raw_entry) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            status = raw_entry.get("status")
            if status is not None and (
                type(status) is not str or status not in {item.value for item in PhaseStatus}
            ):
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            projected_results = values.get("phase_results")
            entry = projected_results.get(phase.value) if type(projected_results) is dict else None
            if entry is None:
                entry = {}
            if type(entry) is not dict or (status is not None and entry.get("status") != status):
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")

            raw_reviews = raw_values.get("prompt_reviews")
            if raw_reviews is None:
                raw_reviews = {}
            if type(raw_reviews) is not dict or len(raw_reviews) > len(PHASE_ORDER):
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            raw_current = raw_reviews.get(phase.value)
            projected_reviews = values.get("prompt_reviews")
            current = (
                projected_reviews.get(phase.value) if type(projected_reviews) is dict else None
            )
            if raw_current is not None and (
                type(raw_current) is not dict or type(current) is not dict or current != raw_current
            ):
                raise PromptReviewConflictError("pipeline prompt-review draft is invalid")

            raw_application_reviews = raw_values.get("application_reviews")
            if raw_application_reviews is None:
                raw_application_reviews = {}
            if type(raw_application_reviews) is not dict or len(raw_application_reviews) > 32:
                raise PromptReviewConflictError("pipeline prompt-review state is invalid")
            raw_application_review = (
                raw_application_reviews.get(app_id) if type(app_id) is str else None
            )
            projected_application_reviews = values.get("application_reviews")
            projected_application_review = (
                projected_application_reviews.get(app_id)
                if type(projected_application_reviews) is dict and type(app_id) is str
                else None
            )
            if raw_application_review is not None and (
                type(raw_application_review) is not dict
                or type(projected_application_review) is not dict
                or projected_application_review != raw_application_review
            ):
                raise PromptReviewConflictError(
                    "pipeline application prompt-review draft is invalid"
                )

            checkpoint = state.get("checkpoint")
            if type(checkpoint) is not dict:
                raise PromptReviewConflictError("pipeline prompt-review checkpoint is invalid")
            try:
                validate_json_object(
                    checkpoint,
                    label="pipeline prompt-review checkpoint",
                    max_bytes=100_000,
                    max_nodes=2_000,
                    max_depth=16,
                )
                validated_native_identifier(
                    checkpoint.get("checkpoint_id"),
                    label="pipeline prompt-review checkpoint",
                )
            except (OverflowError, RecursionError, RuntimeError, TypeError, ValueError):
                raise PromptReviewConflictError(
                    "pipeline prompt-review checkpoint is invalid"
                ) from None
            matching_gate = next(
                (
                    gate
                    for gate in pending_gates_from_state(state)
                    if gate.get("kind") == "prompt_review" and gate.get("phase") == phase.value
                ),
                None,
            )
            if status in TERMINAL_PHASE_STATUSES:
                raise PromptReviewConflictError(
                    f"phase {phase.value!r} is terminal; its executed prompt is immutable"
                )
            if status == PhaseStatus.AWAITING_PROMPT_REVIEW.value:
                if matching_gate is None:
                    raise PromptReviewConflictError(
                        f"phase {phase.value!r} no longer has a pending prompt-review gate"
                    )
            elif status not in {None, PhaseStatus.PENDING.value}:
                raise PromptReviewConflictError(
                    f"phase {phase.value!r} has already started; its prompt can no longer be edited"
                )
            # A resume call returns as soon as LangGraph accepts the new run. The
            # checkpoint can therefore still expose the old prompt-review gate
            # while that run is pending. Reject edits whenever any run can advance
            # the checkpoint, including the AWAITING_PROMPT_REVIEW branch above.
            if await self._has_active_run(thread_id):
                raise PromptReviewConflictError(
                    f"phase {phase.value!r} may advance while the prompt is being edited"
                )

            current_source = current.get("source") if type(current) is dict else None
            now = utcnow_iso()
            source = {
                "origin": "run_override",
                "ref": current_source.get("ref") if type(current_source) is dict else None,
                "editor": actor,
            }

            update_values: JsonDict = {}
            effective_application = body_application
            if app_id:
                existing = _application_override_content(values, app_id)
                prior = (
                    existing
                    if existing is not None
                    else (current.get("application") if type(current) is dict else None)
                )
                # The application prompt is app-wide and run-scoped. A non-null edit updates
                # the single override; a null is treated as "no change" (run-scoped prompts
                # are not reverted to the catalog — match the system/phase-prompt behavior).
                if body_application is not None and body_application != prior:
                    update_values["application_reviews"] = {
                        app_id: {
                            "content": body_application,
                            "source": source,
                            "updated_at": now,
                            "updated_by": actor,
                        }
                    }
                    effective_application = body_application
                else:
                    effective_application = prior

            draft: JsonDict = {
                "system": system,
                "phase_prompt": phase_prompt,
                "application": effective_application,
                "additional_context": additional_context,
                "source": source,
                "updated_at": now,
                "updated_by": actor,
            }
            update_values["prompt_reviews"] = {phase.value: draft}
            # Pin the update to the exact checkpoint observed above. The facade lock
            # serializes prompt edits across replicas; sharing it with gate resume
            # prevents a stale edit from racing the decision that advances the graph.
            update_kwargs: JsonDict = {
                "as_node": "plan_resolver",
                "checkpoint": dict(checkpoint),
            }
            await self._client.threads.update_state(thread_id, update_values, **update_kwargs)
            return draft

    async def _has_active_run(self, thread_id: str) -> bool:
        for status in ("running", "pending"):
            if validated_native_mapping_page(
                await self._client.runs.list(
                    thread_id,
                    status=status,
                    limit=1,
                    select=["run_id", "status"],
                ),
                requested_limit=1,
                label="pipeline active-run probe",
            ):
                return True
        return False

    async def rerun_pipeline(
        self,
        thread_id: str,
        *,
        phases: list[str],
        gates_mode: str,
        idempotency_key: str,
        principal_id: str,
    ) -> str:
        """Start a fresh plan from the complete trusted checkpointed configuration.

        Public checkpoint projections are deliberately not round-trippable. This
        facade re-reads the scoped raw state, proves its ownership still matches
        immutable thread metadata, then changes only phase/gate selection. Hashed
        run metadata makes an ambiguous HTTP retry adopt the committed run without
        persisting the caller's key or principal.
        """

        known_phases = {phase.value for phase in PHASE_ORDER}
        if (
            not phases
            or len(set(phases)) != len(phases)
            or any(phase not in known_phases for phase in phases)
        ):
            raise ValueError("rerun phases must be a non-empty unique known phase list")
        if gates_mode not in {"inherit", "gated", "auto"}:
            raise ValueError("invalid rerun gates mode")

        claim = hashlib.sha256(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "principal_id": principal_id,
                    "idempotency_key": idempotency_key,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._launch_locks.hold(f"rerun:{thread_id}:{claim}"):
            thread, state = await _thread_and_state_definitively(self._client, thread_id)
            run_config = _run_config_from_state(state)
            validated = _validated_durable_replay_config(run_config)
            _ensure_durable_config_ownership(validated, thread)

            run_config = validated.snapshot()
            run_config["phases"] = list(phases)
            run_config["start_phase"] = None
            run_config["stop_after"] = None
            if gates_mode != "inherit":
                run_config["gates"] = {
                    phase.value: {
                        "prompt_review": gates_mode,
                        "output_review": gates_mode,
                    }
                    for phase in PHASE_ORDER
                }
            rerun_config = PipelineConfigurable.model_validate(run_config)
            run_config = rerun_config.snapshot()
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "claim": claim,
                        "configurable": run_config,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()

            existing = await self._find_rerun_run(
                thread_id,
                claim=claim,
                fingerprint=fingerprint,
            )
            if existing is not None:
                return validated_native_identifier(
                    existing.get("run_id"),
                    label="pipeline rerun reconciliation",
                )
            _ensure_rerun_checkpoint_is_terminal(state)
            run: JsonDict | None = None
            conflict = False
            try:
                run = await self._client.runs.create(
                    thread_id,
                    rerun_config.assistant_id,
                    input={},
                    config={
                        "configurable": run_config,
                        "recursion_limit": recommended_recursion_limit(rerun_config.limits),
                    },
                    metadata={
                        RERUN_CLAIM_METADATA_KEY: claim,
                        RERUN_FINGERPRINT_METADATA_KEY: fingerprint,
                    },
                    stream_mode="custom",
                    stream_subgraphs=True,
                    stream_resumable=True,
                    durability="sync",
                    multitask_strategy="reject",
                )
            except ConflictError:
                conflict = True
            if conflict:
                existing = await self._find_rerun_run(
                    thread_id,
                    claim=claim,
                    fingerprint=fingerprint,
                )
                if existing is not None:
                    return validated_native_identifier(
                        existing.get("run_id"),
                        label="pipeline rerun reconciliation",
                    )
                raise RerunActiveRunConflictError(
                    "pipeline rerun was rejected because another run is active"
                )
            if run is None:  # pragma: no cover - native client contract invariant
                raise RuntimeError("pipeline rerun creation returned no run")
            return _validated_run_create_id(run, label="pipeline rerun creation")

    async def _find_rerun_run(
        self,
        thread_id: str,
        *,
        claim: str,
        fingerprint: str,
    ) -> JsonDict | None:
        offset = 0
        while offset < _MAX_RERUN_RECONCILE_RECORDS:
            limit = min(_RUN_LIST_PAGE_SIZE, _MAX_RERUN_RECONCILE_RECORDS - offset)
            page = validated_native_mapping_page(
                await self._client.runs.list(
                    thread_id,
                    limit=limit,
                    offset=offset,
                    select=["run_id", "metadata", "created_at"],
                ),
                requested_limit=limit,
                label="pipeline rerun search",
            )
            for candidate in page:
                metadata = candidate.get("metadata")
                if (
                    not isinstance(metadata, dict)
                    or metadata.get(RERUN_CLAIM_METADATA_KEY) != claim
                ):
                    continue
                if metadata.get(RERUN_FINGERPRINT_METADATA_KEY) != fingerprint:
                    raise RerunIdempotencyConflictError(
                        "rerun idempotency claim was reused for different overrides"
                    )
                return candidate
            offset += len(page)
            if len(page) < limit:
                return None
        raise RerunIdempotencyConflictError(
            "rerun history exceeds the bounded reconciliation limit"
        )

    async def resume_gate(
        self, thread_id: str, interrupt_id: str, action: str, extras: JsonDict
    ) -> str:
        """Compare-and-set gate resume (plan: resume conflict semantics).

        1. Re-read state; the targeted interrupt must still be pending, else
           GateSupersededError (carrying the currently-pending gate, if any).
        2. Revalidate the exact pending gate through the public gate schema and
           require the action to be in its non-empty projected actions list.
        3. Resume run uses multitask_strategy="reject"; a server 409 (lost race)
           maps to GateSupersededError too.
        """
        resume: JsonDict = {"action": action}
        resume.update({key: value for key, value in extras.items() if value is not None})
        validate_gate_payload(resume)
        if contains_credential_material(resume):
            raise ValueError("gate resume must not contain credential material")
        async with self._launch_locks.hold(f"prompt-review:{thread_id}"):
            thread, state = await _thread_and_state_definitively(self._client, thread_id)
            if type(state) is not dict or type(thread) is not dict:
                raise RerunConfigurationConflictError("durable pipeline replay response is invalid")
            gates = pending_gates_from_state(state)
            match = next((g for g in gates if g["interrupt_id"] == interrupt_id), None)
            if match is None:
                raise GateSupersededError(thread_id, interrupt_id, gates[0] if gates else None)

            projected_match = public_gate(match, include_payload=True)
            if projected_match is None:
                raise RerunConfigurationConflictError(
                    "pending pipeline gate does not match the public resume contract"
                )
            projected_payload = projected_match.get("payload")
            if type(projected_payload) is not dict:
                raise RerunConfigurationConflictError("pending pipeline gate payload is invalid")
            allowed = projected_payload.get("actions")
            if type(allowed) is not list or not allowed or action not in allowed:
                safe_allowed = (
                    [candidate for candidate in allowed if type(candidate) is str]
                    if type(allowed) is list
                    else []
                )
                raise InvalidGateActionError(action, safe_allowed)
            if (
                projected_match.get("kind") not in {"prompt_review", "phase_review"}
                and projected_payload.get("thread_id") != thread_id
            ):
                raise RerunConfigurationConflictError(
                    "pending engine recovery gate is bound to another thread"
                )

            run_config = _run_config_from_state(state)
            validated_config = _validated_durable_replay_config(run_config)
            _ensure_durable_config_ownership(validated_config, thread)
            assistant_id = validated_config.assistant_id
            conflict = False
            try:
                run = await self._client.runs.create(
                    thread_id,
                    assistant_id,
                    config={
                        "configurable": run_config,
                        "recursion_limit": recommended_recursion_limit(validated_config.limits),
                    },
                    # Bind the decision to the exact interrupt observed above. A scalar
                    # resume is consumed by whichever interrupt is current when the run
                    # starts, allowing a stale request to decide a newly-opened gate.
                    command={"resume": {interrupt_id: resume}},
                    durability="sync",
                    multitask_strategy="reject",
                )
            except ConflictError:
                conflict = True
                run = None
            if conflict:
                raise GateSupersededError(thread_id, interrupt_id, None)
            if run is None:  # pragma: no cover - client contract invariant
                raise RuntimeError("pipeline gate resume returned no run")
            return _validated_run_create_id(run, label="pipeline gate resume")

    async def abort_pipeline(self, thread_id: str) -> list[str]:
        """Cancel every pending/running run on the thread (engine-level abort is M3)."""
        cancelled: list[str] = []
        saw_active = False
        for _round in range(_ABORT_CANCEL_ROUNDS):
            active_ids = await self._stable_active_run_ids(thread_id)
            if not active_ids:
                if not saw_active:
                    raise NoActiveRunError(thread_id)
                return cancelled
            saw_active = True
            for run_id in active_ids:
                try:
                    await self._client.runs.cancel(thread_id, run_id)
                except NotFoundError:
                    # A run can finish between the stable list and cancellation;
                    # continue so later captured IDs are never skipped.
                    continue
                if run_id not in cancelled:
                    cancelled.append(run_id)
            await asyncio.sleep(0)
        if await self._stable_active_run_ids(thread_id):
            raise ActiveRunSnapshotUnstableError(thread_id)
        return cancelled

    async def _stable_active_run_ids(self, thread_id: str) -> tuple[str, ...]:
        previous: frozenset[str] | None = None
        latest: tuple[str, ...] = ()
        for _ in range(_ACTIVE_RUN_STABILITY_ATTEMPTS):
            latest = await self._active_run_ids_once(thread_id)
            current = frozenset(latest)
            if previous is not None and current == previous:
                return latest
            previous = current
            await asyncio.sleep(0)
        raise ActiveRunSnapshotUnstableError(thread_id)

    async def _active_run_ids_once(self, thread_id: str) -> tuple[str, ...]:
        active_ids: list[str] = []
        for status in ("running", "pending"):
            offset = 0
            while True:
                remaining = _MAX_ABORT_ACTIVE_RUNS + 1 - len(active_ids)
                if remaining <= 0:
                    raise TooManyActiveRunsError(thread_id)
                page_limit = min(_RUN_LIST_PAGE_SIZE, remaining)
                page = validated_native_mapping_page(
                    await self._client.runs.list(
                        thread_id,
                        status=status,
                        limit=page_limit,
                        offset=offset,
                        select=["run_id", "status"],
                    ),
                    requested_limit=page_limit,
                    label="pipeline active-run search",
                )
                if not page:
                    break
                for run in page:
                    run_id = validated_native_identifier(
                        run.get("run_id") if isinstance(run, dict) else None,
                        label="pipeline active-run search",
                    )
                    if run_id not in active_ids:
                        active_ids.append(run_id)
                        if len(active_ids) > _MAX_ABORT_ACTIVE_RUNS:
                            raise TooManyActiveRunsError(thread_id)
                offset += len(page)
                if offset > _MAX_ABORT_ACTIVE_RUNS:
                    raise TooManyActiveRunsError(thread_id)
        return tuple(active_ids)


def _run_config_from_state(state: JsonDict) -> JsonDict:
    """Return the validated durable configurable for a gate-resume run.

    Gate resumes may continue directly into model/provider work, so there is no
    safe default for a checkpoint created before the complete run contract was
    persisted. Such threads require an explicit migration/recovery path instead
    of silently inheriting today's default tenant, provider, and connection.
    """
    if type(state) is not dict:
        raise RerunConfigurationConflictError("durable pipeline state is invalid")
    values = state.get("values")
    if type(values) is not dict:
        raise RerunConfigurationConflictError("durable pipeline configuration snapshot is missing")
    snapshot = values.get("run_config")
    if type(snapshot) is not dict:
        raise RerunConfigurationConflictError(
            "durable pipeline configuration snapshot is missing or invalid"
        )
    return _validated_durable_replay_config(snapshot).snapshot()


def _ensure_rerun_checkpoint_is_terminal(state: JsonDict) -> None:
    """Reject an explicit rerun while any prior phase attempt is unfinished.

    Gate resume/recovery is the only safe way to continue a non-terminal attempt.
    Re-entering ``plan_resolver`` from an output-review checkpoint would otherwise
    preserve that attempt number while clearing its settlement fields, allowing the
    execution engine to see an already-terminal provider idempotency key again.

    This check intentionally runs *after* same-claim reconciliation so an ambiguous
    HTTP retry can still adopt the rerun that was already committed.
    """

    if type(state) is not dict:
        raise RerunConfigurationConflictError("durable pipeline state is invalid")
    values = state.get("values")
    if type(values) is not dict:
        raise RerunConfigurationConflictError("durable pipeline state is invalid")
    raw_results = values.get("phase_results")
    if raw_results is None:
        return
    if type(raw_results) is not dict:
        raise RerunConfigurationConflictError("durable pipeline phase results are invalid")

    known_phases = {phase.value for phase in PHASE_ORDER}
    known_statuses = {status.value for status in PhaseStatus}
    terminal_statuses = {status.value for status in TERMINAL_PHASE_STATUSES}
    for phase_name, entry in raw_results.items():
        if type(phase_name) is not str or phase_name not in known_phases or type(entry) is not dict:
            raise RerunConfigurationConflictError("durable pipeline phase results are invalid")
        if not entry:
            continue
        attempt = entry.get("attempt")
        status = entry.get("status")
        if (
            type(attempt) is not int
            or not 1 <= attempt <= 1_000_000
            or type(status) is not str
            or status not in known_statuses
        ):
            raise RerunConfigurationConflictError("durable pipeline phase results are invalid")
        if status not in terminal_statuses:
            raise RerunConfigurationConflictError(
                "durable pipeline has an unfinished phase attempt; resume or abort it before rerun"
            )


def _ensure_durable_config_ownership(
    validated: PipelineConfigurable,
    thread: JsonDict,
) -> None:
    """Bind one durable replay snapshot to immutable thread ownership metadata."""

    if type(thread) is not dict:
        raise RerunConfigurationConflictError("pipeline thread metadata is invalid")
    metadata = thread.get("metadata")
    if type(metadata) is not dict:
        raise RerunConfigurationConflictError("pipeline thread metadata is invalid")
    metadata_project = metadata.get("project_id")
    metadata_app = metadata.get("app_id")
    if (
        (metadata_project is not None and type(metadata_project) is not str)
        or (metadata_app is not None and type(metadata_app) is not str)
        or validated.project_id != metadata_project
        or validated.app_id != metadata_app
        or (validated.app_id is not None and validated.project_id is None)
        or (
            validated.environment_id is not None
            and (
                validated.project_id is None
                or validated.app_id is None
                or validated.environment_target is None
            )
        )
    ):
        raise RerunConfigurationConflictError(
            "durable pipeline configuration ownership does not match the thread"
        )


def _validated_durable_replay_config(snapshot: JsonDict) -> PipelineConfigurable:
    """Validate state before a trusted facade call may replay it.

    Direct public run creation now rejects provider workload selectors, but
    checkpoints created before that boundary was added can still contain them.
    A trusted rerun/resume must not turn such legacy caller-controlled state
    back into an executable provider request. Graph execution retains its
    compatibility handling for genuinely trusted runs already in progress;
    only a new facade-owned replay fails closed here.
    """

    if type(snapshot) is not dict:
        raise RerunConfigurationConflictError("durable pipeline configuration is invalid")
    invalid = False
    try:
        validate_json_object(
            snapshot,
            label="durable pipeline configuration",
            max_bytes=5_000_000,
            max_nodes=20_000,
        )
    except ValueError:
        invalid = True
    if invalid:
        raise RerunConfigurationConflictError("durable pipeline configuration is invalid")
    if set(snapshot) != set(PipelineConfigurable.model_fields):
        raise RerunConfigurationConflictError(
            "durable pipeline configuration snapshot is incomplete"
        )
    if contains_credential_material(snapshot):
        raise RerunConfigurationConflictError(
            "durable pipeline configuration contains credential material"
        )
    validated: PipelineConfigurable | None = None
    try:
        validated = PipelineConfigurable.model_validate(snapshot)
    except (TypeError, ValueError):
        pass
    if validated is None:
        raise RerunConfigurationConflictError("durable pipeline configuration is invalid")
    if json.dumps(
        validated.snapshot(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) != json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ):
        raise RerunConfigurationConflictError("durable pipeline configuration is not canonical")
    forbidden = _UNSAFE_DURABLE_REPLAY_LOAD_TEST_FIELDS.intersection(validated.load_test)
    if forbidden:
        raise RerunConfigurationConflictError(
            "durable pipeline configuration contains untrusted provider selectors"
        )
    return validated
