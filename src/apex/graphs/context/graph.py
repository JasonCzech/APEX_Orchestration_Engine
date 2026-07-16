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
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.pipeline import (
    MAX_CONTEXT_REF_CHARS,
    MAX_CONTEXT_SOURCE_CHARS,
    MAX_CONTEXT_TITLE_CHARS,
)
from apex.ports.work_tracking import WorkTrackingMutationTargetNotFoundError
from apex.services.connections import (
    ConnectionResolver,
    DbConnectionStore,
    close_adapter,
    validate_resolved_work_tracking_project,
)
from apex.services.run_validation import CONTEXT_RUN_INPUT_KEYS, validate_context_run_input
from apex.services.work_items import validated_provider_work_item

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
    """Build a bounded, secret-free packet from an untrusted provider response."""

    safe_source = bounded_diagnostic(source, max_chars=MAX_CONTEXT_SOURCE_CHARS)
    safe_title = bounded_diagnostic(title, max_chars=MAX_CONTEXT_TITLE_CHARS)
    safe_ref = bounded_diagnostic(ref, max_chars=MAX_CONTEXT_REF_CHARS) if ref is not None else None
    safe_summary = (
        bounded_diagnostic(summary, max_chars=_SUMMARY_SNIPPET_CHARS).strip() or None
        if summary is not None
        else None
    )
    packet_id = hashlib.sha256(f"{safe_source}|{safe_ref or safe_title}".encode()).hexdigest()[:32]
    return {
        "id": packet_id,
        "source": safe_source,
        "title": safe_title,
        "ref": safe_ref,
        "summary": safe_summary,
    }


async def _work_tracking_evidence(keys: list[str], project_id: str | None) -> list[JsonDict]:
    resolver = _make_resolver()
    tracker: Any | None = None
    try:
        tracker = await resolver.resolve(PortKind.WORK_TRACKING, project_id=project_id)
        validate_resolved_work_tracking_project(
            tracker,
            provider=getattr(tracker, "provider", ""),
            requested_project_id=project_id,
        )
        return await _gather_tracker_evidence(tracker, keys)
    finally:
        await _close_context_resources_definitively(tracker, resolver)


async def _gather_tracker_evidence(tracker: Any, keys: list[str]) -> list[JsonDict]:
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
                item = validated_provider_work_item(await tracker.get_item(key))
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
            await _settle_cancelled_evidence_tasks(list(pending))

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
        # Parent run cancellation can interrupt asyncio.wait itself, and one
        # completed provider failure can make ``task.result()`` skip later failed
        # siblings. Always gather every task with ``return_exceptions=True`` so no
        # provider diagnostic escapes through the event loop's exception handler,
        # and settle all children before the resolver lease leaves this scope.
        await _settle_cancelled_evidence_tasks(tasks)


async def _close_context_resources_definitively(tracker: Any | None, resolver: Any) -> None:
    """Release the tracker checkout and resolver cache under repeated cancellation."""

    close = getattr(resolver, "close", None)
    if callable(close):
        resolver_cleanup = close()
    else:
        exit_method = getattr(resolver, "__aexit__", None)
        if callable(exit_method):
            resolver_cleanup = exit_method(None, None, None)
        else:

            async def _noop_resolver_cleanup() -> None:
                return None

            resolver_cleanup = _noop_resolver_cleanup()
    coroutines = [resolver_cleanup]
    if tracker is not None:
        coroutines.insert(0, close_adapter(tracker))

    async def run_cleanup(awaitable: Any) -> None:
        await awaitable

    tasks = [asyncio.create_task(run_cleanup(coroutine)) for coroutine in coroutines]
    cancelled = False
    error: BaseException | None = None
    for task in tasks:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            task.result()
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as exc:
            if error is None:
                error = exc
    if cancelled:
        raise asyncio.CancelledError from None
    if error is not None:
        raise error


async def _settle_cancelled_evidence_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel and definitively settle children even under repeated parent cancel."""

    for task in tasks:
        if not task.done():
            task.cancel()
    if not tasks:
        return

    async def settle() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    waiter = asyncio.create_task(settle())
    interrupted = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # A fresh shield on the next iteration keeps waiting for the same
            # settlement task instead of cancelling child cleanup.
            interrupted = True
    # Retrieve synchronously once done. A final ``await waiter`` would introduce
    # one more cancellation checkpoint after cleanup has already settled and can
    # obscure the original cancellation under a tightly repeated cancel signal.
    waiter.result()
    if interrupted:
        raise asyncio.CancelledError


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
    provider_failed = False
    try:
        evidence.extend(await _work_tracking_evidence(validated.work_item_keys, project_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        # LangGraph may persist and surface node failures. Provider/resolver
        # exceptions are untrusted diagnostics. Raise a fixed error after
        # leaving the handler so opaque provider secrets are neither rendered
        # nor retained in ``__context__`` by the durable graph failure.
        provider_failed = True
    if provider_failed:
        raise RuntimeError("work-tracking evidence gathering failed")
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
