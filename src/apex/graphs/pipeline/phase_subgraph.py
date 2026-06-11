"""Per-phase subgraph factory: prepare -> prompt_gate -> agent -> output_gate -> finalize.

Each interrupt lives in its own node (resume re-executes the interrupting node from the
top, so gate nodes are side-effect-free before interrupt()). The awaiting_* status and
gate_opened event are committed by a separate "open_*" node so they are durable in the
checkpoint before the graph pauses. Agent bodies are deterministic M1 stubs (no LLM, no
ports); state writes use deterministic ids so node re-execution stays idempotent under
the append-unique reducers.
"""

from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from apex.domain.pipeline import (
    TERMINAL_PHASE_STATUSES,
    ArtifactRef,
    DialogueEntry,
    Phase,
    PhaseStatus,
    ToolCallRecord,
    utcnow_iso,
)
from apex.graphs.pipeline.configurable import GateMode, PipelineConfigurable
from apex.graphs.pipeline.gates import (
    PHASE_REVIEW_ACTIONS,
    PROMPT_REVIEW_ACTIONS,
    build_phase_review_payload,
    build_prompt_review_payload,
    make_approval,
    parse_gate_decision,
    resolve_actor,
)
from apex.graphs.pipeline.state import JsonDict, PipelineState
from apex.services.prompts import resolve_phase_prompt_sync

EVENT_SCHEMA_VERSION = 1

# Bounds re-interrupt loops inside a single gate node (modify re-reviews, bad actions).
MAX_GATE_LOOPS = 10


def emit_event(event: JsonDict) -> None:
    """Send a custom stream event; no-op outside a runnable context (plain unit calls)."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)


def stub_tool_names(phase: Phase) -> list[str]:
    return [f"{phase.value}.stub_lookup"]


def _prompt_variables(state: PipelineState) -> dict[str, str]:
    return {
        "title": state.get("title") or "untitled run",
        "request": state.get("request") or "(no request provided)",
    }


def _entry(state: PipelineState, phase: Phase) -> JsonDict:
    return (state.get("phase_results") or {}).get(phase.value) or {}


def _attempt(entry: JsonDict) -> int:
    return int(entry.get("attempt") or 1)


def _phase_update(phase: Phase, attempt: int, **fields: Any) -> JsonDict:
    # The merge reducer replaces wholesale when attempt differs, so every partial
    # update must carry the current attempt to merge instead of clobber.
    return {"phase_results": {phase.value: {"attempt": attempt, **fields}}}


def _make_prepare(phase: Phase):
    def prepare(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
        attempt = _attempt(_entry(state, phase))
        # Resolution order: run override -> catalog active version -> builtin
        # defaults (catalog falls through silently when Postgres is absent).
        resolved = resolve_phase_prompt_sync(phase, cfg, variables=_prompt_variables(state))
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "phase_status",
                "phase": phase.value,
                "status": PhaseStatus.RUNNING.value,
                "attempt": attempt,
            }
        )
        update = _phase_update(
            phase,
            attempt,
            status=PhaseStatus.RUNNING.value,
            started_at=utcnow_iso(),
            resolved_prompt={"system": resolved["system"], "user": resolved["user"]},
            resolved_prompt_source=dict(resolved["source"]),
        )
        update["current_phase"] = phase.value
        return update

    return prepare


def _make_open_prompt_gate(phase: Phase):
    def open_prompt_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).prompt_review is not GateMode.GATED:
            return {}
        attempt = _attempt(_entry(state, phase))
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "gate_opened",
                "gate": "prompt_review",
                "phase": phase.value,
                "attempt": attempt,
            }
        )
        return _phase_update(phase, attempt, status=PhaseStatus.AWAITING_PROMPT_REVIEW.value)

    return open_prompt_gate


def _make_prompt_gate(phase: Phase):
    def prompt_gate(state: PipelineState, config: RunnableConfig) -> Command[str]:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).prompt_review is not GateMode.GATED:
            return Command(goto="agent")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        prompt = dict(entry.get("resolved_prompt") or {})
        source = dict(entry.get("resolved_prompt_source") or {})
        packets = list(state.get("context_packets") or [])
        tools = stub_tool_names(phase)
        error: str | None = None
        for _ in range(MAX_GATE_LOOPS):
            payload = build_prompt_review_payload(phase, prompt, source, packets, tools, error)
            decision = parse_gate_decision(interrupt(payload), PROMPT_REVIEW_ACTIONS)
            action = decision["action"]
            error = None
            if action == "approve":
                update = _phase_update(
                    phase,
                    attempt,
                    resolved_prompt=prompt,
                    resolved_prompt_source=source,
                    approvals=[make_approval("prompt_review", "approve", config)],
                )
                return Command(goto="agent", update=update)
            if action == "modify":
                edit = decision.get("prompt")
                if isinstance(edit, dict):
                    prompt = {
                        "system": edit.get("system", prompt.get("system")),
                        "user": edit.get("user", prompt.get("user")),
                    }
                source = {
                    "origin": "gate_edit",
                    "ref": source.get("ref"),
                    "editor": resolve_actor(config),
                }
                continue  # re-interrupt for re-review of the edited prompt
            if action == "skip_phase":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.SKIPPED.value,
                    approvals=[make_approval("prompt_review", "skip_phase", config)],
                )
                return Command(goto="finalize", update=update)
            if action == "abort":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.ABORTED.value,
                    approvals=[make_approval("prompt_review", "abort", config)],
                )
                update["run_aborted"] = True
                return Command(goto="finalize", update=update)
            error = decision.get("error") or "unsupported action"
        warning = f"prompt review loop cap ({MAX_GATE_LOOPS}) reached; proceeding with last prompt"
        update = _phase_update(
            phase,
            attempt,
            resolved_prompt=prompt,
            resolved_prompt_source=source,
            warnings=[warning],
        )
        return Command(goto="agent", update=update)

    return prompt_gate


def _make_agent(phase: Phase):
    def agent(state: PipelineState, config: RunnableConfig) -> JsonDict:
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        revise_count = int(entry.get("revise_count") or 0)
        title = state.get("title") or "untitled run"
        request = state.get("request") or "(no request provided)"
        summary = f"[{phase.value}] stub result for '{title}': {request}"
        instructions = entry.get("revise_instructions")
        if instructions:
            summary += f" (revised per: {instructions})"
        digest = (
            f"Deterministic stub reasoning for {phase.value} "
            f"(attempt {attempt}, revision {revise_count})."
        )
        tool_call = ToolCallRecord(
            id=f"{phase.value}-a{attempt}-r{revise_count}-stub-lookup",
            tool=f"{phase.value}.stub_lookup",
            args_preview={"title": title},
            status="ok",
            duration_ms=0,
        ).model_dump(mode="json")
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "tool_call",
                "phase": phase.value,
                "id": tool_call["id"],
                "tool": tool_call["tool"],
                "status": tool_call["status"],
            }
        )
        return _phase_update(
            phase,
            attempt,
            status=PhaseStatus.RUNNING.value,
            summary=summary,
            reasoning_digest=digest,
            tool_calls=[tool_call],
        )

    return agent


def _make_open_output_gate(phase: Phase):
    def open_output_gate(state: PipelineState, config: RunnableConfig) -> JsonDict:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return {}
        attempt = _attempt(_entry(state, phase))
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "gate_opened",
                "gate": "phase_review",
                "phase": phase.value,
                "attempt": attempt,
            }
        )
        return _phase_update(phase, attempt, status=PhaseStatus.AWAITING_OUTPUT_REVIEW.value)

    return open_output_gate


def _make_output_gate(phase: Phase):
    def output_gate(state: PipelineState, config: RunnableConfig) -> Command[str]:
        cfg = PipelineConfigurable.from_config(config)
        if cfg.gate_policy(phase).output_review is not GateMode.GATED:
            return Command(goto="finalize")
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        revise_count = int(entry.get("revise_count") or 0)
        summary = entry.get("summary")
        warnings = list(entry.get("warnings") or [])
        result_preview = {"summary": summary, "reasoning_digest": entry.get("reasoning_digest")}
        artifact_ids = set(entry.get("artifact_ids") or [])
        artifact_previews = [
            {"id": a.get("id"), "kind": a.get("kind"), "name": a.get("name")}
            for a in state.get("artifacts") or []
            if a.get("id") in artifact_ids
        ]
        phase_dialogue = [d for d in state.get("dialogue") or [] if d.get("phase") == phase.value]
        new_dialogue: list[JsonDict] = []
        max_turns = cfg.limits.max_dialogue_turns
        error: str | None = None
        for _ in range(max_turns + MAX_GATE_LOOPS):
            tail = (phase_dialogue + new_dialogue)[-3:]
            payload = build_phase_review_payload(
                phase, summary, result_preview, artifact_previews, warnings, tail, error
            )
            decision = parse_gate_decision(interrupt(payload), PHASE_REVIEW_ACTIONS)
            action = decision["action"]
            error = None
            extra: JsonDict = {"dialogue": new_dialogue} if new_dialogue else {}
            if action == "approve":
                update = _phase_update(
                    phase,
                    attempt,
                    approvals=[make_approval("phase_review", "approve", config)],
                )
                return Command(goto="finalize", update={**update, **extra})
            if action == "revise":
                instructions = str(decision.get("instructions") or "")
                if revise_count >= cfg.limits.max_revise_loops:
                    warning = (
                        f"max_revise_loops ({cfg.limits.max_revise_loops}) reached; "
                        "proceeding as approve"
                    )
                    update = _phase_update(
                        phase,
                        attempt,
                        warnings=[warning],
                        approvals=[make_approval("phase_review", "approve", config, warning)],
                    )
                    return Command(goto="finalize", update={**update, **extra})
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.RUNNING.value,
                    revise_instructions=instructions,
                    revise_count=revise_count + 1,
                    approvals=[make_approval("phase_review", "revise", config, instructions)],
                )
                return Command(goto="agent", update={**update, **extra})
            if action == "discuss":
                message = str(decision.get("message") or "")
                operator_turns = sum(
                    1 for d in phase_dialogue + new_dialogue if d.get("role") == "operator"
                )
                if operator_turns >= max_turns:
                    error = f"max_dialogue_turns ({max_turns}) reached; choose another action"
                    continue
                index = len(phase_dialogue) + len(new_dialogue)
                new_dialogue = new_dialogue + [
                    DialogueEntry(
                        id=f"{phase.value}-a{attempt}-d{index}-operator",
                        phase=phase,
                        role="operator",
                        content=message,
                    ).model_dump(mode="json"),
                    DialogueEntry(
                        id=f"{phase.value}-a{attempt}-d{index + 1}-agent",
                        phase=phase,
                        role="agent",
                        content=f"[{phase.value} agent stub] acknowledged: {message}",
                    ).model_dump(mode="json"),
                ]
                continue  # re-interrupt with the refreshed dialogue tail
            if action == "abort":
                update = _phase_update(
                    phase,
                    attempt,
                    status=PhaseStatus.ABORTED.value,
                    approvals=[make_approval("phase_review", "abort", config)],
                )
                update["run_aborted"] = True
                return Command(goto="finalize", update={**update, **extra})
            error = decision.get("error") or "unsupported action"
        warning = "phase review loop cap reached; proceeding as approve"
        update = _phase_update(
            phase,
            attempt,
            warnings=[warning],
            approvals=[make_approval("phase_review", "approve", config, warning)],
        )
        extra = {"dialogue": new_dialogue} if new_dialogue else {}
        return Command(goto="finalize", update={**update, **extra})

    return output_gate


def _make_finalize(phase: Phase):
    def finalize(state: PipelineState, config: RunnableConfig) -> JsonDict:
        entry = _entry(state, phase)
        attempt = _attempt(entry)
        status = entry.get("status")
        if status not in TERMINAL_PHASE_STATUSES:
            status = PhaseStatus.SUCCEEDED.value
        ended_at = utcnow_iso()
        duration_s: float | None = None
        started_at = entry.get("started_at")
        if started_at:
            delta = datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
            duration_s = delta.total_seconds()
        transcript = ArtifactRef(
            id=f"{phase.value}-a{attempt}-transcript",
            kind="transcript",
            name=f"{phase.value} transcript (attempt {attempt})",
            uri=f"memory://transcripts/{phase.value}/attempt-{attempt}",
            media_type="text/plain",
            summary=entry.get("reasoning_digest"),
        ).model_dump(mode="json")
        emit_event(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "type": "phase_status",
                "phase": phase.value,
                "status": status,
                "attempt": attempt,
            }
        )
        update = _phase_update(
            phase,
            attempt,
            status=status,
            ended_at=ended_at,
            duration_s=duration_s,
            artifact_ids=[transcript["id"]],
            transcript_ref=transcript,
        )
        update["artifacts"] = [transcript]
        return update

    return finalize


def make_phase_subgraph(phase: Phase) -> CompiledStateGraph[PipelineState, Any, Any, Any]:
    """Build the compiled phase spine for one Phase, sharing the parent state schema.

    Compiled without a checkpointer so it inherits the parent graph's persistence.
    """
    builder = StateGraph(PipelineState)
    builder.add_node("prepare", _make_prepare(phase))
    builder.add_node("open_prompt_gate", _make_open_prompt_gate(phase))
    builder.add_node("prompt_gate", _make_prompt_gate(phase), destinations=("agent", "finalize"))
    builder.add_node("agent", _make_agent(phase))
    builder.add_node("open_output_gate", _make_open_output_gate(phase))
    builder.add_node("output_gate", _make_output_gate(phase), destinations=("agent", "finalize"))
    builder.add_node("finalize", _make_finalize(phase))
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "open_prompt_gate")
    builder.add_edge("open_prompt_gate", "prompt_gate")
    builder.add_edge("agent", "open_output_gate")
    builder.add_edge("open_output_gate", "output_gate")
    builder.add_edge("finalize", END)
    return builder.compile(name=f"phase_{phase.value}")


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "emit_event",
    "make_phase_subgraph",
    "stub_tool_names",
]
