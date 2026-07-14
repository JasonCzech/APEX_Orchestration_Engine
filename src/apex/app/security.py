"""Lightweight HTTP hardening middleware for the mounted /v1 API."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableSequence
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
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
_IPNetwork = IPv4Network | IPv6Network


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
    """Best-effort audit logging plus process-local 401 lockout."""

    def __init__(self, app: ASGIApp, settings: RateLimitSettings | None = None) -> None:
        self._app = app
        self._settings = settings or RateLimitSettings()
        self._trusted_proxy_networks = _proxy_networks(self._settings)
        self._failure_buckets: dict[str, deque[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._last_sweep: float = 0.0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if self._settings.enabled and _is_rate_limited_path(scope, self._settings):
            retry_after = self._auth_lockout_retry_after(scope, now=time.monotonic())
            if retry_after is not None:
                _schedule_audit(scope, 429, reason="authentication lockout")
                await _send_rate_limited(send, retry_after)
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
        if self._settings.enabled and _is_rate_limited_path(scope, self._settings):
            self._record_auth_result(scope, status_code=status_code, now=time.monotonic())

    def _auth_lockout_retry_after(self, scope: Mapping[str, Any], *, now: float) -> int | None:
        self._sweep_auth_state(now=now)
        retry_after = 0
        for key in _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks):
            until = self._lockouts.get(key)
            if until is None:
                continue
            if now >= until:
                self._lockouts.pop(key, None)
                self._failure_buckets.pop(key, None)
                continue
            retry_after = max(retry_after, max(int(until - now), 1))
        return retry_after or None

    def _record_auth_result(
        self, scope: Mapping[str, Any], *, status_code: int, now: float
    ) -> None:
        self._sweep_auth_state(now=now)
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        if status_code != 401:
            # Reset only the credential. A source may interleave one valid key with
            # rotating invalid keys; clearing its aggregate bucket here would make
            # that valid request an authentication-lockout bypass. Unmatched routes
            # and other unauthenticated non-401 responses are not proof that the
            # credential succeeded and must not clear even its own bucket.
            state = scope.get("state")
            authenticated = isinstance(state, Mapping) and state.get("identity") is not None
            if authenticated:
                for key in keys[1:]:
                    self._failure_buckets.pop(key, None)
                    self._lockouts.pop(key, None)
            return
        limit = max(int(self._settings.auth_failures), 0)
        if limit <= 0:
            return
        window = max(float(self._settings.auth_failure_window_s), 1.0)
        lockout = max(float(self._settings.auth_lockout_s), 1.0)
        max_buckets = max(int(self._settings.max_buckets), 1)
        for key in keys:
            bucket = self._failure_buckets.get(key)
            if bucket is None:
                # The source-IP key is first, so even when the cap is reached a
                # rotating-credential attack remains covered by its source bucket.
                if len(self._failure_buckets) >= max_buckets:
                    continue
                bucket = deque()
                self._failure_buckets[key] = bucket
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            bucket.append(now)
            if len(bucket) >= limit:
                self._lockouts[key] = now + lockout

    def _sweep_auth_state(self, *, now: float) -> None:
        window = max(float(self._settings.auth_failure_window_s), 1.0)
        if now - self._last_sweep < window:
            return
        self._last_sweep = now
        for key, bucket in list(self._failure_buckets.items()):
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            if not bucket:
                self._failure_buckets.pop(key, None)
        for key, until in list(self._lockouts.items()):
            if now >= until:
                self._lockouts.pop(key, None)


class RateLimitMiddleware:
    """Simple per-key/IP fixed-window limiter for /v1 request bursts.

    This is intentionally small and process-local. It gives local/Helm installs a
    sane default, while production can still enforce stronger distributed limits
    at an ingress or API gateway.
    """

    def __init__(self, app: ASGIApp, settings: RateLimitSettings) -> None:
        self._app = app
        self._settings = settings
        self._trusted_proxy_networks = _proxy_networks(settings)
        self._buckets: dict[str, deque[float]] = {}
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

        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        # Preserve per-credential fairness while placing a finite aggregate ceiling
        # on one source rotating arbitrary API-key values. Authentication failures
        # have a much tighter IP-based lockout in AuthAuditMiddleware.
        ip_limit = max(limit, int(self._settings.auth_failures), 1)
        retry_after = self._check_bucket(
            keys[0], now=now, limit=ip_limit, window=window, required=True
        )
        if retry_after is not None:
            return retry_after
        if len(keys) == 1:
            return None
        return self._check_bucket(keys[1], now=now, limit=limit, window=window, required=False)

    def _check_bucket(
        self,
        key: str,
        *,
        now: float,
        limit: int,
        window: float,
        required: bool,
    ) -> int | None:
        bucket = self._buckets.get(key)
        if bucket is None:
            max_buckets = max(int(self._settings.max_buckets), 1)
            if len(self._buckets) >= max_buckets:
                # Source buckets are mandatory and fail closed. Per-credential
                # buckets may be omitted at the cap because the source bucket still
                # constrains the request and avoids making the first request fail
                # when max_buckets is deliberately configured to one.
                return int(window) if required else None
            bucket = deque()
            self._buckets[key] = bucket
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
    """Return the most specific rate key (kept for audit/helper compatibility)."""

    keys = _rate_keys(scope)
    return keys[-1]


def _rate_keys(
    scope: Mapping[str, Any],
    *,
    trusted_proxy_networks: tuple[_IPNetwork, ...] = (),
) -> tuple[str, ...]:
    """Return source first, then credential, so rotation cannot evade source limits."""

    source = _source_rate_key(scope, trusted_proxy_networks)
    api_key = extract_api_key(scope.get("headers") or [])
    if api_key:
        return source, f"key:{hash_api_key(api_key)}"
    return (source,)


def _proxy_networks(settings: RateLimitSettings) -> tuple[_IPNetwork, ...]:
    return tuple(ip_network(value, strict=False) for value in settings.trusted_proxy_cidrs)


def _source_rate_key(
    scope: Mapping[str, Any], trusted_proxy_networks: tuple[_IPNetwork, ...]
) -> str:
    """Resolve the client IP through only an explicitly trusted proxy chain."""

    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return "anonymous"
    peer_text = str(client[0])
    try:
        peer = ip_address(peer_text)
    except ValueError:
        return f"ip:{peer_text}"
    if not trusted_proxy_networks or not _in_networks(peer, trusted_proxy_networks):
        return f"ip:{peer.compressed}"

    forwarded: list[str] = []
    for name, value in scope.get("headers") or []:
        raw_name = name.decode("latin-1") if isinstance(name, bytes) else str(name)
        if raw_name.casefold() != "x-forwarded-for":
            continue
        raw_value = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        forwarded.extend(part.strip() for part in raw_value.split(","))

    # Walk from the immediate peer toward the caller. Trusted proxies are
    # skipped; the first untrusted address is the source that cannot be forged
    # by anything to its left. A malformed hop fails closed to the direct peer.
    for raw_hop in reversed(forwarded):
        try:
            hop = ip_address(raw_hop)
        except ValueError:
            return f"ip:{peer.compressed}"
        if not _in_networks(hop, trusted_proxy_networks):
            return f"ip:{hop.compressed}"
    return f"ip:{peer.compressed}"


def _in_networks(address: Any, networks: tuple[_IPNetwork, ...]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


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
