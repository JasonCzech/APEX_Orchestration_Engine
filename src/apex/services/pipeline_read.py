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

from typing import Any, Protocol

from langgraph_sdk.errors import ConflictError

from apex.domain.pipeline import PHASE_ORDER, Phase, utcnow_iso
from apex.graphs.pipeline.configurable import Limits, PipelineConfigurable
from apex.graphs.pipeline.execution_phase import recommended_recursion_limit
from apex.services.prompts import (
    prompt_review_from_resolved,
    resolve_phase_prompt_no_catalog,
    resolve_phase_prompt_sync,
)

JsonDict = dict[str, Any]

PIPELINE_GRAPH_ID = "pipeline"


class LangGraphClientLike(Protocol):
    """Structural slice of langgraph_sdk LangGraphClient used by this service."""

    threads: Any
    runs: Any


class GateSupersededError(Exception):
    """The targeted interrupt is no longer pending (resolved or replaced)."""

    def __init__(self, thread_id: str, interrupt_id: str, pending_gate: JsonDict | None) -> None:
        self.thread_id = thread_id
        self.interrupt_id = interrupt_id
        self.pending_gate = pending_gate
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


# ── Pure mapping helpers ─────────────────────────────────────────────────────


def build_phase_strip(values: JsonDict | None) -> list[JsonDict]:
    """Canonical-order strip from state ``phase_results``; absent phases -> "none"."""
    results = (values or {}).get("phase_results") or {}
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
    handle = (values or {}).get("engine_handle")
    if not isinstance(handle, dict) or not handle.get("engine"):
        return None
    return {"engine": handle.get("engine"), "external_run_id": handle.get("external_run_id")}


def _gate_info(interrupt: JsonDict) -> JsonDict:
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
    for interrupts in (thread.get("interrupts") or {}).values():
        for interrupt in interrupts or []:
            gates.append(_gate_info(interrupt))
    return gates


def pending_gates_from_state(state: JsonDict) -> list[JsonDict]:
    """Gate infos from a ThreadState: tasks[].interrupts, else top-level interrupts."""
    gates: list[JsonDict] = []
    for task in state.get("tasks") or []:
        for interrupt in task.get("interrupts") or []:
            gates.append(_gate_info(interrupt))
    if not gates:
        for interrupt in state.get("interrupts") or []:
            gates.append(_gate_info(interrupt))
    return gates


def _public_gate(gate: JsonDict | None) -> JsonDict | None:
    if gate is None:
        return None
    return {k: gate.get(k) for k in ("interrupt_id", "kind", "phase")}


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
    values = thread.get("values") or {}
    metadata = thread.get("metadata") or {}
    gates = pending_gates_from_thread(thread)
    return {
        "thread_id": thread.get("thread_id"),
        "title": values.get("title") or metadata.get("title"),
        "project_id": metadata.get("project_id"),
        "app_id": metadata.get("app_id"),
        "thread_status": thread.get("status"),
        "current_phase": values.get("current_phase"),
        "phase_strip": build_phase_strip(values),
        "engine": engine_info_from_values(values),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "pending_gate": _public_gate(gates[0] if gates else None),
    }


def _auto_gates(phases: list[str] | None) -> JsonDict:
    """Auto (un-gated) policy for the selected phases — the default for headless runs."""
    targets = phases or [phase.value for phase in PHASE_ORDER]
    return {name: {"prompt_review": "auto", "output_review": "auto"} for name in targets}


# ── Service ──────────────────────────────────────────────────────────────────


class PipelineReadService:
    """Facade over the loopback client; constructed per-request with the caller's key."""

    def __init__(self, client: LangGraphClientLike) -> None:
        self._client = client

    async def start_run(
        self,
        *,
        title: str,
        request: str = "",
        project_id: str | None = None,
        app_id: str | None = None,
        phases: list[str] | None = None,
        gates: JsonDict | None = None,
        agent_backend: str | None = None,
        model_by_phase: JsonDict | None = None,
        external_results: JsonDict | None = None,
        context_packets: list[JsonDict] | None = None,
    ) -> JsonDict:
        """Create a thread and start a pipeline run; returns {thread_id, run_id, stream_url}.

        Convenience entrypoint for external clients (e.g. a results-analysis dashboard):
        wraps thread-create + run-start over the loopback API so callers don't drive the
        raw LangGraph surface. Gates default to "auto" for the selected phases so an
        unattended analysis run completes without an operator resuming gates; pass an
        explicit `gates` map for interactive runs. Raises ValueError on unknown phases.
        """
        if phases:
            known = {phase.value for phase in PHASE_ORDER}
            unknown = sorted(name for name in phases if name not in known)
            if unknown:
                raise ValueError(f"unknown phase(s): {unknown}")

        metadata: JsonDict = {"title": title}
        if project_id:
            metadata["project_id"] = project_id
        if app_id:
            metadata["app_id"] = app_id
        thread = await self._client.threads.create(metadata=metadata)
        thread_id = thread["thread_id"]

        configurable: JsonDict = {}
        if project_id:
            configurable["project_id"] = project_id
        if app_id:
            configurable["app_id"] = app_id
        if phases:
            configurable["phases"] = phases
        if agent_backend:
            configurable["agent_backend"] = agent_backend
        if model_by_phase:
            configurable["model_by_phase"] = model_by_phase
        resolved_gates = gates if gates is not None else _auto_gates(phases)
        if resolved_gates:
            configurable["gates"] = resolved_gates

        run_input: JsonDict = {"title": title, "request": request}
        if external_results:
            run_input["external_results"] = external_results
        if context_packets:
            run_input["context_packets"] = context_packets

        run = await self._client.runs.create(
            thread_id,
            PIPELINE_GRAPH_ID,
            input=run_input,
            config={
                "configurable": configurable,
                "recursion_limit": recommended_recursion_limit(Limits()),
            },
        )
        return {
            "thread_id": thread_id,
            "run_id": run["run_id"],
            "stream_url": f"/runs/{run['run_id']}/stream",
        }

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
        threads = await self._client.threads.search(
            metadata={"project_id": project} if project else None,
            status=status,
            limit=limit,
            offset=offset,
            sort_by="updated_at",
            sort_order="desc",
        )
        items = [map_thread_summary(thread) for thread in threads]
        if q:
            needle = q.lower()
            items = [
                item
                for item in items
                if needle in (item["title"] or "").lower()
                or needle in (item["thread_id"] or "").lower()
            ]
        return items

    async def get_pipeline(self, thread_id: str) -> JsonDict:
        """Thread + full state values + pending interrupts (gate infos with payloads)."""
        thread = await self._client.threads.get(thread_id)
        state = await self._client.threads.get_state(thread_id)
        gates = pending_gates_from_state(state)
        summary = map_thread_summary(thread)
        summary["pending_gate"] = _public_gate(gates[0] if gates else None)
        return {
            **summary,
            "values": state.get("values") or {},
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
        values = state.get("values") or {}
        metadata = thread.get("metadata") or {}
        app_id = metadata.get("app_id")

        review = (values.get("prompt_reviews") or {}).get(phase.value)
        if isinstance(review, dict):
            return _with_application_override(dict(review), values, app_id)

        entry = (values.get("phase_results") or {}).get(phase.value) or {}
        prompt = entry.get("resolved_prompt")
        if isinstance(prompt, dict) and (prompt.get("system") or prompt.get("user")):
            source = entry.get("resolved_prompt_source")
            return _with_application_override(
                {
                    "system": prompt.get("system") or "",
                    "phase_prompt": prompt.get("user") or "",
                    "application": prompt.get("application"),
                    "additional_context": "",
                    "source": dict(source) if isinstance(source, dict) else {"origin": "catalog"},
                    "updated_at": utcnow_iso(),
                    "updated_by": "system",
                },
                values,
                app_id,
            )

        cfg = PipelineConfigurable(
            project_id=metadata.get("project_id"),
            app_id=app_id,
        )
        variables = {
            "title": values.get("title") or metadata.get("title") or "untitled run",
            "request": values.get("request") or "(no request provided)",
        }
        try:
            resolved = resolve_phase_prompt_sync(phase, cfg, variables=variables)
        except Exception:
            resolved = resolve_phase_prompt_no_catalog(phase, cfg, variables=variables)
        return _with_application_override(
            dict(prompt_review_from_resolved(resolved)), values, app_id
        )

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
        phase = phase_by_name(phase_name)
        thread = await self._client.threads.get(thread_id)
        metadata = thread.get("metadata") or {}
        app_id = metadata.get("app_id")
        state = await self._client.threads.get_state(thread_id)
        values = state.get("values") or {}
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
            # Either way effective_application reflects the value the next GET will return,
            # so the response never disagrees with the persisted state.
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
        await self._client.threads.update_state(thread_id, update_values)
        return draft

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
        state = await self._client.threads.get_state(thread_id)
        gates = pending_gates_from_state(state)
        match = next((g for g in gates if g["interrupt_id"] == interrupt_id), None)
        if match is None:
            raise GateSupersededError(thread_id, interrupt_id, gates[0] if gates else None)

        allowed = match["payload"].get("actions")
        if isinstance(allowed, list) and allowed and action not in allowed:
            raise InvalidGateActionError(action, [str(a) for a in allowed])

        resume: JsonDict = {"action": action}
        resume.update({k: v for k, v in extras.items() if v is not None})
        try:
            run = await self._client.runs.create(
                thread_id,
                PIPELINE_GRAPH_ID,
                config={"recursion_limit": recommended_recursion_limit(_limits_from_state(state))},
                command={"resume": resume},
                multitask_strategy="reject",
            )
        except ConflictError as exc:
            raise GateSupersededError(thread_id, interrupt_id, None) from exc
        return run["run_id"]

    async def abort_pipeline(self, thread_id: str) -> list[str]:
        """Cancel every pending/running run on the thread (engine-level abort is M3)."""
        cancelled: list[str] = []
        for status in ("running", "pending"):
            for run in await self._client.runs.list(thread_id, status=status):
                await self._client.runs.cancel(thread_id, run["run_id"])
                cancelled.append(run["run_id"])
        if not cancelled:
            raise NoActiveRunError(thread_id)
        return cancelled


def _limits_from_state(state: JsonDict) -> Limits:
    """Recover the run's Limits to size the resume recursion budget.

    The pipeline's plan_resolver checkpoints the resolved limits into state
    `values["limits"]` (graph.plan_resolver); that is the authoritative location,
    since LangGraph's get_state does not surface the run configurable. Falls back
    to defaults only when no run has seeded the snapshot yet.
    """
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    snapshot = (values or {}).get("limits")
    if isinstance(snapshot, dict):
        return Limits.model_validate(snapshot)
    return Limits()
