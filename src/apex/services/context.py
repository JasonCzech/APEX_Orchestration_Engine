"""Context summaries + dashboard evidence aggregation behind `/v1/context`.

Both functions take the loopback LangGraph client as an argument (duck-typed:
`runs.create`, `threads.search`, `threads.get`) so tests inject fakes and the
router stays thin. Always construct the client with the *caller's* API key —
loopback calls then hit the same auth filters as direct calls, which is what
scopes the evidence aggregation per consumer.
"""

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Evidence aggregation scans at most this many threads per request (newest first
# via the server's default ordering). Cross-run evidence is a dashboard
# convenience view, not an audit log; older threads fall off rather than turning
# this endpoint into an unbounded scan. Raise deliberately if dashboards need more.
EVIDENCE_THREAD_SCAN_CAP = 50


async def start_context_summary(
    client: Any,
    *,
    subject: str,
    work_item_keys: list[str] | None = None,
    document_ids: list[str] | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    """Launch a stateless background run on the `context` assistant.

    Returns {run_id, stream_url}; callers join the SSE stream by GETting
    `stream_url` on the LangGraph surface (same host) with their API key.
    """
    run = await client.runs.create(
        None,
        "context",
        input={
            "subject": subject,
            "work_item_keys": list(work_item_keys or []),
            "document_ids": list(document_ids or []),
            "project_id": project_id,
        },
    )
    run_id = run["run_id"]
    return {"run_id": run_id, "stream_url": f"/runs/{run_id}/stream"}


def _packet_key(packet: dict[str, Any]) -> Any:
    return packet.get("id") or (packet.get("source"), packet.get("title"), packet.get("ref"))


async def collect_context_evidence(
    client: Any,
    *,
    project_id: str | None = None,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate `context_packets` from pipeline thread state, deduped by packet id.

    `thread_id` narrows to a single thread (LookupError if unreadable/missing);
    otherwise scans up to EVIDENCE_THREAD_SCAN_CAP threads, optionally filtered by
    `project_id` metadata. Consumer scoping rides on the loopback client's API key.
    """
    if thread_id is not None:
        try:
            threads: list[Any] = [await client.threads.get(thread_id)]
        except Exception as exc:
            logger.info("apex.context.thread_lookup_failed", thread_id=thread_id)
            raise LookupError(f"thread '{thread_id}' not found") from exc
    else:
        metadata = {"project_id": project_id} if project_id is not None else None
        threads = await client.threads.search(metadata=metadata, limit=EVIDENCE_THREAD_SCAN_CAP)

    packets: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for thread in threads[:EVIDENCE_THREAD_SCAN_CAP]:
        values = thread.get("values") or {}
        for packet in values.get("context_packets") or []:
            key = _packet_key(packet)
            if key in seen:
                continue
            seen.add(key)
            packets.append(
                {
                    "id": packet.get("id"),
                    "source": packet.get("source") or "",
                    "title": packet.get("title") or "",
                    "summary": packet.get("summary"),
                    "ref": packet.get("ref"),
                    "thread_id": thread.get("thread_id"),
                }
            )
    return packets
