"""Small shared retry/backoff/circuit-breaker helpers for HTTP adapters."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class CircuitOpenError(RuntimeError):
    """Raised when an adapter poll path is temporarily short-circuited."""


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 0.5
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

    def before_request(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.reset_after_s:
            self._opened_at = None
            self._failures = 0
            return
        raise CircuitOpenError(f"HTTP circuit {self.name!r} is open")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
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

    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_request()
        try:
            response = await client.request(normalized_method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = exc
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                raise
            await sleep_fn(_delay(policy, attempt, random_fn))
            continue

        if response.status_code in policy.transient_statuses:
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                return response
            await response.aclose()
            await sleep_fn(_delay(policy, attempt, random_fn))
            continue

        if breaker is not None:
            breaker.record_success()
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("resilient_request exhausted attempts without a response")


def retry_policy(
    *,
    attempts: int = 3,
    retry_methods: Iterable[str] = IDEMPOTENT_METHODS,
) -> RetryPolicy:
    return RetryPolicy(attempts=attempts, retry_methods=frozenset(m.upper() for m in retry_methods))


def _delay(policy: RetryPolicy, attempt: int, random_fn: Any) -> float:
    base = min(policy.base_delay_s * 2 ** max(attempt - 1, 0), policy.max_delay_s)
    return min(base * (1 + float(random_fn()) * 0.25), policy.max_delay_s)
