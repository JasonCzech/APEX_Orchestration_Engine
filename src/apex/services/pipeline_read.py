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

from apex.domain.pipeline import PHASE_ORDER

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


# ── Service ──────────────────────────────────────────────────────────────────


class PipelineReadService:
    """Facade over the loopback client; constructed per-request with the caller's key."""

    def __init__(self, client: LangGraphClientLike) -> None:
        self._client = client

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
