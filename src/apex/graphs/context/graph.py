"""Context-gathering graph: deterministic evidence assembly for `/v1/context/summaries`.

Input:  {subject, work_item_keys?, document_ids?, project_id?}
Output: {summary, evidence: [{id, source, title, ref, summary}]}

A single node resolves the work-tracking and documents ports through the shared
ConnectionResolver (stub providers in dev/CI), fetches each requested item —
tolerating per-item failures so one bad key never sinks the run — and composes a
deterministic summary string. Evidence ids are content-derived (sha256 of
source|ref) so re-execution after crash recovery is idempotent.

M4 note: LLM synthesis replaces the summary template then; the input/output
contract and evidence shape stay as-is.
"""

import hashlib
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import SQLAlchemyError

from apex.adapters.registry import PortKind
from apex.domain.integrations import DocRef
from apex.services.connections import get_connection_resolver

JsonDict = dict[str, Any]

_SUMMARY_SNIPPET_CHARS = 280


class ContextState(TypedDict, total=False):
    # input
    subject: str
    work_item_keys: list[str]
    document_ids: list[str]
    project_id: str | None
    # output
    summary: str
    evidence: list[JsonDict]


def _packet(source: str, title: str, ref: str | None, summary: str | None) -> JsonDict:
    packet_id = hashlib.sha256(f"{source}|{ref or title}".encode()).hexdigest()[:32]
    snippet = (summary or "").strip()[:_SUMMARY_SNIPPET_CHARS] or None
    return {"id": packet_id, "source": source, "title": title, "ref": ref, "summary": snippet}


async def _work_tracking_evidence(keys: list[str], project_id: str | None) -> list[JsonDict]:
    try:
        tracker = await get_connection_resolver().resolve(
            PortKind.WORK_TRACKING, project_id=project_id
        )
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


def _doc_title(text: str, doc_id: str) -> str:
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first_line.lstrip("# ").strip() or doc_id


async def _document_evidence(document_ids: list[str], project_id: str | None) -> list[JsonDict]:
    try:
        docs = await get_connection_resolver().resolve(PortKind.DOCUMENTS, project_id=project_id)
    except (SQLAlchemyError, OSError):
        return []
    evidence: list[JsonDict] = []
    for doc_id in document_ids:
        try:
            content = await docs.fetch(DocRef(id=doc_id))
        except Exception:
            continue  # tolerate unknown document ids
        evidence.append(
            _packet("documents", _doc_title(content.text, doc_id), content.ref.uri, content.text)
        )
    return evidence


async def gather_evidence(state: ContextState) -> ContextState:
    """Gather stub evidence and compose a deterministic summary (LLM synthesis: M4)."""
    subject = (state.get("subject") or "").strip()
    project_id = state.get("project_id")
    evidence: list[JsonDict] = []
    evidence.extend(await _work_tracking_evidence(state.get("work_item_keys") or [], project_id))
    evidence.extend(await _document_evidence(state.get("document_ids") or [], project_id))

    lines = [f"Context summary for: {subject or '(no subject)'}"]
    lines.extend(f"- [{packet['source']}] {packet['title']}" for packet in evidence)
    if not evidence:
        lines.append("- no evidence gathered")
    return {"summary": "\n".join(lines), "evidence": evidence}


builder = StateGraph(ContextState)
builder.add_node("gather_evidence", gather_evidence)
builder.add_edge(START, "gather_evidence")
builder.add_edge("gather_evidence", END)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile(name="context")
