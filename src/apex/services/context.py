"""Context summaries + dashboard evidence aggregation behind `/v1/context`.

Both functions take the loopback LangGraph client as an argument (duck-typed:
`runs.create`, `threads.search`, `threads.get`) so tests inject fakes and the
router stays thin. Always construct the client with the *caller's* API key —
loopback calls then hit the same auth filters as direct calls, which is what
scopes the evidence aggregation per consumer.
"""

from typing import Any

import structlog
from langgraph_sdk.errors import APIStatusError, NotFoundError

from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.pipeline import (
    MAX_CONTEXT_ID_CHARS,
    MAX_CONTEXT_REF_CHARS,
    MAX_CONTEXT_SOURCE_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    MAX_CONTEXT_TITLE_CHARS,
)
from apex.services.run_validation import validate_context_run_input

logger = structlog.get_logger(__name__)

# Evidence aggregation scans at most this many threads per request (newest first
# via the server's default ordering). Cross-run evidence is a dashboard
# convenience view, not an audit log; older threads fall off rather than turning
# this endpoint into an unbounded scan. Raise deliberately if dashboards need more.
EVIDENCE_THREAD_SCAN_CAP = 50
EVIDENCE_THREAD_PAGE_SIZE = 10
EVIDENCE_RESULT_PAGE_CAP = 100
EVIDENCE_PACKETS_PER_THREAD_CAP = 64
_MAX_THREAD_ID_CHARS = 255
_AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES = frozenset({408, 409, 425, 429})


async def start_context_summary(
    client: Any,
    *,
    subject: str,
    work_item_keys: list[str] | None = None,
    document_packets: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    """Launch a durable background run on the `context` assistant.

    Returns {run_id, stream_url}; callers join the SSE stream by GETting
    `stream_url` on the LangGraph surface (same host) with their API key.
    """
    validated = validate_context_run_input(
        {
            "subject": subject,
            "work_item_keys": list(work_item_keys or []),
            "document_packets": list(document_packets or []),
            "project_id": project_id,
        }
    )
    metadata = {
        "kind": "context_summary",
        "title": validated.subject,
        **({"project_id": validated.project_id} if validated.project_id is not None else {}),
    }
    thread = await client.threads.create(metadata=metadata)
    thread_id = str(thread["thread_id"])
    try:
        run = await client.runs.create(
            thread_id,
            "context",
            input={
                "subject": validated.subject,
                "work_item_keys": validated.work_item_keys,
                "document_packets": [
                    packet.model_dump(mode="json", exclude_none=True)
                    for packet in validated.document_packets
                ],
                "project_id": validated.project_id,
            },
            stream_mode="custom",
            stream_subgraphs=True,
            stream_resumable=True,
            durability="sync",
            multitask_strategy="reject",
        )
    except APIStatusError as exc:
        definitive_rejection = (
            400 <= exc.status_code < 500
            and exc.status_code not in _AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES
        )
        if definitive_rejection:
            try:
                await client.threads.delete(thread_id)
            except Exception as cleanup_exc:
                logger.warning(
                    "context.rejected_launch_thread_cleanup_failed",
                    thread_id=thread_id,
                    status_code=exc.status_code,
                    error_type=cleanup_exc.__class__.__name__,
                )
        else:
            logger.warning(
                "context.run_create_ambiguous",
                thread_id=thread_id,
                status_code=exc.status_code,
                error_type=exc.__class__.__name__,
            )
        raise
    except Exception as exc:
        # A transport/server failure may happen after the run row commits. Keep
        # the thread so operators can reconcile the ambiguous run and its state.
        logger.warning(
            "context.run_create_ambiguous",
            thread_id=thread_id,
            error_type=exc.__class__.__name__,
        )
        raise
    run_id = str(run["run_id"])
    return {
        "run_id": run_id,
        "stream_url": f"/threads/{thread_id}/runs/{run_id}/stream?stream_mode=custom",
    }


def _packet_key(packet: dict[str, Any]) -> Any:
    return packet.get("id") or (packet.get("source"), packet.get("title"), packet.get("ref"))


def _public_text(value: Any, *, max_chars: int, required: bool = False) -> str | None:
    if not isinstance(value, str) or len(value) > max_chars or (required and not value):
        return None
    return bounded_diagnostic(value, max_chars=max(1, len(value)))


def _public_evidence_packet(
    packet: Any,
    *,
    thread_id: Any,
) -> dict[str, Any] | None:
    """Project one legacy checkpoint packet through fixed response budgets."""

    if not isinstance(packet, dict):
        return None
    projected_thread_id = _public_text(
        thread_id,
        max_chars=_MAX_THREAD_ID_CHARS,
        required=True,
    )
    source = _public_text(
        packet.get("source"),
        max_chars=MAX_CONTEXT_SOURCE_CHARS,
        required=True,
    )
    title = _public_text(
        packet.get("title"),
        max_chars=MAX_CONTEXT_TITLE_CHARS,
        required=True,
    )
    if projected_thread_id is None or source is None or title is None:
        return None
    packet_id = (
        None
        if packet.get("id") is None
        else _public_text(
            packet.get("id"),
            max_chars=MAX_CONTEXT_ID_CHARS,
            required=True,
        )
    )
    if packet.get("id") is not None and packet_id is None:
        return None
    return {
        "id": packet_id,
        "source": source,
        "title": title,
        "summary": _public_text(
            packet.get("summary"),
            max_chars=MAX_CONTEXT_SUMMARY_CHARS,
        ),
        "ref": _public_text(
            packet.get("ref"),
            max_chars=MAX_CONTEXT_REF_CHARS,
        ),
        "thread_id": projected_thread_id,
    }


async def collect_context_evidence(
    client: Any,
    *,
    project_id: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Aggregate `context_packets` from pipeline thread state, deduped by packet id.

    `thread_id` narrows to a single thread (LookupError if unreadable/missing);
    otherwise scans up to EVIDENCE_THREAD_SCAN_CAP threads, optionally filtered by
    `project_id` metadata. Consumer scoping rides on the loopback client's API key.
    """
    if not 1 <= limit <= EVIDENCE_RESULT_PAGE_CAP:
        raise ValueError(f"evidence limit must be between 1 and {EVIDENCE_RESULT_PAGE_CAP}")
    if not 0 <= offset <= 10_000:
        raise ValueError("evidence offset must be between 0 and 10000")
    threads: list[Any] = []
    page_limit = EVIDENCE_THREAD_PAGE_SIZE
    if thread_id is not None:
        try:
            threads = [await client.threads.get(thread_id)]
        except (NotFoundError, KeyError) as exc:
            logger.info("apex.context.thread_lookup_failed", thread_id=thread_id)
            raise LookupError(f"thread '{thread_id}' not found") from exc
    packets: list[dict[str, Any]] = []
    seen: set[Any] = set()
    scan_offset = 0
    while True:
        if thread_id is None:
            remaining = EVIDENCE_THREAD_SCAN_CAP - scan_offset
            if remaining <= 0:
                break
            page_limit = min(EVIDENCE_THREAD_PAGE_SIZE, remaining)
            metadata = {"project_id": project_id} if project_id is not None else None
            threads = await client.threads.search(
                metadata=metadata,
                limit=page_limit,
                offset=scan_offset,
                select=["thread_id"],
                extract={"context_packets": "values.context_packets"},
                sort_by="updated_at",
                sort_order="desc",
            )
            if not threads:
                break
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            extracted = thread.get("extracted")
            if isinstance(extracted, dict):
                context_packets = extracted.get("context_packets") or []
            else:
                values = thread.get("values") or {}
                context_packets = (
                    values.get("context_packets") or [] if isinstance(values, dict) else []
                )
            if (
                not isinstance(context_packets, list)
                or len(context_packets) > EVIDENCE_PACKETS_PER_THREAD_CAP
            ):
                continue
            for raw_packet in context_packets:
                packet = _public_evidence_packet(
                    raw_packet,
                    thread_id=thread.get("thread_id"),
                )
                if packet is None:
                    continue
                key = _packet_key(packet)
                if key in seen:
                    continue
                seen.add(key)
                packets.append(packet)
                if len(packets) >= offset + limit:
                    return packets[offset : offset + limit]
        if thread_id is not None:
            break
        scan_offset += len(threads)
        if len(threads) < page_limit:
            break
    return packets[offset : offset + limit]
