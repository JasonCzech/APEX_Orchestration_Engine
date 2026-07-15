"""SSRF guard for the agent's fetch_results tool (apex.services.results_fetch).

Deny-by-default: a URL is only fetchable when its host is allow-listed and not
private. These assert the guard alone (no network) — the happy host check uses
allow_private=True so it never performs DNS resolution.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import apex.adapters.network_safety as network_safety
from apex.graphs.pipeline.phase_subgraph import _invoke_agent_tool, _safe_tool_args
from apex.services import results_fetch
from apex.services.results_fetch import (
    FetchError,
    fetch_results_text,
    redact_fetch_url,
    validate_fetch_url,
)


def test_allow_listed_host_passes() -> None:
    host = validate_fetch_url(
        "https://results.example.com/run/42",
        allowed_hosts=["results.example.com"],
        allow_private=True,
    )
    assert host == "results.example.com"


def test_host_not_in_allow_list_is_rejected() -> None:
    # Rejected before any DNS resolution — the allow-list check comes first.
    with pytest.raises(FetchError, match="allow-list"):
        validate_fetch_url(
            "https://evil.example.com/x",
            allowed_hosts=["results.example.com"],
        )


def test_empty_allow_list_disables_the_tool() -> None:
    with pytest.raises(FetchError, match="disabled"):
        validate_fetch_url("https://results.example.com/x", allowed_hosts=[])


def test_private_or_loopback_ip_literal_is_rejected() -> None:
    # Even an allow-listed loopback literal is blocked unless allow_private is set.
    with pytest.raises(FetchError, match="private/loopback"):
        validate_fetch_url(
            "http://127.0.0.1/metadata",
            allowed_hosts=["127.0.0.1"],
            allow_private=False,
        )


def test_link_local_metadata_ip_is_rejected() -> None:
    with pytest.raises(FetchError, match="private/loopback"):
        validate_fetch_url(
            "http://169.254.169.254/latest/meta-data",
            allowed_hosts=["169.254.169.254"],
            allow_private=False,
        )


def test_non_http_scheme_is_rejected() -> None:
    with pytest.raises(FetchError, match="scheme"):
        validate_fetch_url("file:///etc/passwd", allowed_hosts=["results.example.com"])


def test_locked_fetch_requires_https_before_network_io() -> None:
    with pytest.raises(FetchError, match="must use https"):
        validate_fetch_url(
            "http://results.example.com/private/token?signature=abc",
            allowed_hosts=["results.example.com"],
            allow_private=False,
            require_https=True,
        )


@pytest.mark.parametrize("allow_private", [False, True])
def test_locked_agent_fetch_tool_propagates_https_policy(allow_private: bool) -> None:
    settings = SimpleNamespace(
        is_locked_down=True,
        llm=SimpleNamespace(
            fetch_allowed_hosts=["results.example.com"],
            fetch_allow_private_hosts=allow_private,
            fetch_max_bytes=1_000,
            fetch_timeout_s=1.0,
        ),
    )

    output = _invoke_agent_tool(
        SimpleNamespace(name="fetch_results"),
        {"url": "http://results.example.com/run/42?token=secret"},
        settings=settings,
        remaining_chars=2_000,
        approved_urls=frozenset({"http://results.example.com/run/42?token=secret"}),
    )

    assert "must use https" in output
    assert "token=secret" not in output


def test_agent_fetch_rejects_model_modified_url_before_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = "https://results.example.com/exfiltrate/context?signature=model-secret"
    approved = "https://results.example.com/run/42"
    settings = SimpleNamespace(
        is_locked_down=False,
        llm=SimpleNamespace(
            fetch_allowed_hosts=["results.example.com"],
            fetch_allow_private_hosts=False,
            fetch_max_bytes=1_000,
            fetch_timeout_s=1.0,
        ),
    )

    def forbidden_fetch(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("unapproved model URL reached network fetch")

    monkeypatch.setattr(results_fetch, "fetch_results_text", forbidden_fetch)

    output = _invoke_agent_tool(
        SimpleNamespace(name="fetch_results"),
        {"url": requested},
        settings=settings,
        remaining_chars=2_000,
        approved_urls=frozenset({approved}),
    )

    assert "not supplied by this run" in output
    assert "model-secret" not in output


def test_embedded_url_credentials_are_rejected_and_redacted() -> None:
    url = "https://alice:secret@results.example.com/private/token-123?signature=abc"

    with pytest.raises(FetchError, match="embedded credentials"):
        validate_fetch_url(
            url,
            allowed_hosts=["results.example.com"],
            allow_private=True,
        )

    assert redact_fetch_url(url) == "https://results.example.com"
    preview = _safe_tool_args({"url": url, "query": "ordinary input"})
    assert preview == {
        "url": "https://results.example.com",
        "query": "ordinary input",
    }


def test_tool_argument_preview_recursively_bounds_and_redacts_nested_values() -> None:
    nested = {
        "api_key": "provider-key-sentinel",
        "requests": [
            {
                "callback_url": "https://alice:secret@example.com/private?token=abc",
                "payload": {
                    "authorization": "Bearer access-token-sentinel",
                    "items": ["x" * 1_000] * 40,
                },
            }
        ]
        * 40,
    }

    preview = _safe_tool_args(nested)
    encoded = str(preview)

    assert "alice" not in encoded
    assert "secret" not in encoded
    assert "token=abc" not in encoded
    assert "provider-key-sentinel" not in encoded
    assert "access-token-sentinel" not in encoded
    assert preview["api_key"] == "[REDACTED]"
    assert len(preview["requests"]) == 17
    assert len(encoded.encode("utf-8")) <= 8_192


def test_tool_argument_preview_scrubs_dynamic_keys_values_and_custom_objects() -> None:
    class CredentialEcho:
        def __str__(self) -> str:
            return "Authorization: Bearer custom-object-token"

    signed_url = (
        "https://results.example.com/run/42?"
        "X-Amz-Credential=credential-secret&X-Amz-Signature=signature-secret"
    )
    dynamic_key = "password=dynamic-key-secret"
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    preview = _safe_tool_args(
        {
            dynamic_key: "ordinary",
            "generic_value": "Authorization: Bearer generic-value-token",
            "custom": CredentialEcho(),
            "generic_signed_link": signed_url,
            signed_url: "dynamic URL key",
            "cyclic": cyclic,
        }
    )
    encoded = str(preview)

    for credential in (
        "dynamic-key-secret",
        "generic-value-token",
        "custom-object-token",
        "credential-secret",
        "signature-secret",
    ):
        assert credential not in encoded
    assert "https://results.example.com" in encoded
    assert preview["cyclic"]["self"] == "…[cycle]"
    assert len(encoded.encode("utf-8")) <= 8_192


def test_fetch_stops_stream_at_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    consumed: list[bytes] = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for chunk in (b"abcd", b"efgh", b"must-not-be-read"):
                consumed.append(chunk)
                yield chunk

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(), request=request)
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)

    text = fetch_results_text(
        "https://results.example.com/large",
        allowed_hosts=["results.example.com"],
        allow_private=True,
        max_bytes=6,
    )
    assert text == "abcdef"
    assert consumed == [b"abcd", b"efgh"]


def test_fetch_does_not_request_an_extra_chunk_at_exact_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed: list[bytes] = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for chunk in (b"abcd", b"efgh", b"must-not-be-read"):
                consumed.append(chunk)
                yield chunk

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(), request=request)
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)

    text = fetch_results_text(
        "https://results.example.com/exact",
        allowed_hosts=["results.example.com"],
        allow_private=True,
        max_bytes=8,
    )

    assert text == "abcdefgh"
    assert consumed == [b"abcd", b"efgh"]


def test_fetch_enforces_one_wall_clock_deadline_across_trickled_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    consumed: list[bytes] = []

    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal now
            for chunk in (b"a", b"b", b"c"):
                now += 0.6
                consumed.append(chunk)
                yield chunk

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=TrickleStream(), request=request)
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)
    monkeypatch.setattr(results_fetch.time, "monotonic", lambda: now)

    with pytest.raises(FetchError, match="ReadTimeout"):
        fetch_results_text(
            "https://results.example.com/trickle",
            allowed_hosts=["results.example.com"],
            allow_private=True,
            timeout_s=1.0,
        )

    assert consumed == [b"a", b"b"]


def test_fetch_rejects_an_empty_body_that_finishes_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0

    class LateEmptyStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal now
            now = 1.1
            yield from ()

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=LateEmptyStream(), request=request)
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)
    monkeypatch.setattr(results_fetch.time, "monotonic", lambda: now)

    with pytest.raises(FetchError, match="ReadTimeout"):
        fetch_results_text(
            "https://results.example.com/empty",
            allowed_hosts=["results.example.com"],
            allow_private=True,
            timeout_s=1.0,
        )


def test_fetch_requires_identity_encoding_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = False
    seen_accept_encoding: str | None = None

    class CompressedStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal consumed
            consumed = True
            yield b"compressed-bomb"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_accept_encoding
        seen_accept_encoding = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=CompressedStream(),
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)

    with pytest.raises(FetchError, match="identity encoding"):
        fetch_results_text(
            "https://results.example.com/bomb",
            allowed_hosts=["results.example.com"],
            allow_private=True,
            max_bytes=8,
        )

    assert seen_accept_encoding == "identity"
    assert consumed is False


def test_fetch_rejects_declared_oversize_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = False

    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal consumed
            consumed = True
            yield b"large"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": "9"},
            stream=LargeStream(),
            request=request,
        )
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)

    with pytest.raises(FetchError, match="exceeds"):
        fetch_results_text(
            "https://results.example.com/large",
            allowed_hosts=["results.example.com"],
            allow_private=True,
            max_bytes=8,
        )

    assert consumed is False


def test_fetch_http_error_does_not_persist_path_or_query_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text="upstream failed", request=request)
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("transport", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(results_fetch.httpx, "Client", client_factory)
    url = "https://results.example.com/private/token-123?signature=abc"

    with pytest.raises(FetchError) as error:
        fetch_results_text(
            url,
            allowed_hosts=["results.example.com"],
            allow_private=True,
        )

    message = str(error.value)
    assert "https://results.example.com" in message
    assert "token-123" not in message
    assert "signature" not in message


def test_dns_rebinding_is_rejected_again_at_socket_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_resolve(host: str, port: int, **_kwargs: object) -> list[str]:
        del host, port
        nonlocal calls
        calls += 1
        return ["169.254.169.254"]

    monkeypatch.setattr(network_safety, "_resolve_hostname_sync", fake_resolve)

    with pytest.raises(FetchError, match="private adapter hosts are disabled"):
        fetch_results_text(
            "http://results.example.com/run/42",
            allowed_hosts=["results.example.com"],
        )

    assert calls == 1  # allow-list validation is syntax-only; connect-time DNS is authoritative
