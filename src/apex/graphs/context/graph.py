"""Context-gathering graph: deterministic evidence assembly for `/v1/context/summaries`.

Input:  {subject, work_item_keys?, document_packets?, project_id?}
Output: {summary, evidence: [{id, source, title, ref, summary}]}

A single node resolves work-tracking through a loop-local ConnectionResolver and
combines those results with already-authorized uploaded-document packets supplied
by the API route. The graph never turns caller-provided document ids into reads.
Evidence ids are content-derived (sha256 of source|ref) so re-execution after
crash recovery is idempotent.

M4 note: LLM synthesis replaces the summary template then; the input/output
contract and evidence shape stay as-is.
"""

import hashlib
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import PortKind
from apex.services.connections import ConnectionResolver, DbConnectionStore
from apex.services.run_validation import CONTEXT_RUN_INPUT_KEYS, validate_context_run_input

JsonDict = dict[str, Any]

_SUMMARY_SNIPPET_CHARS = 280


class ContextInput(TypedDict, total=False):
    """Only caller-owned channels may enter a new context-graph checkpoint."""

    subject: str
    work_item_keys: list[str]
    document_packets: list[JsonDict]
    project_id: str | None
    app_id: str | None


class ContextState(TypedDict, total=False):
    # input
    subject: str
    work_item_keys: list[str]
    document_packets: list[JsonDict]
    project_id: str | None
    app_id: str | None
    # output
    summary: str
    evidence: list[JsonDict]


def _make_resolver() -> ConnectionResolver:
    """Create a resolver whose lifetime is exactly this graph-node invocation."""

    return ConnectionResolver(store=DbConnectionStore())


def _packet(source: str, title: str, ref: str | None, summary: str | None) -> JsonDict:
    packet_id = hashlib.sha256(f"{source}|{ref or title}".encode()).hexdigest()[:32]
    snippet = (summary or "").strip()[:_SUMMARY_SNIPPET_CHARS] or None
    return {"id": packet_id, "source": source, "title": title, "ref": ref, "summary": snippet}


async def _work_tracking_evidence(keys: list[str], project_id: str | None) -> list[JsonDict]:
    async with _make_resolver() as resolver:
        try:
            tracker = await resolver.resolve(PortKind.WORK_TRACKING, project_id=project_id)
        except (SQLAlchemyError, OSError):
            return []
        evidence: list[JsonDict] = []
        for key in keys:
            try:
                item = await tracker.get_item(key)
            except Exception:
                continue  # tolerate unknown keys / provider hiccups
            evidence.append(
                _packet("work_tracking", item.title, item.url or item.key, item.description)
            )
        return evidence


def _document_evidence(document_packets: list[JsonDict]) -> list[JsonDict]:
    """Normalize only packets authorized by the HTTP boundary."""

    evidence: list[JsonDict] = []
    for raw in document_packets:
        source = str(raw.get("source") or "document")
        title = str(raw.get("title") or raw.get("id") or "uploaded document")
        packet = _packet(
            source,
            title,
            str(raw["ref"]) if raw.get("ref") is not None else None,
            str(raw.get("text") or raw.get("summary") or "") or None,
        )
        if raw.get("id") is not None:
            packet["id"] = str(raw["id"])
        evidence.append(packet)
    return evidence


async def gather_evidence(state: ContextState) -> ContextState:
    """Gather stub evidence and compose a deterministic summary (LLM synthesis: M4)."""
    public_input = {key: state[key] for key in CONTEXT_RUN_INPUT_KEYS if key in state}
    validated = validate_context_run_input(public_input)
    subject = validated.subject
    project_id = validated.project_id
    evidence: list[JsonDict] = []
    evidence.extend(await _work_tracking_evidence(validated.work_item_keys, project_id))
    evidence.extend(
        _document_evidence(
            [
                packet.model_dump(mode="json", exclude_none=True)
                for packet in validated.document_packets
            ]
        )
    )

    lines = [f"Context summary for: {subject or '(no subject)'}"]
    lines.extend(f"- [{packet['source']}] {packet['title']}" for packet in evidence)
    if not evidence:
        lines.append("- no evidence gathered")
    return {"summary": "\n".join(lines), "evidence": evidence}


builder = StateGraph(ContextState, input_schema=ContextInput)
builder.add_node("gather_evidence", gather_evidence)
builder.add_edge(START, "gather_evidence")
builder.add_edge("gather_evidence", END)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile(name="context")
