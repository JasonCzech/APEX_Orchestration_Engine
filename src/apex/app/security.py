"""Lightweight HTTP hardening middleware for the mounted /v1 API."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, MutableSequence
from typing import Any

from apex.auth.service import extract_api_key, hash_api_key
from apex.services.audit import append_audit_event_best_effort, request_audit_event
from apex.settings import RateLimitSettings, SecurityHeadersSettings

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

_HeaderList = MutableSequence[tuple[bytes, bytes]]
_AUDITED_STATUSES = {401, 403, 429}
_PENDING_AUDIT: set[asyncio.Task[None]] = set()


class SecurityHeadersMiddleware:
    """Add defensive browser headers to every HTTP response."""

    def __init__(self, app: ASGIApp, settings: SecurityHeadersSettings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self._settings.enabled:
            await self._app(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                _set_header(headers, b"x-content-type-options", b"nosniff")
                _set_header(headers, b"x-frame-options", b"DENY")
                _set_header(headers, b"referrer-policy", b"no-referrer")
                _set_header(
                    headers,
                    b"permissions-policy",
                    b"geolocation=(), microphone=(), camera=()",
                )
                if self._settings.content_security_policy:
                    _set_header(
                        headers,
                        b"content-security-policy",
                        self._settings.content_security_policy.encode("utf-8"),
                    )
                if self._settings.hsts_max_age_s > 0:
                    value = f"max-age={int(self._settings.hsts_max_age_s)}"
                    if self._settings.hsts_include_subdomains:
                        value += "; includeSubDomains"
                    _set_header(headers, b"strict-transport-security", value.encode("ascii"))
            await send(message)

        await self._app(scope, receive, send_wrapper)


class AuthAuditMiddleware:
    """Best-effort audit logging for authentication/authorization decisions."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        status_code = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            _schedule_audit(scope, 500, reason="unhandled exception")
            raise
        if status_code in _AUDITED_STATUSES:
            _schedule_audit(scope, status_code)


class RateLimitMiddleware:
    """Simple per-key/IP fixed-window limiter for /v1 request bursts.

    This is intentionally small and process-local. It gives local/Helm installs a
    sane default, while production can still enforce stronger distributed limits
    at an ingress or API gateway.
    """

    def __init__(self, app: ASGIApp, settings: RateLimitSettings) -> None:
        self._app = app
        self._settings = settings
        self._buckets: defaultdict[str, deque[float]] = defaultdict(deque)
        self._last_sweep: float = 0.0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or not self._settings.enabled
            or not _is_rate_limited_path(scope, self._settings)
        ):
            await self._app(scope, receive, send)
            return

        retry_after = self._check(scope, now=time.monotonic())
        if retry_after is not None:
            _schedule_audit(scope, 429, reason="rate limit exceeded")
            await _send_rate_limited(send, retry_after)
            return
        await self._app(scope, receive, send)

    def _check(self, scope: Mapping[str, Any], *, now: float) -> int | None:
        limit = max(int(self._settings.requests), 1)
        window = max(float(self._settings.window_s), 1.0)
        self._sweep(now=now, window=window)

        key = _rate_key(scope)
        bucket = self._buckets.get(key)
        if bucket is None:
            max_buckets = max(int(self._settings.max_buckets), 1)
            if len(self._buckets) >= max_buckets:
                return int(window)
            bucket = self._buckets[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(int(window - (now - bucket[0])), 1)
        bucket.append(now)
        return None

    def _sweep(self, *, now: float, window: float) -> None:
        # Evicting idle buckets is O(N); the per-key bucket touched by each request is
        # pruned inline in _check, so this full pass only needs to run periodically to
        # bound memory — at most once per window rather than on every request.
        if now - self._last_sweep < window:
            return
        self._last_sweep = now
        for key, bucket in list(self._buckets.items()):
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]


def _rate_key(scope: Mapping[str, Any]) -> str:
    api_key = extract_api_key(scope.get("headers") or [])
    if api_key:
        return f"key:{hash_api_key(api_key)}"
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return f"ip:{client[0]}"
    return "anonymous"


def _is_rate_limited_path(scope: Mapping[str, Any], settings: RateLimitSettings) -> bool:
    path = str(scope.get("path") or "")
    return any(path.startswith(prefix) for prefix in settings.protected_path_prefixes)


def _schedule_audit(scope: Mapping[str, Any], status_code: int, reason: str | None = None) -> None:
    try:
        task = asyncio.get_running_loop().create_task(
            append_audit_event_best_effort(
                request_audit_event(scope, status_code=status_code, reason=reason)
            )
        )
        _PENDING_AUDIT.add(task)
        task.add_done_callback(_PENDING_AUDIT.discard)
    except Exception:
        return


async def _send_rate_limited(send: Any, retry_after: int) -> None:
    payload = {
        "type": "about:blank",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "Rate limit exceeded",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"retry-after", str(retry_after).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _set_header(headers: _HeaderList, key: bytes, value: bytes) -> None:
    lower = key.lower()
    for index, (existing, _) in enumerate(headers):
        if existing.lower() == lower:
            headers[index] = (key, value)
            return
    headers.append((key, value))
