from typing import Any

import apex.services.langgraph_client as langgraph_client


def test_destructive_loopback_capability_is_opt_in_and_process_secret(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def fake_get_client(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(langgraph_client, "get_client", fake_get_client)

    assert langgraph_client.loopback_client("caller-key") is sentinel
    assert calls[-1] == {"api_key": "caller-key", "headers": None}

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
