"""Small shared retry/backoff/circuit-breaker helpers for HTTP adapters."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any

import httpx

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})
STREAM_ERROR_PREVIEW_BYTES = 64 * 1024


class CircuitOpenError(RuntimeError):
    """Raised when an adapter poll path is temporarily short-circuited."""


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 0.5
    # A server-sent Retry-After is honored up to this cap (the local backoff cap
    # max_delay_s is far too small to respect a real throttle signal). Still bounded
    # by total_timeout_s / the per-request deadline.
    retry_after_cap_s: float = 30.0
    total_timeout_s: float | None = 10.0
    transient_statuses: frozenset[int] = TRANSIENT_STATUSES
    retry_methods: frozenset[str] = IDEMPOTENT_METHODS


@dataclass(slots=True)
class CircuitBreaker:
    """A lightweight in-process breaker for hot polling loops."""

    name: str
    failure_threshold: int = 5
    reset_after_s: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None
    _lock: Lock = field(default_factory=Lock, repr=False)

    def before_request(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at < self.reset_after_s:
                raise CircuitOpenError(f"HTTP circuit {self.name!r} is open")
            self._opened_at = None
            self._failures = 0

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()


async def resilient_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    random_fn: Any = random.random,
    sleep_fn: Any = asyncio.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Run an HTTP request with bounded retries for transient failures.

    Callers still own domain-specific status mapping. This helper only retries
    transport errors and retryable HTTP statuses, then returns the final
    response for the adapter to interpret.
    """

    policy = retry or RetryPolicy()
    normalized_method = method.upper()
    max_attempts = max(policy.attempts, 1)
    can_retry = normalized_method in policy.retry_methods
    last_error: httpx.HTTPError | None = None
    deadline = (
        time.monotonic() + max(policy.total_timeout_s, 0.0)
        if policy.total_timeout_s is not None
        else None
    )

    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_request()
        try:
            response = await _request_with_deadline(
                client, normalized_method, url, deadline=deadline, **kwargs
            )
        except TimeoutError as exc:
            timeout = httpx.TimeoutException("total retry timeout exceeded")
            last_error = timeout
            if breaker is not None:
                breaker.record_failure()
            raise timeout from exc
        except httpx.HTTPError as exc:
            last_error = exc
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                raise
            await _sleep_with_deadline(_delay(policy, attempt, random_fn), deadline, sleep_fn)
            continue

        if response.status_code in policy.transient_statuses:
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                return response
            delay = _retry_delay(response, policy, attempt, random_fn)
            await response.aclose()
            await _sleep_with_deadline(delay, deadline, sleep_fn)
            continue

        if breaker is not None:
            breaker.record_success()
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("resilient_request exhausted attempts without a response")


async def resilient_stream_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    random_fn: Any = random.random,
    sleep_fn: Any = asyncio.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Open a response stream with the same bounded resilience as normal requests.

    The returned response remains open and must be closed by the caller. Responses
    discarded for a retry are always closed first, so a transient status cannot
    leak a connection from the client's pool. Only response headers are received
    before this helper returns; the successful body is never buffered in memory.
    """

    policy = retry or RetryPolicy()
    normalized_method = method.upper()
    max_attempts = max(policy.attempts, 1)
    can_retry = normalized_method in policy.retry_methods
    last_error: httpx.HTTPError | None = None
    deadline = (
        time.monotonic() + max(policy.total_timeout_s, 0.0)
        if policy.total_timeout_s is not None
        else None
    )

    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_request()
        try:
            response = await _stream_request_with_deadline(
                client, normalized_method, url, deadline=deadline, **kwargs
            )
        except TimeoutError as exc:
            timeout = httpx.TimeoutException("total retry timeout exceeded")
            last_error = timeout
            if breaker is not None:
                breaker.record_failure()
            raise timeout from exc
        except httpx.HTTPError as exc:
            last_error = exc
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                raise
            await _sleep_with_deadline(_delay(policy, attempt, random_fn), deadline, sleep_fn)
            continue

        if response.status_code in policy.transient_statuses:
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                return response
            delay = _retry_delay(response, policy, attempt, random_fn)
            await response.aclose()
            await _sleep_with_deadline(delay, deadline, sleep_fn)
            continue

        if breaker is not None:
            breaker.record_success()
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("resilient_stream_request exhausted attempts without a response")


async def read_stream_error_preview(
    response: httpx.Response, *, max_bytes: int = STREAM_ERROR_PREVIEW_BYTES
) -> bytes:
    """Read at most ``max_bytes`` from an error stream for diagnostics.

    Callers must close the response in ``finally``. Stopping at the preview cap
    avoids buffering or decompressing an attacker-controlled error body merely
    to build an exception message.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    preview = bytearray()
    async for chunk in response.aiter_raw():
        remaining = max_bytes - len(preview)
        if remaining <= 0:
            break
        preview.extend(chunk[:remaining])
        if len(preview) >= max_bytes:
            break
    return bytes(preview)


def retry_policy(
    *,
    attempts: int = 3,
    retry_methods: Iterable[str] = IDEMPOTENT_METHODS,
    total_timeout_s: float | None = 10.0,
) -> RetryPolicy:
    return RetryPolicy(
        attempts=attempts,
        retry_methods=frozenset(m.upper() for m in retry_methods),
        total_timeout_s=total_timeout_s,
    )


def _delay(policy: RetryPolicy, attempt: int, random_fn: Any) -> float:
    base = min(policy.base_delay_s * 2 ** max(attempt - 1, 0), policy.max_delay_s)
    return min(base * (1 + float(random_fn()) * 0.25), policy.max_delay_s)


async def _request_with_deadline(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline: float | None,
    **kwargs: Any,
) -> httpx.Response:
    if deadline is None:
        return await client.request(method, url, **kwargs)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(client.request(method, url, **kwargs), timeout=remaining)


async def _stream_request_with_deadline(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline: float | None,
    **kwargs: Any,
) -> httpx.Response:
    request = client.build_request(method, url, **kwargs)
    if deadline is None:
        return await client.send(request, stream=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(client.send(request, stream=True), timeout=remaining)


async def _sleep_with_deadline(delay: float, deadline: float | None, sleep_fn: Any) -> None:
    if deadline is None:
        await sleep_fn(delay)
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.TimeoutException("total retry timeout exceeded")
    await sleep_fn(min(delay, remaining))


def _retry_delay(
    response: httpx.Response,
    policy: RetryPolicy,
    attempt: int,
    random_fn: Any,
) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after is None:
        return _delay(policy, attempt, random_fn)
    cap = max(policy.retry_after_cap_s, policy.max_delay_s)
    try:
        return min(max(float(retry_after), 0.0), cap)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return _delay(policy, attempt, random_fn)
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return min(max((retry_at - datetime.now(UTC)).total_seconds(), 0.0), cap)
