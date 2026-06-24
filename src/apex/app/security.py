"""Lightweight HTTP hardening middleware for the mounted /v1 API."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, MutableSequence
from typing import Any

from apex.auth.service import extract_api_key, hash_api_key
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
            await send(message)

        await self._app(scope, receive, send_wrapper)


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

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or not self._settings.enabled
            or not str(scope.get("path") or "").startswith("/v1/")
        ):
            await self._app(scope, receive, send)
            return

        retry_after = self._check(scope, now=time.monotonic())
        if retry_after is not None:
            await _send_rate_limited(send, retry_after)
            return
        await self._app(scope, receive, send)

    def _check(self, scope: Mapping[str, Any], *, now: float) -> int | None:
        limit = max(int(self._settings.requests), 1)
        window = max(float(self._settings.window_s), 1.0)
        bucket = self._buckets[_rate_key(scope)]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(int(window - (now - bucket[0])), 1)
        bucket.append(now)
        return None


def _rate_key(scope: Mapping[str, Any]) -> str:
    api_key = extract_api_key(dict(scope.get("headers") or []))
    if api_key:
        return f"key:{hash_api_key(api_key)}"
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return f"ip:{client[0]}"
    return "anonymous"


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
