from typing import Any

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
