"""Context service: run creation + evidence aggregation with a fake loopback client."""

from typing import Any, cast

import httpx
import pytest
from langgraph_sdk.errors import APIStatusError

from apex.services.context import (
    EVIDENCE_PACKETS_PER_THREAD_CAP,
    EVIDENCE_THREAD_SCAN_CAP,
    collect_context_evidence,
    start_context_summary,
)


class FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self, thread_id: str | None, assistant_id: str, *, input: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "thread_id": thread_id,
                "assistant_id": assistant_id,
                "input": input,
                **kwargs,
            }
        )
        return {"run_id": "run-123", "status": "pending"}


class FakeThreads:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self._threads = threads
        self.search_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []

    async def create(self, *, metadata: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append({"metadata": metadata})
        return {"thread_id": "thread-context-1", "metadata": metadata}

    async def delete(self, thread_id: str) -> None:
        self.delete_calls.append(thread_id)

    async def search(
        self,
        *,
        metadata: Any = None,
        limit: int = 10,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"metadata": metadata, "limit": limit, "offset": offset, **kwargs})
        page = self._threads[offset : offset + limit]
        if kwargs.get("extract") is None:
            return page
        return [
            {
                "thread_id": thread["thread_id"],
                "extracted": {
                    "context_packets": (thread.get("values") or {}).get("context_packets") or []
                },
            }
            for thread in page
        ]

    async def get(self, thread_id: str) -> dict[str, Any]:
        self.get_calls.append(thread_id)
        for thread in self._threads:
            if thread["thread_id"] == thread_id:
                return thread
        raise KeyError(thread_id)


class FakeLoopbackClient:
    def __init__(self, threads: list[dict[str, Any]] | None = None) -> None:
        self.runs = FakeRuns()
        self.threads = FakeThreads(threads or [])


def _thread(thread_id: str, packets: list[dict[str, Any]]) -> dict[str, Any]:
    return {"thread_id": thread_id, "values": {"context_packets": packets}}


def _packet(packet_id: str, title: str = "t") -> dict[str, Any]:
    return {"id": packet_id, "source": "work_tracking", "title": title, "ref": None}


async def test_start_context_summary_creates_durable_streamable_run() -> None:
    client = FakeLoopbackClient()
    result = await start_context_summary(
        client,
        subject="Checkout latency",
        work_item_keys=["PHX-241"],
        project_id="proj-a",
    )
    assert result == {
        "run_id": "run-123",
        "stream_url": "/threads/thread-context-1/runs/run-123/stream?stream_mode=custom",
    }
    assert client.threads.create_calls == [
        {
            "metadata": {
                "kind": "context_summary",
                "title": "Checkout latency",
                "project_id": "proj-a",
            }
        }
    ]
    [call] = client.runs.calls
    assert call["thread_id"] == "thread-context-1"
    assert call["assistant_id"] == "context"
    assert call["input"] == {
        "subject": "Checkout latency",
        "work_item_keys": ["PHX-241"],
        "document_packets": [],
        "project_id": "proj-a",
    }
    assert call["stream_resumable"] is True
    assert call["stream_mode"] == "custom"
    assert call["durability"] == "sync"
    assert call["multitask_strategy"] == "reject"


@pytest.mark.parametrize(("status_code", "deleted"), [(404, True), (409, False), (503, False)])
async def test_context_summary_cleans_up_only_definitively_rejected_run_creates(
    status_code: int,
    deleted: bool,
) -> None:
    client = FakeLoopbackClient()

    async def reject(*args: Any, **kwargs: Any) -> dict[str, Any]:
        request = httpx.Request(
            "POST",
            "http://loopback/threads/thread-context-1/runs",
        )
        raise APIStatusError(
            "run create failed",
            response=httpx.Response(status_code, request=request),
            body=None,
        )

    cast(Any, client.runs).create = reject
    with pytest.raises(APIStatusError):
        await start_context_summary(client, subject="Checkout latency", project_id="proj-a")

    assert client.threads.delete_calls == (["thread-context-1"] if deleted else [])


async def test_evidence_dedupes_packets_across_threads() -> None:
    shared = _packet("pkt-shared", "seen twice")
    client = FakeLoopbackClient(
        [
            _thread("t1", [shared, _packet("pkt-a")]),
            _thread("t2", [dict(shared), _packet("pkt-b")]),
            {"thread_id": "t3", "values": None},  # tolerated: no state yet
        ]
    )
    packets = await collect_context_evidence(client)
    assert [p["id"] for p in packets] == ["pkt-shared", "pkt-a", "pkt-b"]
    assert packets[0]["thread_id"] == "t1"  # first sighting wins


async def test_evidence_projection_drops_malformed_and_oversized_legacy_packets() -> None:
    client = FakeLoopbackClient(
        [
            _thread(
                "t1",
                [
                    cast(Any, "not-an-object"),
                    {"id": ["unhashable"], "source": "document", "title": "bad id"},
                    {"id": "too-large", "source": "document", "title": "x" * 501},
                    _packet("safe", "bounded evidence"),
                ],
            ),
            _thread(
                "t2",
                [
                    _packet(f"overflow-{index}")
                    for index in range(EVIDENCE_PACKETS_PER_THREAD_CAP + 1)
                ],
            ),
        ]
    )

    packets = await collect_context_evidence(client)

    assert packets == [
        {
            "id": "safe",
            "source": "work_tracking",
            "title": "bounded evidence",
            "summary": None,
            "ref": None,
            "thread_id": "t1",
        }
    ]


async def test_evidence_projection_redacts_legacy_credentials() -> None:
    client = FakeLoopbackClient(
        [
            _thread(
                "t1",
                [
                    {
                        "id": "packet-1",
                        "source": "document",
                        "title": "jira_pat=legacy-title-secret",
                        "summary": "Authorization: Bearer legacy-summary-secret",
                        "ref": "https://user:legacy-url-secret@example.test/evidence",
                    }
                ],
            )
        ]
    )

    [packet] = await collect_context_evidence(client)
    rendered = repr(packet)

    assert "[REDACTED]" in rendered
    assert "legacy-title-secret" not in rendered
    assert "legacy-summary-secret" not in rendered
    assert "legacy-url-secret" not in rendered


async def test_evidence_search_is_capped_and_project_filtered() -> None:
    threads = [
        _thread(f"t{i}", [_packet(f"pkt-{i}")]) for i in range(EVIDENCE_THREAD_SCAN_CAP + 25)
    ]
    client = FakeLoopbackClient(threads)
    packets = await collect_context_evidence(client, project_id="proj-a")
    assert len(client.threads.search_calls) == 5
    assert [call["offset"] for call in client.threads.search_calls] == [0, 10, 20, 30, 40]
    for search_call in client.threads.search_calls:
        assert search_call == {
            "metadata": {"project_id": "proj-a"},
            "limit": 10,
            "offset": search_call["offset"],
            "select": ["thread_id"],
            "extract": {"context_packets": "values.context_packets"},
            "sort_by": "updated_at",
            "sort_order": "desc",
        }
    assert len(packets) == EVIDENCE_THREAD_SCAN_CAP


async def test_evidence_without_project_passes_no_metadata_filter() -> None:
    client = FakeLoopbackClient([_thread("t1", [_packet("pkt-1")])])
    await collect_context_evidence(client)
    assert client.threads.search_calls[0]["metadata"] is None


async def test_thread_id_narrows_to_one_thread_without_searching() -> None:
    client = FakeLoopbackClient(
        [_thread("t1", [_packet("pkt-1")]), _thread("t2", [_packet("pkt-2")])]
    )
    packets = await collect_context_evidence(client, thread_id="t2")
    assert [p["id"] for p in packets] == ["pkt-2"]
    assert packets[0]["thread_id"] == "t2"
    assert client.threads.get_calls == ["t2"]
    assert client.threads.search_calls == []


async def test_unknown_thread_raises_lookup_error() -> None:
    client = FakeLoopbackClient([])
    with pytest.raises(LookupError):
        await collect_context_evidence(client, thread_id="missing")


async def test_thread_lookup_backend_failure_is_not_misreported_as_missing() -> None:
    client = FakeLoopbackClient([])

    async def fail(_thread_id: str) -> dict[str, Any]:
        raise RuntimeError("backend unavailable")

    client.threads.get = fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="backend unavailable"):
        await collect_context_evidence(client, thread_id="thread-1")
