"""Context service: run creation + evidence aggregation with a fake loopback client."""

from typing import Any

import pytest

from apex.services.context import (
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
        self.calls.append({"thread_id": thread_id, "assistant_id": assistant_id, "input": input})
        return {"run_id": "run-123", "status": "pending"}


class FakeThreads:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self._threads = threads
        self.search_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    async def search(
        self, *, metadata: Any = None, limit: int = 10, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"metadata": metadata, "limit": limit})
        return self._threads[:limit]

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


async def test_start_context_summary_creates_stateless_background_run() -> None:
    client = FakeLoopbackClient()
    result = await start_context_summary(
        client,
        subject="Checkout latency",
        work_item_keys=["PHX-241"],
        project_id="proj-a",
    )
    assert result == {"run_id": "run-123", "stream_url": "/runs/run-123/stream"}
    [call] = client.runs.calls
    assert call["thread_id"] is None  # stateless run
    assert call["assistant_id"] == "context"
    assert call["input"] == {
        "subject": "Checkout latency",
        "work_item_keys": ["PHX-241"],
        "document_ids": [],
        "project_id": "proj-a",
    }


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


async def test_evidence_search_is_capped_and_project_filtered() -> None:
    threads = [
        _thread(f"t{i}", [_packet(f"pkt-{i}")]) for i in range(EVIDENCE_THREAD_SCAN_CAP + 25)
    ]
    client = FakeLoopbackClient(threads)
    packets = await collect_context_evidence(client, project_id="proj-a")
    [search_call] = client.threads.search_calls
    assert search_call == {"metadata": {"project_id": "proj-a"}, "limit": EVIDENCE_THREAD_SCAN_CAP}
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
