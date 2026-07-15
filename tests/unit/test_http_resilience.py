import asyncio
import gzip
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest

from apex.adapters.http_resilience import (
    HARD_MAX_BUFFERED_RESPONSE_BYTES,
    CircuitBreaker,
    CircuitOpenError,
    InvalidJsonResponseError,
    ResponseTooLargeError,
    RetryPolicy,
    parse_json_response,
    resilient_request,
    resilient_stream_request,
    retry_policy,
)


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.iterated = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


async def _noop_sleep(_: float) -> None:
    return None


async def test_resilient_request_retries_idempotent_transient_status() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client, "GET", "/health", sleep_fn=_noop_sleep, random_fn=lambda: 0.0
        )

    assert response.status_code == 200
    assert calls == 2


async def test_resilient_request_does_not_retry_unsafe_methods_by_default() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client, "POST", "/create", sleep_fn=_noop_sleep, random_fn=lambda: 0.0
        )

    assert response.status_code == 503
    assert calls == 1


async def test_resilient_request_caps_streamed_response_body() -> None:
    stream = TrackingStream(b"x" * 11)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ResponseTooLargeError, match="limit of 10 bytes"):
            await resilient_request(client, "GET", "/large", max_response_bytes=10)

    assert stream.iterated is True
    assert stream.closed is True


async def test_resilient_request_rejects_compressed_decompression_bomb() -> None:
    compressed = gzip.compress(b"x" * (8 * 1024 * 1024), compresslevel=9)
    assert len(compressed) < 16 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=compressed,
            request=request,
        )

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ResponseTooLargeError, match="compressed responses"):
            await resilient_request(client, "GET", "/bomb")


async def test_resilient_request_overrides_lowercase_accept_encoding_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get_list("accept-encoding") == ["identity"]
        return httpx.Response(200, json={"ok": True}, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client,
            "GET",
            "/headers",
            headers={"accept-encoding": "gzip"},
        )

    assert response.json() == {"ok": True}


async def test_resilient_request_rejects_cap_above_hard_ceiling_without_io() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="max_response_bytes"):
            await resilient_request(
                client,
                "GET",
                "/large",
                max_response_bytes=HARD_MAX_BUFFERED_RESPONSE_BYTES + 1,
            )

    assert calls == 0


async def test_resilient_request_allows_explicit_post_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200 if calls == 2 else 502, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client,
            "POST",
            "/search",
            retry=retry_policy(retry_methods={"POST"}),
            sleep_fn=_noop_sleep,
            random_fn=lambda: 0.0,
        )

    assert response.status_code == 200
    assert calls == 2


async def test_resilient_request_honors_retry_after_seconds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, request=request)
        return httpx.Response(200, request=request)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client,
            "GET",
            "/poll",
            retry=RetryPolicy(attempts=2, max_delay_s=5.0, total_timeout_s=None),
            sleep_fn=record_sleep,
        )

    assert response.status_code == 200
    assert sleeps == [2.0]


async def test_resilient_request_retry_after_not_clamped_to_max_delay() -> None:
    # Regression: Retry-After must be honored up to retry_after_cap_s, not silently
    # clamped to the tiny local backoff cap (max_delay_s), which made it inert.
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not sleeps:
            return httpx.Response(429, headers={"retry-after": "20"}, request=request)
        return httpx.Response(200, request=request)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client,
            "GET",
            "/poll",
            # default max_delay_s=0.5; retry_after_cap_s=30 -> 20s Retry-After honored.
            retry=RetryPolicy(attempts=2, total_timeout_s=None),
            sleep_fn=record_sleep,
        )

    assert response.status_code == 200
    assert sleeps == [20.0]


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "-Infinity"])
async def test_resilient_request_ignores_non_finite_retry_after(retry_after: str) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200 if calls == 2 else 429,
            headers={"retry-after": retry_after},
            request=request,
        )

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_request(
            client,
            "GET",
            "/poll",
            retry=RetryPolicy(attempts=2, total_timeout_s=None),
            sleep_fn=record_sleep,
            random_fn=lambda: 0.0,
        )

    assert response.status_code == 200
    assert sleeps == [0.05]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_delay_s", float("nan")),
        ("max_delay_s", float("inf")),
        ("retry_after_cap_s", float("-inf")),
        ("total_timeout_s", float("nan")),
        ("base_delay_s", -0.1),
    ],
)
def test_retry_policy_rejects_unsafe_delay_values(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        RetryPolicy(**cast(Any, {field: value}))


@pytest.mark.parametrize("jitter", [float("nan"), float("inf"), -0.1, 1.1])
async def test_resilient_request_rejects_unsafe_jitter(jitter: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="retry jitter"):
            await resilient_request(
                client,
                "GET",
                "/poll",
                retry=RetryPolicy(attempts=2),
                random_fn=lambda: jitter,
            )


async def test_retry_deadline_bounds_injected_sleep() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async def stalled_sleep(_: float) -> None:
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(httpx.TimeoutException, match="total retry timeout"):
            async with asyncio.timeout(0.5):
                await resilient_request(
                    client,
                    "GET",
                    "/poll",
                    retry=RetryPolicy(attempts=2, total_timeout_s=0.01),
                    sleep_fn=stalled_sleep,
                    random_fn=lambda: 0.0,
                )


async def test_resilient_request_enforces_total_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(httpx.TimeoutException):
            await resilient_request(
                client,
                "GET",
                "/poll",
                retry=RetryPolicy(total_timeout_s=0.0),
            )

    assert calls == 0


async def test_resilient_request_total_timeout_includes_slow_stream_body() -> None:
    class SlowStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0.05)
            yield b"late"

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(httpx.ReadTimeout, match="reading response body"):
            await resilient_request(
                client,
                "GET",
                "/slow-body",
                retry=RetryPolicy(attempts=1, total_timeout_s=0.01),
            )

    assert stream.closed is True


async def test_resilient_stream_request_retries_transport_error_without_buffering() -> None:
    calls = 0

    class TrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.iterated = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.iterated = True
            yield b"streamed"

    stream = TrackingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_stream_request(
            client, "GET", "/report", sleep_fn=_noop_sleep, random_fn=lambda: 0.0
        )
        assert stream.iterated is False
        assert await response.aread() == b"streamed"
        await response.aclose()

    assert calls == 2


async def test_resilient_stream_request_closes_transient_response_before_retry() -> None:
    class TrackingStream(httpx.AsyncByteStream):
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.iterated = False
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.iterated = True
            yield self.data

        async def aclose(self) -> None:
            self.closed = True

    calls = 0
    first_stream = TrackingStream(b"retry body must not be buffered")
    final_stream = TrackingStream(b"ok")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(599, stream=first_stream, request=request)
        return httpx.Response(200, stream=final_stream, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        response = await resilient_stream_request(
            client, "GET", "/report", sleep_fn=_noop_sleep, random_fn=lambda: 0.0
        )
        assert first_stream.closed is True
        assert first_stream.iterated is False
        assert final_stream.iterated is False
        assert await response.aread() == b"ok"
        await response.aclose()

    assert calls == 2


async def test_circuit_breaker_opens_after_failures_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("apex.adapters.http_resilience.time.monotonic", lambda: now)
    breaker = CircuitBreaker("poll", failure_threshold=2, reset_after_s=5)
    policy = RetryPolicy(attempts=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    ) as client:
        await resilient_request(client, "GET", "/poll", retry=policy, breaker=breaker)
        await resilient_request(client, "GET", "/poll", retry=policy, breaker=breaker)
        with pytest.raises(CircuitOpenError):
            await resilient_request(client, "GET", "/poll", retry=policy, breaker=breaker)

    now = 106.0
    breaker.before_request()


def test_parse_json_response_accepts_bounded_utf8_json() -> None:
    response = httpx.Response(
        200,
        content=b'{"items":[1,true,null,"ok"]}',
        headers={"Content-Type": "application/problem+json; charset=utf-8"},
    )

    assert parse_json_response(response, context="provider response") == {
        "items": [1, True, None, "ok"]
    }


def test_parse_json_response_rejects_deep_json_before_recursive_decode() -> None:
    response = httpx.Response(
        200,
        content=("[" * 65 + "0" + "]" * 65).encode(),
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(InvalidJsonResponseError, match="64-level nesting"):
        parse_json_response(response, context="provider response")


def test_parse_json_response_rejects_flat_token_amplification() -> None:
    response = httpx.Response(
        200,
        content=b"[0,0,0,0]",
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(InvalidJsonResponseError, match="4-token"):
        parse_json_response(response, context="provider response", max_tokens=4)


@pytest.mark.parametrize(
    "body",
    [
        b'{"phase":"running","phase":"completed"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
        b'{"value":-1e999}',
        b'{"value":1e-999}',
        b'{"value":' + (b"9" * 5_000) + b"}",
    ],
)
def test_parse_json_response_rejects_ambiguous_or_unsafe_json(body: bytes) -> None:
    response = httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "application/json"},
    )

    with pytest.raises(InvalidJsonResponseError, match="invalid JSON"):
        parse_json_response(response, context="provider response")


def test_parse_json_response_rejects_duplicate_content_type_headers() -> None:
    response = httpx.Response(
        200,
        content=b'{"ok":true}',
        headers=[
            (b"content-type", b"application/json"),
            (b"content-type", b"application/json"),
        ],
    )

    with pytest.raises(InvalidJsonResponseError, match="ambiguous Content-Type"):
        parse_json_response(response, context="provider response")


@pytest.mark.parametrize(
    ("content", "headers", "match"),
    [
        (b'{"ok":true}', {"Content-Type": "text/html"}, "non-JSON Content-Type"),
        (
            b'{\x00"\x00o\x00k\x00"\x00:\x00t\x00r\x00u\x00e\x00}',
            {"Content-Type": "application/json; charset=utf-16"},
            "unsupported JSON charset",
        ),
        (
            b'{"ok":true}',
            {"Content-Type": "application/json; charset = utf-16"},
            "unsupported JSON charset",
        ),
        (
            b'{"ok":true}',
            {"Content-Type": "application/json; charset=utf-8; charset = us-ascii"},
            "unsupported JSON charset",
        ),
    ],
)
def test_parse_json_response_rejects_ambiguous_encoding(
    content: bytes, headers: dict[str, str], match: str
) -> None:
    with pytest.raises(InvalidJsonResponseError, match=match):
        parse_json_response(
            httpx.Response(200, content=content, headers=headers),
            context="provider response",
        )
