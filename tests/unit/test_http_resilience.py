import httpx
import pytest

from apex.adapters.http_resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    resilient_request,
    retry_policy,
)


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
