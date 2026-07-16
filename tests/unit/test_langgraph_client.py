import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import apex.services.langgraph_client as langgraph_client


def test_loopback_capability_is_process_secret_for_every_facade_call(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def fake_get_client(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(langgraph_client, "get_client", fake_get_client)

    assert langgraph_client.loopback_client("caller-key") is sentinel
    headers = calls[-1]["headers"]
    assert isinstance(headers, dict)
    encoded = {str(key).encode(): str(value).encode() for key, value in headers.items()}
    assert langgraph_client.is_trusted_loopback(encoded) is True

    assert langgraph_client.loopback_client("caller-key", authorize_destructive=True) is sentinel
    headers = calls[-1]["headers"]
    assert isinstance(headers, dict)
    encoded = {str(key).encode(): str(value).encode() for key, value in headers.items()}
    assert langgraph_client.is_trusted_loopback(encoded) is True
    assert (
        langgraph_client.is_trusted_loopback(
            {b"x-apex-trusted-loopback": b"caller-controlled-value"}
        )
        is False
    )


async def test_rejected_thread_deletion_settles_under_repeated_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    class Threads:
        async def delete(self, thread_id: str) -> None:
            assert thread_id == "thread-1"
            started.set()
            await release.wait()
            completed.set()
            raise RuntimeError("deletion failed after cancellation")

    operation = asyncio.create_task(
        langgraph_client.delete_native_thread_definitively(
            SimpleNamespace(threads=Threads()),
            "thread-1",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, timeout=1)
    assert completed.is_set()
