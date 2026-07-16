"""Context summaries + dashboard evidence aggregation behind `/v1/context`.

Both functions take the loopback LangGraph client as an argument (duck-typed:
`runs.create`, `threads.search`, `threads.get`) so tests inject fakes and the
router stays thin. Always construct the client with the *caller's* API key —
loopback calls then hit the same auth filters as direct calls, which is what
scopes the evidence aggregation per consumer.
"""

import asyncio
from typing import Any

import structlog
from langgraph_sdk.errors import APIStatusError, NotFoundError

from apex.domain.diagnostics import (
    bounded_diagnostic,
    contains_credential_material,
    safe_type_name,
)
from apex.domain.pipeline import (
    MAX_CONTEXT_ID_CHARS,
    MAX_CONTEXT_REF_CHARS,
    MAX_CONTEXT_SOURCE_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    MAX_CONTEXT_TITLE_CHARS,
)
from apex.services.langgraph_client import delete_native_thread_definitively
from apex.services.public_projection import (
    native_run_stream_url,
    validated_native_identifier,
    validated_native_mapping_page,
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
_AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES = frozenset({408, 409, 425, 429})


class ContextRunStartError(RuntimeError):
    """The loopback runtime could not safely establish a context run."""


class ContextEvidenceReadError(RuntimeError):
    """The loopback runtime could not safely read context evidence."""


async def start_context_summary(
    client: Any,
    *,
    subject: str,
    work_item_keys: list[str] | None = None,
    document_packets: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
    work_tracking_connection_id: str | None = None,
) -> dict[str, str]:
    """Launch a durable background run on the `context` assistant.

    Returns {run_id, stream_url}; callers join the SSE stream by GETting
    `stream_url` on the LangGraph surface (same host) with their API key.
    """
    validated = validate_context_run_input(
        {
            "subject": subject,
            "work_item_keys": [] if work_item_keys is None else work_item_keys,
            "document_packets": [] if document_packets is None else document_packets,
            "project_id": project_id,
            "work_tracking_connection_id": work_tracking_connection_id,
        }
    )
    metadata = {
        "kind": "context_summary",
        "title": validated.subject,
        **({"project_id": validated.project_id} if validated.project_id is not None else {}),
        **(
            {"work_tracking_connection_id": validated.work_tracking_connection_id}
            if validated.work_tracking_connection_id is not None
            else {}
        ),
    }
    run_input = {
        "subject": validated.subject,
        "work_item_keys": validated.work_item_keys,
        "document_packets": [
            packet.model_dump(mode="json", exclude_none=True)
            for packet in validated.document_packets
        ],
        "project_id": validated.project_id,
        "work_tracking_connection_id": validated.work_tracking_connection_id,
    }
    if contains_credential_material({"metadata": metadata, "input": run_input}):
        raise ValueError("context summary must not contain credential material")
    thread_id: str | None = None
    thread_create_failed = False
    try:
        thread = await client.threads.create(metadata=metadata)
        thread_id = validated_native_identifier(
            thread.get("thread_id") if type(thread) is dict else None,
            label="context thread creation",
        )
    except Exception as exc:
        logger.warning(
            "context.thread_create_failed",
            error_type=safe_type_name(exc),
        )
        thread_create_failed = True
    if thread_create_failed:
        raise ContextRunStartError("context thread creation failed")
    assert thread_id is not None
    run_failure: str | None = None
    cleanup_cancelled = False
    run_id: str | None = None
    try:
        run = await client.runs.create(
            thread_id,
            "context",
            input=run_input,
            stream_mode="custom",
            stream_subgraphs=True,
            stream_resumable=True,
            durability="sync",
            multitask_strategy="reject",
        )
        run_id = validated_native_identifier(
            run.get("run_id") if type(run) is dict else None,
            label="context run creation",
        )
    except APIStatusError as exc:
        definitive_rejection = (
            400 <= exc.status_code < 500
            and exc.status_code not in _AMBIGUOUS_RUN_CREATE_CLIENT_STATUSES
        )
        if definitive_rejection:
            try:
                await delete_native_thread_definitively(client, thread_id)
            except asyncio.CancelledError:
                cleanup_cancelled = True
            except Exception as cleanup_exc:
                logger.warning(
                    "context.rejected_launch_thread_cleanup_failed",
                    thread_id=thread_id,
                    status_code=exc.status_code,
                    error_type=safe_type_name(cleanup_exc),
                )
            run_failure = "context run creation was rejected"
        else:
            logger.warning(
                "context.run_create_ambiguous",
                thread_id=thread_id,
                status_code=exc.status_code,
                error_type=safe_type_name(exc),
            )
            run_failure = "context run creation outcome is ambiguous"
    except Exception as exc:
        # A transport/server failure may happen after the run row commits. Keep
        # the thread so operators can reconcile the ambiguous run and its state.
        logger.warning(
            "context.run_create_ambiguous",
            thread_id=thread_id,
            error_type=safe_type_name(exc),
        )
        run_failure = "context run creation outcome is ambiguous"
    if cleanup_cancelled:
        raise asyncio.CancelledError
    if run_failure is not None:
        raise ContextRunStartError(run_failure)
    assert run_id is not None
    return {
        "run_id": run_id,
        "stream_url": native_run_stream_url(thread_id, run_id),
    }


def _packet_key(packet: dict[str, Any]) -> Any:
    return packet.get("id") or (packet.get("source"), packet.get("title"), packet.get("ref"))


def _public_text(value: Any, *, max_chars: int, required: bool = False) -> str | None:
    if type(value) is not str or len(value) > max_chars or (required and not value):
        return None
    return bounded_diagnostic(value, max_chars=max(1, len(value)))


def _public_evidence_packet(
    packet: Any,
    *,
    thread_id: Any,
) -> dict[str, Any] | None:
    """Project one legacy checkpoint packet through fixed response budgets."""

    if type(packet) is not dict:
        return None
    try:
        projected_thread_id = validated_native_identifier(
            thread_id,
            label="context evidence thread",
        )
    except RuntimeError:
        return None
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
    if type(limit) is not int or not 1 <= limit <= EVIDENCE_RESULT_PAGE_CAP:
        raise ValueError(f"evidence limit must be between 1 and {EVIDENCE_RESULT_PAGE_CAP}")
    if type(offset) is not int or not 0 <= offset <= 10_000:
        raise ValueError("evidence offset must be between 0 and 10000")
    threads: list[Any] = []
    page_limit = EVIDENCE_THREAD_PAGE_SIZE
    if thread_id is not None:
        invalid_thread = False
        try:
            requested_thread_id = validated_native_identifier(
                thread_id,
                label="context thread lookup",
            )
        except RuntimeError:
            invalid_thread = True
            requested_thread_id = ""
        if invalid_thread:
            raise LookupError("context thread not found")
        missing_thread = False
        lookup_failed = False
        try:
            threads = validated_native_mapping_page(
                [await client.threads.get(requested_thread_id)],
                requested_limit=1,
                label="context evidence thread lookup",
            )
        except (NotFoundError, KeyError):
            logger.info(
                "apex.context.thread_lookup_failed",
                thread_id=requested_thread_id,
            )
            missing_thread = True
        except Exception as exc:
            logger.warning(
                "apex.context.thread_lookup_unavailable",
                thread_id=requested_thread_id,
                error_type=safe_type_name(exc),
            )
            lookup_failed = True
        # Raise outside the handler so the caller-controlled provider exception
        # is not retained as ``__context__`` on a durable/traced service error.
        if missing_thread:
            raise LookupError("context thread not found")
        if lookup_failed:
            raise ContextEvidenceReadError("context evidence thread lookup failed")
        returned_thread_id = validated_native_identifier(
            threads[0].get("thread_id"),
            label="context evidence thread lookup",
        )
        if returned_thread_id != requested_thread_id:
            raise RuntimeError("context evidence thread lookup returned an unexpected identifier")
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
            search_failed = False
            raw_threads: Any = None
            try:
                raw_threads = await client.threads.search(
                    metadata=metadata,
                    limit=page_limit,
                    offset=scan_offset,
                    select=["thread_id"],
                    extract={"context_packets": "values.context_packets"},
                    sort_by="updated_at",
                    sort_order="desc",
                )
            except Exception as exc:
                logger.warning(
                    "apex.context.thread_search_unavailable",
                    error_type=safe_type_name(exc),
                )
                search_failed = True
            # Translate after leaving the provider exception handler so a
            # secret-bearing SDK/transport error is not retained in the
            # service exception's chain.
            if search_failed:
                raise ContextEvidenceReadError("context evidence thread search failed")
            threads = validated_native_mapping_page(
                raw_threads,
                requested_limit=page_limit,
                label="context evidence thread search",
            )
            if not threads:
                break
        for thread in threads:
            if type(thread) is not dict:
                continue
            extracted = thread.get("extracted")
            if type(extracted) is dict:
                raw_packets = extracted.get("context_packets")
                context_packets = [] if raw_packets is None else raw_packets
            else:
                values = thread.get("values")
                if type(values) is dict:
                    raw_packets = values.get("context_packets")
                    context_packets = [] if raw_packets is None else raw_packets
                else:
                    context_packets = []
            if (
                type(context_packets) is not list
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
