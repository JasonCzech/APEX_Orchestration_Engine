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

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from apex.adapters.registry import PortKind
from apex.ports.work_tracking import WorkTrackingMutationTargetNotFoundError
from apex.services.connections import ConnectionResolver, DbConnectionStore
from apex.services.run_validation import CONTEXT_RUN_INPUT_KEYS, validate_context_run_input

JsonDict = dict[str, Any]

_SUMMARY_SNIPPET_CHARS = 280
CONTEXT_EVIDENCE_CONCURRENCY = 8
CONTEXT_EVIDENCE_PROCESS_CAPACITY = 16
CONTEXT_EVIDENCE_TOTAL_TIMEOUT_S = 30.0
_CONTEXT_PROVIDER_ADMISSION = threading.BoundedSemaphore(CONTEXT_EVIDENCE_PROCESS_CAPACITY)


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
        tracker = await resolver.resolve(PortKind.WORK_TRACKING, project_id=project_id)
        local_admission = asyncio.Semaphore(CONTEXT_EVIDENCE_CONCURRENCY)

        @asynccontextmanager
        async def provider_slot() -> AsyncIterator[None]:
            delay = 0.001
            while not _CONTEXT_PROVIDER_ADMISSION.acquire(blocking=False):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.05)
            try:
                yield
            finally:
                _CONTEXT_PROVIDER_ADMISSION.release()

        async def fetch(index: int, key: str) -> tuple[int, JsonDict | None]:
            async with local_admission, provider_slot():
                try:
                    item = await tracker.get_item(key)
                except asyncio.CancelledError:
                    raise
                except WorkTrackingMutationTargetNotFoundError:
                    return index, None  # a definitive missing work-item is not evidence
                return index, _packet(
                    "work_tracking", item.title, item.url or item.key, item.description
                )

        tasks = [asyncio.create_task(fetch(index, key)) for index, key in enumerate(keys)]
        if not tasks:
            return []
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=CONTEXT_EVIDENCE_TOTAL_TIMEOUT_S,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            completed: dict[int, JsonDict] = {}
            for task in tasks:
                if task in pending:
                    continue
                # Operational/provider exceptions are evidence-gathering failures,
                # not proof that no evidence exists. Re-raise after all timed-out
                # siblings have been cancelled and settled above.
                index, packet = task.result()
                if packet is not None:
                    completed[index] = packet
            if pending:
                # Returning the completed subset makes an unavailable provider look
                # indistinguishable from genuinely absent evidence. The caller can
                # retry a bounded operational failure, but cannot repair a false
                # negative embedded in a durable context summary.
                raise TimeoutError("work-tracking evidence gathering timed out")
            # Preserve caller order even though provider calls complete concurrently.
            return [completed[index] for index in sorted(completed)]
        finally:
            # Parent run cancellation can interrupt asyncio.wait itself. Every child
            # must settle before the resolver/adapter lease leaves this scope.
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)


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
    summary = "\n".join(lines)
    try:
        writer = get_stream_writer()
    except RuntimeError:
        pass
    else:
        # The public run stream is custom-only. Emit the one bounded result the
        # context UI consumes without exposing graph input, provider state, or
        # checkpoint internals through LangGraph's raw update/value modes.
        writer({"schema_version": 1, "type": "context_summary", "summary": summary})
    return {"summary": summary, "evidence": evidence}


builder = StateGraph(ContextState, input_schema=ContextInput)
builder.add_node("gather_evidence", gather_evidence)
builder.add_edge(START, "gather_evidence")
builder.add_edge("gather_evidence", END)

# Compiled without a checkpointer: the LangGraph server injects its own persistence.
graph = builder.compile(name="context")
