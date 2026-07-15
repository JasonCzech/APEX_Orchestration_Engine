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

from apex.domain.diagnostics import contains_credential_material
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
    Limits,
    PipelineConfigurable,
)
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.services.langgraph_client import (
    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY,
    RERUN_CLAIM_METADATA_KEY,
    RERUN_FINGERPRINT_METADATA_KEY,
)
from apex.services.launch_locks import LaunchLockManager
from apex.services.pipeline_public import (
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
from apex.services.public_projection import public_engine_handle_summary
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


class PromptReviewConflictError(Exception):
    """A prompt draft can no longer be edited at the observed checkpoint."""


class RerunConfigurationConflictError(Exception):
    """The durable rerun snapshot is missing, malformed, or ownership-drifted."""


class RerunIdempotencyConflictError(Exception):
    """A rerun idempotency claim was reused for different overrides."""


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


def engine_info_from_values(values: JsonDict | None) -> JsonDict | None:
    """Tiny engine summary from state ``engine_handle`` (None when absent/malformed)."""
    return public_engine_handle_summary((values or {}).get("engine_handle"))


def _gate_info(interrupt: Any) -> JsonDict:
    if not isinstance(interrupt, dict):
        return {}
    value = interrupt.get("value")
    payload: JsonDict = value if isinstance(value, dict) else {}
    return {
        "interrupt_id": interrupt.get("id"),
        "kind": payload.get("kind"),
        "phase": payload.get("phase"),
        "payload": payload,
    }


def pending_gates_from_thread(thread: JsonDict) -> list[JsonDict]:
    """Gate infos from a Thread's ``interrupts`` mapping (task_id -> interrupts)."""
    gates: list[JsonDict] = []
    mapping = thread.get("interrupts")
    if not isinstance(mapping, dict) or len(mapping) > 100:
        return gates
    for interrupts in mapping.values():
        if not isinstance(interrupts, list) or len(interrupts) > 100:
            continue
        gates.extend(info for interrupt in interrupts if (info := _gate_info(interrupt)))
    return gates


def pending_gates_from_state(state: JsonDict) -> list[JsonDict]:
    """Gate infos from a ThreadState: tasks[].interrupts, else top-level interrupts."""
    gates: list[JsonDict] = []
    tasks = state.get("tasks")
    if isinstance(tasks, list) and len(tasks) <= 100:
        for task in tasks:
            if not isinstance(task, dict):
                continue
            interrupts = task.get("interrupts")
            if not isinstance(interrupts, list) or len(interrupts) > 100:
                continue
            gates.extend(info for interrupt in interrupts if (info := _gate_info(interrupt)))
    if not gates:
        interrupts = state.get("interrupts")
        if isinstance(interrupts, list) and len(interrupts) <= 100:
            gates.extend(info for interrupt in interrupts if (info := _gate_info(interrupt)))
    return gates


def _public_gate(gate: JsonDict | None) -> JsonDict | None:
    return public_gate(gate, include_payload=False)


def phase_by_name(name: str) -> Phase:
    for phase in PHASE_ORDER:
        if phase.value == name:
            return phase
    raise ValueError(f"unknown phase {name!r}")


def _application_override_content(values: JsonDict, app_id: str | None) -> str | None:
    """Run-scoped, app-wide application prompt override content, if set."""
    if not app_id:
        return None
    override = (values.get("application_reviews") or {}).get(app_id)
    if isinstance(override, dict) and override.get("content") is not None:
        return str(override["content"])
    return None


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
        run_configurable: JsonDict = dict(configurable or {})
        selected_assistant_id = assistant_id or PIPELINE_GRAPH_ID
        run_configurable["assistant_id"] = selected_assistant_id
        configured_phases = run_configurable.get("phases")
        requested_phases = phases
        if requested_phases is None and isinstance(configured_phases, list):
            requested_phases = [str(name) for name in configured_phases]
        if requested_phases:
            known = {phase.value for phase in PHASE_ORDER}
            unknown = sorted(name for name in requested_phases if name not in known)
            if unknown:
                raise ValueError(f"unknown phase(s): {unknown}")

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
            if isinstance(inherited_gates, dict)
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
        if external_results:
            run_input["external_results"] = ExternalResults.model_validate(
                external_results
            ).model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        if context_packets:
            run_input["context_packets"] = validate_context_packets(context_packets)

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
        thread = await self._client.threads.create(
            metadata=metadata,
            **(
                {"thread_id": deterministic_thread_id, "if_exists": "do_nothing"}
                if deterministic_thread_id is not None
                else {}
            ),
        )
        thread_id = thread["thread_id"]

        if idempotency_key:
            existing_metadata = thread.get("metadata") or {}
            existing_fingerprint = existing_metadata.get("launch_idempotency_fingerprint")
            # A deterministic id is guessable by design. Only a thread stamped
            # atomically by this launch path is adoptable; a same-scope native
            # thread with missing metadata must not become someone else's run.
            if existing_fingerprint != request_fingerprint:
                raise LaunchIdempotencyConflictError(
                    "idempotency_key was already used for a different request"
                )
            existing_run = await self._find_launch_root_run(
                thread_id,
                request_fingerprint=request_fingerprint,
            )
            if existing_run is not None:
                existing_run_id = existing_run["run_id"]
                return {
                    "thread_id": thread_id,
                    "run_id": existing_run_id,
                    "stream_url": (
                        f"/threads/{thread_id}/runs/{existing_run_id}/stream?stream_mode=custom"
                    ),
                }

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
        except ConflictError:
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
                        existing_run_id = existing_run["run_id"]
                        return {
                            "thread_id": thread_id,
                            "run_id": existing_run_id,
                            "stream_url": (
                                f"/threads/{thread_id}/runs/{existing_run_id}/stream"
                                "?stream_mode=custom"
                            ),
                        }
            raise
        except APIStatusError as exc:
            definitive_rejection = (
                400 <= exc.status_code < 500
                and exc.status_code not in _AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES
            )
            if definitive_rejection and deterministic_thread_id is None:
                # The server definitively rejected this non-idempotent launch, so
                # its freshly-created random thread cannot contain a committed run.
                # Deterministic threads are durable idempotency claims and remain.
                try:
                    await self._client.threads.delete(thread_id)
                except Exception as cleanup_exc:  # cleanup failure must not mask the launch error
                    logger.warning(
                        "pipeline.rejected_launch_thread_cleanup_failed",
                        thread_id=thread_id,
                        status_code=exc.status_code,
                        error_type=cleanup_exc.__class__.__name__,
                    )
            else:
                # A 5xx can be returned after the server committed the run.
                logger.warning(
                    "pipeline.run_create_ambiguous",
                    thread_id=thread_id,
                    status_code=exc.status_code,
                    error_type=exc.__class__.__name__,
                )
            raise
        except Exception as exc:
            # A transport/server exception does not prove the run was rejected. The
            # server may have committed it before the response was lost; deleting the
            # thread here would erase its checkpoints and the only durable external
            # cleanup handle. Keep both deterministic and randomly-created threads so
            # operators can reconcile an ambiguously accepted launch.
            logger.warning(
                "pipeline.run_create_ambiguous",
                thread_id=thread_id,
                error_type=exc.__class__.__name__,
            )
            raise
        return {
            "thread_id": thread_id,
            "run_id": run["run_id"],
            "stream_url": (f"/threads/{thread_id}/runs/{run['run_id']}/stream?stream_mode=custom"),
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
            page = await self._client.runs.list(
                thread_id,
                limit=limit,
                offset=offset,
                select=["run_id", "status", "metadata", "created_at"],
            )
            for candidate in page:
                candidate_metadata = candidate.get("metadata")
                if isinstance(candidate_metadata, dict) and (
                    LAUNCH_ROOT_FINGERPRINT_METADATA_KEY in candidate_metadata
                ):
                    saw_root_marker = True
                    if (
                        candidate_metadata.get(LAUNCH_ROOT_FINGERPRINT_METADATA_KEY)
                        == request_fingerprint
                    ):
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
            threads = await self._client.threads.search(
                metadata=metadata,
                status=status,
                limit=limit,
                offset=offset,
                sort_by="updated_at",
                sort_order="desc",
                select=list(PIPELINE_SUMMARY_SELECT),
                extract=dict(PIPELINE_SUMMARY_EXTRACT),
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
            threads = await self._client.threads.search(
                metadata=metadata,
                status=status,
                limit=scan_limit,
                offset=scan_offset,
                sort_by="updated_at",
                sort_order="desc",
                select=list(PIPELINE_SUMMARY_SELECT),
                extract=dict(PIPELINE_SUMMARY_EXTRACT),
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
        gates = [
            projected
            for gate in pending_gates_from_state(state)
            if (projected := public_gate(gate, include_payload=True)) is not None
        ]
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
        validate_prompt_parts(
            system=str(body.get("system") or ""),
            user=str(body.get("phase_prompt") or ""),
            application=(str(body["application"]) if body.get("application") is not None else None),
            additional_context=str(body.get("additional_context") or ""),
        )
        if contains_credential_material({"body": body, "actor": actor}):
            raise ValueError("prompt review must not contain credential material")
        phase = phase_by_name(phase_name)
        async with self._launch_locks.hold(f"prompt-review:{thread_id}"):
            thread = await self._client.threads.get(thread_id)
            metadata = thread.get("metadata") or {}
            app_id = metadata.get("app_id")
            state = await self._client.threads.get_state(thread_id)
            values = state.get("values") or {}
            entry = (values.get("phase_results") or {}).get(phase.value) or {}
            status = entry.get("status")
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

            current = (values.get("prompt_reviews") or {}).get(phase.value)
            current_source = current.get("source") if isinstance(current, dict) else None
            now = utcnow_iso()
            source = {
                "origin": "run_override",
                "ref": current_source.get("ref") if isinstance(current_source, dict) else None,
                "editor": actor,
            }

            body_application = body.get("application")
            update_values: JsonDict = {}
            effective_application = body_application
            if app_id:
                existing = _application_override_content(values, app_id)
                prior = (
                    existing
                    if existing is not None
                    else (current.get("application") if isinstance(current, dict) else None)
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
                "system": str(body.get("system") or ""),
                "phase_prompt": str(body.get("phase_prompt") or ""),
                "application": effective_application,
                "additional_context": str(body.get("additional_context") or ""),
                "source": source,
                "updated_at": now,
                "updated_by": actor,
            }
            update_values["prompt_reviews"] = {phase.value: draft}
            # Pin the update to the exact checkpoint observed above. The facade lock
            # serializes prompt edits across replicas; sharing it with gate resume
            # prevents a stale edit from racing the decision that advances the graph.
            update_kwargs: JsonDict = {"as_node": "plan_resolver"}
            checkpoint = state.get("checkpoint")
            if isinstance(checkpoint, dict):
                update_kwargs["checkpoint"] = checkpoint
            await self._client.threads.update_state(thread_id, update_values, **update_kwargs)
            return draft

    async def _has_active_run(self, thread_id: str) -> bool:
        for status in ("running", "pending"):
            if await self._client.runs.list(
                thread_id,
                status=status,
                limit=1,
                select=["run_id", "status"],
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
            thread, state = await asyncio.gather(
                self._client.threads.get(thread_id),
                self._client.threads.get_state(thread_id),
            )
            metadata = thread.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            values = state.get("values")
            values = values if isinstance(values, dict) else {}
            snapshot = values.get("run_config")
            if not isinstance(snapshot, dict):
                raise RerunConfigurationConflictError("durable rerun configuration is missing")
            validated = _validated_durable_replay_config(snapshot)

            metadata_project = metadata.get("project_id")
            metadata_app = metadata.get("app_id")
            if (
                (metadata_project is not None and not isinstance(metadata_project, str))
                or (metadata_app is not None and not isinstance(metadata_app, str))
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
                    "durable rerun configuration ownership does not match the thread"
                )

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
                return str(existing["run_id"])
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
                existing = await self._find_rerun_run(
                    thread_id,
                    claim=claim,
                    fingerprint=fingerprint,
                )
                if existing is not None:
                    return str(existing["run_id"])
                raise
            return str(run["run_id"])

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
            page = await self._client.runs.list(
                thread_id,
                limit=limit,
                offset=offset,
                select=["run_id", "metadata", "created_at"],
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
        2. Action must be in the payload's "actions" list (absent list = permissive,
           for forward-compat with payloads that omit it).
        3. Resume run uses multitask_strategy="reject"; a server 409 (lost race)
           maps to GateSupersededError too.
        """
        resume: JsonDict = {"action": action}
        resume.update({key: value for key, value in extras.items() if value is not None})
        validate_gate_payload(resume)
        if contains_credential_material(resume):
            raise ValueError("gate resume must not contain credential material")
        async with self._launch_locks.hold(f"prompt-review:{thread_id}"):
            state = await self._client.threads.get_state(thread_id)
            gates = pending_gates_from_state(state)
            match = next((g for g in gates if g["interrupt_id"] == interrupt_id), None)
            if match is None:
                raise GateSupersededError(thread_id, interrupt_id, gates[0] if gates else None)

            allowed = match["payload"].get("actions")
            if isinstance(allowed, list) and allowed and action not in allowed:
                raise InvalidGateActionError(action, [str(a) for a in allowed])

            run_config = _run_config_from_state(state)
            assistant_id = _validated_durable_replay_config(run_config).assistant_id
            try:
                limits = _limits_from_state(state)
            except (TypeError, ValueError) as exc:
                raise RerunConfigurationConflictError(
                    "durable pipeline limits are invalid"
                ) from exc
            try:
                run = await self._client.runs.create(
                    thread_id,
                    assistant_id,
                    config={
                        "configurable": run_config,
                        "recursion_limit": recommended_recursion_limit(limits),
                    },
                    # Bind the decision to the exact interrupt observed above. A scalar
                    # resume is consumed by whichever interrupt is current when the run
                    # starts, allowing a stale request to decide a newly-opened gate.
                    command={"resume": {interrupt_id: resume}},
                    durability="sync",
                    multitask_strategy="reject",
                )
            except ConflictError as exc:
                raise GateSupersededError(thread_id, interrupt_id, None) from exc
            return run["run_id"]

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
                page = await self._client.runs.list(
                    thread_id,
                    status=status,
                    limit=page_limit,
                    offset=offset,
                    select=["run_id", "status"],
                )
                if not page:
                    break
                for run in page:
                    run_id = str(run["run_id"])
                    if run_id not in active_ids:
                        active_ids.append(run_id)
                        if len(active_ids) > _MAX_ABORT_ACTIVE_RUNS:
                            raise TooManyActiveRunsError(thread_id)
                offset += len(page)
                if offset > _MAX_ABORT_ACTIVE_RUNS:
                    raise TooManyActiveRunsError(thread_id)
        return tuple(active_ids)


def _limits_from_state(state: JsonDict) -> Limits:
    """Recover the run's Limits to size the resume recursion budget.

    The pipeline's plan_resolver checkpoints the resolved limits into state
    `values["limits"]` (graph.plan_resolver); that is the authoritative location,
    since LangGraph's get_state does not surface the run configurable. Falls back
    to defaults only when no run has seeded the snapshot yet.
    """
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    run_config = (values or {}).get("run_config")
    if isinstance(run_config, dict) and isinstance(run_config.get("limits"), dict):
        return Limits.model_validate(run_config["limits"])
    snapshot = (values or {}).get("limits")
    if isinstance(snapshot, dict):
        return Limits.model_validate(snapshot)
    return Limits()


def _run_config_from_state(state: JsonDict) -> JsonDict:
    """Return the validated durable configurable for a gate-resume run.

    Old threads have only the limits snapshot. They retain their historical
    behavior, while every newly-created run checkpoints the complete contract.
    """
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    snapshot = (values or {}).get("run_config")
    if not isinstance(snapshot, dict):
        return {}
    return _validated_durable_replay_config(snapshot).snapshot()


def _validated_durable_replay_config(snapshot: JsonDict) -> PipelineConfigurable:
    """Validate state before a trusted facade call may replay it.

    Direct public run creation now rejects provider workload selectors, but
    checkpoints created before that boundary was added can still contain them.
    A trusted rerun/resume must not turn such legacy caller-controlled state
    back into an executable provider request. Graph execution retains its
    compatibility handling for genuinely trusted runs already in progress;
    only a new facade-owned replay fails closed here.
    """

    if contains_credential_material(snapshot):
        raise RerunConfigurationConflictError(
            "durable pipeline configuration contains credential material"
        )
    try:
        validated = PipelineConfigurable.model_validate(snapshot)
    except (TypeError, ValueError) as exc:
        raise RerunConfigurationConflictError("durable pipeline configuration is invalid") from exc
    forbidden = _UNSAFE_DURABLE_REPLAY_LOAD_TEST_FIELDS.intersection(validated.load_test)
    if forbidden:
        raise RerunConfigurationConflictError(
            "durable pipeline configuration contains untrusted provider selectors"
        )
    return validated
