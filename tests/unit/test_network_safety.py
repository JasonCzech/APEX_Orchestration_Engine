"""Connect-time DNS pinning shared by adapters and result fetching."""

import socket

import httpx
import pytest

from apex.adapters.network_safety import (
    UnsafeDestinationError,
    resolve_destination,
    safe_async_http_client,
)
from apex.services.connections import validate_adapter_base_url


def _answer(address: str, port: int | None) -> tuple[object, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 80))


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            _answer("93.184.216.34", 443),
            _answer("10.0.0.8", 443),
        ],
    )

    with pytest.raises(UnsafeDestinationError, match="private adapter hosts"):
        resolve_destination("mixed.example", 443)


async def test_adapter_transport_revalidates_dns_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["93.184.216.34", "127.0.0.1"])
    calls = 0

    def fake_getaddrinfo(
        host: str, port: int | None, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        return [_answer(next(answers), port)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    validate_adapter_base_url("http://rebind.example")
    async with safe_async_http_client(timeout=1.0) as client:
        with pytest.raises(httpx.ConnectError, match="private adapter hosts are disabled"):
            await client.get("http://rebind.example/resource")

    assert calls == 2  # persistence validation, then the actual socket connect
