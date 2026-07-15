"""Lightweight HTTP hardening middleware for the mounted /v1 API."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableSequence
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, unquote

from apex.app.distributed_limits import (
    DistributedLimitBackend,
    LimitBackendUnavailable,
    StreamLease,
)
from apex.auth.service import extract_api_key, hash_api_key
from apex.domain.diagnostics import bounded_diagnostic, is_credential_field
from apex.domain.input_limits import validate_json_object
from apex.services.audit import append_audit_event_best_effort, request_audit_event
from apex.services.langgraph_client import is_trusted_loopback
from apex.settings import RateLimitSettings, RequestBodySettings, SecurityHeadersSettings

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
_MAX_PENDING_AUDIT = 1024
_MAX_LANGGRAPH_READ_PAGE_SIZE = 100
_MAX_LANGGRAPH_HISTORY_PAGE_SIZE = 5
_MAX_LANGGRAPH_READ_OFFSET = 10_000
_MAX_LANGGRAPH_THREAD_IDS = 100
_MAX_LANGGRAPH_ASSISTANT_PAGE_SIZE = 5
_MAX_LANGGRAPH_ASSISTANT_VERSIONS_PAGE_SIZE = 2
_MAX_LANGGRAPH_FILTER_BYTES = 100_000
_MAX_LANGGRAPH_FILTER_NODES = 2_000
_MAX_LANGGRAPH_IDENTIFIER_CHARS = 255
_MAX_LANGGRAPH_QUERY_CHARS = 20_000
_MAX_LANGGRAPH_QUERY_FIELDS = 64
_MAX_ASSISTANT_XRAY_DEPTH = 3
_MAX_LANGGRAPH_DESCRIPTION_CHARS = 20_000
_MAX_JSON_BODY_DEPTH = 64
_MAX_JSON_BODY_TOKENS = 20_000
_MAX_JSON_SCALAR_BYTES = 512_000
_MAX_NATIVE_PROJECTION_JSON_BYTES = 5_000_000
_MAX_NATIVE_PROJECTION_JSON_NODES = 20_000
_MAX_PUBLIC_STREAM_EVENT_BYTES = 512_000
_LANGGRAPH_RUN_STATUSES = {
    "pending",
    "running",
    "error",
    "success",
    "timeout",
    "interrupted",
}
_IPNetwork = IPv4Network | IPv6Network


class _RequestBodyTooLarge(Exception):
    """Internal control flow used to stop an over-limit receive stream."""


class _RequestBodyTimedOut(Exception):
    """Internal control flow for a request that misses its whole-body deadline."""


class _RequestInputRejected(Exception):
    """Internal control flow for a bounded request rejected before route effects."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class _JsonNestingPrecheck:
    """Incrementally bound JSON depth/tokens/scalars before recursive decoding."""

    def __init__(
        self,
        max_depth: int,
        *,
        max_tokens: int = _MAX_JSON_BODY_TOKENS,
        max_scalar_bytes: int = _MAX_JSON_SCALAR_BYTES,
    ) -> None:
        self._max_depth = max_depth
        self._max_tokens = max_tokens
        self._max_scalar_bytes = max_scalar_bytes
        self._depth = 0
        self._tokens = 0
        self._in_string = False
        self._in_unquoted_scalar = False
        self._escaped = False
        self._scalar_bytes = 0

    def _start_token(self) -> str | None:
        self._tokens += 1
        if self._tokens > self._max_tokens:
            return f"JSON body must not exceed {self._max_tokens} tokens"
        return None

    def _add_scalar_byte(self) -> str | None:
        self._scalar_bytes += 1
        if self._scalar_bytes > self._max_scalar_bytes:
            return f"JSON scalar must not exceed {self._max_scalar_bytes} bytes"
        return None

    def feed(self, chunk: bytes) -> str | None:
        # JSON on these HTTP surfaces is UTF-8. UTF-16/32 encodings place NUL
        # bytes between ASCII delimiters, which would corrupt this byte-level
        # quote/escape state machine and could hide excessive nesting. A literal
        # NUL is invalid in UTF-8 JSON; the valid escaped spelling (\\u0000)
        # contains no NUL byte and remains accepted.
        if b"\x00" in chunk:
            return "JSON body must be UTF-8 encoded"
        for value in chunk:
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif value == 0x5C:  # backslash
                    self._escaped = True
                elif value == 0x22:  # quote
                    self._in_string = False
                    self._scalar_bytes = 0
                    continue
                if error := self._add_scalar_byte():
                    return error
                continue
            if value == 0x22:
                if error := self._start_token():
                    return error
                self._in_string = True
                self._in_unquoted_scalar = False
                self._scalar_bytes = 0
            elif value in {0x5B, 0x7B}:  # [ {
                if error := self._start_token():
                    return error
                self._depth += 1
                self._in_unquoted_scalar = False
                self._scalar_bytes = 0
                if self._depth > self._max_depth:
                    return f"JSON body nesting must not exceed {self._max_depth} levels"
            elif value in {0x5D, 0x7D}:  # ] }
                self._depth = max(0, self._depth - 1)
                self._in_unquoted_scalar = False
                self._scalar_bytes = 0
            elif value in {0x2C, 0x3A, 0x20, 0x09, 0x0A, 0x0D}:  # , : whitespace
                self._in_unquoted_scalar = False
                self._scalar_bytes = 0
            else:
                if not self._in_unquoted_scalar:
                    if error := self._start_token():
                        return error
                    self._in_unquoted_scalar = True
                    self._scalar_bytes = 0
                if error := self._add_scalar_byte():
                    return error
        return None


class _StreamAdmissionRejected(Exception):
    """A fully received, authenticated stream request exceeded concurrency caps."""


class _StreamAdmissionUnavailable(Exception):
    """The shared backend could not admit an authenticated stream request."""


class _StreamRequestDisconnected(Exception):
    """The client disconnected before its stream request body completed."""


_STREAM_ADMISSION_SCOPE_KEY = "_apex_stream_admission"


class _DeferredStreamAdmission:
    """Acquire an established-stream permit only after body and authentication."""

    def __init__(
        self,
        acquire: Callable[[], Awaitable[None]],
        *,
        body_complete: bool,
    ) -> None:
        self._acquire = acquire
        self._body_complete = body_complete
        self._authenticated = False
        self._attempted = False
        self._admitted = False
        self._error: _StreamAdmissionRejected | _StreamAdmissionUnavailable | None = None
        self._lock = asyncio.Lock()

    async def mark_authenticated(self) -> None:
        async with self._lock:
            self._authenticated = True
            await self._admit_if_ready()

    async def receive(self, receive: Any) -> dict[str, Any]:
        message = await receive()
        if message.get("type") == "http.request" and not message.get("more_body", False):
            async with self._lock:
                self._body_complete = True
                await self._admit_if_ready()
        return message

    async def ensure_before_success(self, receive: Any) -> None:
        # A successful response is a safe fallback authentication signal for
        # non-LangGraph test/custom apps. Production LangGraph authentication
        # calls mark_stream_request_authenticated() before authorization effects.
        await self.mark_authenticated()
        while True:
            async with self._lock:
                if self._body_complete:
                    return
            message = await self.receive(receive)
            if message.get("type") == "http.disconnect":
                raise _StreamRequestDisconnected

    async def _admit_if_ready(self) -> None:
        if not self._authenticated or not self._body_complete or self._admitted:
            return
        if self._error is not None:
            raise self._error
        if self._attempted:
            return
        self._attempted = True
        try:
            await self._acquire()
        except (_StreamAdmissionRejected, _StreamAdmissionUnavailable) as exc:
            self._error = exc
            raise
        self._admitted = True


async def mark_stream_request_authenticated(scope: Mapping[str, Any]) -> None:
    """Complete deferred stream admission after LangGraph authentication."""

    admission = scope.get(_STREAM_ADMISSION_SCOPE_KEY)
    if isinstance(admission, _DeferredStreamAdmission):
        await admission.mark_authenticated()


class RequestBodyLimitMiddleware:
    """Reject oversized or stalled bodies before parsing/authentication."""

    def __init__(self, app: ASGIApp, settings: RequestBodySettings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if _is_disabled_v2_runtime_surface(scope):
            await _reject_disabled_runtime_surface(scope, send)
            return
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        if error := _credential_header_error(scope):
            await _send_bad_request(send, error)
            return
        if detail := _blocked_public_native_projection(scope):
            await _send_not_found(send, detail)
            return
        if _is_native_cron_path(scope):
            await _send_not_found(send, "Native scheduled runs are disabled")
            return
        if detail := _disabled_native_thread_primitive(scope):
            await _send_not_found(send, detail)
            return
        if _request_target_contains_nul(scope):
            await _send_bad_request(send, "Request target must not contain U+0000")
            return
        if error := _langsmith_baggage_error(scope):
            await _send_bad_request(send, error)
            return
        if error := _direct_langgraph_query_limit_error(scope):
            await _send_unprocessable_entity(send, error)
            return

        limit = self._limit_for(scope)
        content_length = _content_length(scope.get("headers") or [])
        if content_length is not None and content_length > limit:
            await _send_payload_too_large(send, limit)
            return

        method = str(scope.get("method") or "").upper()
        request_path = str(scope.get("path") or "")
        consumed = 0
        response_started = False
        public_custom_stream = (
            _is_run_item_stream_path(request_path) or _is_run_create_stream_path(request_path)
        ) and not _scope_is_trusted_loopback(scope)
        public_json_projection = _public_native_json_projection_kind(
            method,
            request_path,
            trusted_loopback=_scope_is_trusted_loopback(scope),
        )
        public_thread_update_projection = (
            method == "PATCH"
            and _is_thread_item_path(request_path)
            and not _scope_is_trusted_loopback(scope)
        )
        public_native_request = (
            request_path != "/ready"
            and not request_path.startswith("/v1/")
            and not _scope_is_trusted_loopback(scope)
        )
        stream_buffer = bytearray()
        projection_response_buffer = bytearray()
        projection_response_oversized = False
        native_error_status: int | None = None
        discard_oversized_stream_event = False
        body_complete = content_length == 0
        enforce_deadline = method in {"POST", "PUT", "PATCH", "DELETE"} and not body_complete
        deadline = asyncio.get_running_loop().time() + self._settings.timeout_s
        inspect_direct_read_body = _direct_langgraph_read_body_path(scope)
        direct_read_body = bytearray()
        json_precheck = (
            _JsonNestingPrecheck(_MAX_JSON_BODY_DEPTH) if _is_json_mutation_request(scope) else None
        )

        async def capped_receive() -> dict[str, Any]:
            nonlocal body_complete, consumed
            if enforce_deadline and not body_complete:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise _RequestBodyTimedOut
                try:
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError as exc:
                    raise _RequestBodyTimedOut from exc
            else:
                message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                consumed += len(body) if isinstance(body, bytes) else 0
                if consumed > limit:
                    raise _RequestBodyTooLarge
                if json_precheck is not None and isinstance(body, bytes):
                    if error := json_precheck.feed(body):
                        raise _RequestInputRejected(error)
                if inspect_direct_read_body and isinstance(body, bytes):
                    direct_read_body.extend(body)
                if not message.get("more_body", False):
                    body_complete = True
                    if inspect_direct_read_body:
                        error = _direct_langgraph_body_limit_error(
                            inspect_direct_read_body,
                            bytes(direct_read_body),
                            trusted_loopback=_scope_is_trusted_loopback(scope),
                        )
                        if error is not None:
                            raise _RequestInputRejected(error)
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal projection_response_oversized, discard_oversized_stream_event
            nonlocal native_error_status, response_started
            if message.get("type") == "http.response.start":
                response_started = True
                status = int(message.get("status") or 500)
                if public_native_request and status >= 400:
                    native_error_status = status
                    message = _safe_native_problem_start(message)
                elif public_thread_update_projection and 200 <= status < 300:
                    message = _safe_empty_projection_start(message)
                elif public_json_projection is not None and 200 <= status < 300:
                    message = _safe_projected_json_start(
                        message,
                        preserve_run_location=public_json_projection == "run",
                    )
                elif public_custom_stream and 200 <= status < 300:
                    message = _safe_public_stream_start(message)
                await send(message)
                return
            if native_error_status is not None and message.get("type") == "http.response.body":
                if message.get("more_body", False):
                    return
                await send(
                    {
                        **message,
                        "body": _safe_native_problem_body(native_error_status),
                        "more_body": False,
                    }
                )
                return
            if public_thread_update_projection and message.get("type") == "http.response.body":
                if message.get("more_body", False):
                    return
                await send({**message, "body": b"", "more_body": False})
                return
            if public_json_projection is not None and message.get("type") == "http.response.body":
                body = message.get("body", b"")
                if isinstance(body, bytes) and not projection_response_oversized:
                    projection_response_buffer.extend(body)
                    if len(projection_response_buffer) > _MAX_NATIVE_PROJECTION_JSON_BYTES:
                        projection_response_buffer.clear()
                        projection_response_oversized = True
                if message.get("more_body", False):
                    return
                projected = (
                    _empty_native_projection(public_json_projection)
                    if projection_response_oversized
                    else _safe_native_projection_response(
                        public_json_projection,
                        bytes(projection_response_buffer),
                    )
                )
                await send({**message, "body": projected, "more_body": False})
                return
            if public_custom_stream and message.get("type") == "http.response.body":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    stream_buffer.extend(body)
                    sanitized = bytearray()
                    while True:
                        boundary = _sse_event_boundary(stream_buffer)
                        if boundary is None:
                            if len(stream_buffer) > _MAX_PUBLIC_STREAM_EVENT_BYTES:
                                stream_buffer.clear()
                                discard_oversized_stream_event = True
                                sanitized.extend(_safe_stream_error_event())
                            break
                        end, separator_size = boundary
                        event = bytes(stream_buffer[:end])
                        del stream_buffer[: end + separator_size]
                        if discard_oversized_stream_event:
                            discard_oversized_stream_event = False
                            continue
                        sanitized.extend(_sanitize_public_sse_event(event))
                    if not message.get("more_body", False) and stream_buffer:
                        if not discard_oversized_stream_event:
                            sanitized.extend(_sanitize_public_sse_event(bytes(stream_buffer)))
                        stream_buffer.clear()
                        discard_oversized_stream_event = False
                    message = {**message, "body": bytes(sanitized)}
            if public_native_request and message.get("type") == "http.response.trailers":
                # Native runtimes may opt into ASGI trailers independently of
                # their ordinary response headers. They are not part of any
                # APEX public contract and could otherwise bypass projection.
                return
            await send(message)

        try:
            await self._app(scope, capped_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await _send_payload_too_large(send, limit)
        except _RequestBodyTimedOut:
            if response_started:
                raise
            await _send_request_timeout(send, self._settings.timeout_s)
        except _RequestInputRejected as exc:
            if response_started:
                raise
            await _send_unprocessable_entity(send, exc.detail)

    def _limit_for(self, scope: Mapping[str, Any]) -> int:
        if (
            str(scope.get("method") or "").upper() == "POST"
            and str(scope.get("path") or "") == "/v1/documents"
        ):
            return self._settings.document_upload_max_bytes
        return self._settings.max_bytes


class SecurityHeadersMiddleware:
    """Add defensive browser and authenticated-response headers."""

    def __init__(self, app: ASGIApp, settings: SecurityHeadersSettings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        credentialed_request = _scope_has_api_credentials(scope)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if self._settings.enabled:
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
                if credentialed_request:
                    # Every authenticated APEX/native LangGraph response is scoped
                    # to the supplied principal. Override route/provider caching,
                    # including on streaming response starts, so a shared browser,
                    # proxy, or CDN can never replay one principal's data to another.
                    _set_header(headers, b"cache-control", b"private, no-store")
                    _set_header(headers, b"pragma", b"no-cache")
                    _append_vary(headers, "Authorization", "X-API-Key")
            await send(message)

        await self._app(scope, receive, send_wrapper)


class AuthAuditMiddleware:
    """Best-effort audit logging plus local or Redis-shared 401 lockout."""

    def __init__(
        self,
        app: ASGIApp,
        settings: RateLimitSettings | None = None,
        backend: DistributedLimitBackend | None = None,
    ) -> None:
        self._app = app
        self._settings = settings or RateLimitSettings()
        if self._settings.backend == "redis" and backend is None:
            raise ValueError("Redis auth lockouts require a distributed limit backend")
        self._backend = backend
        self._trusted_proxy_networks = _proxy_networks(self._settings)
        self._failure_buckets: dict[str, deque[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._last_sweep: float = 0.0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        protected = self._settings.enabled and _is_rate_limited_path(scope, self._settings)
        lockout_enforced = protected and not _scope_is_trusted_loopback(scope)
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        if lockout_enforced:
            try:
                retry_after = (
                    await self._backend.auth_retry_after(keys)
                    if self._backend is not None
                    else self._auth_lockout_retry_after(scope, now=time.monotonic())
                )
            except LimitBackendUnavailable:
                _schedule_audit(scope, 503, reason="distributed auth lockout unavailable")
                await _send_limit_backend_unavailable(send)
                return
            if retry_after is not None:
                _schedule_audit(scope, 429, reason="authentication lockout")
                await _send_rate_limited(send, retry_after)
                return
        status_code = 500
        backend_failed = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal backend_failed, status_code
            if backend_failed:
                return
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                if lockout_enforced and status_code == 401 and self._backend is not None:
                    try:
                        await self._backend.record_auth_failure(
                            keys,
                            limit=self._settings.auth_failures,
                            window_s=self._settings.auth_failure_window_s,
                            lockout_s=self._settings.auth_lockout_s,
                        )
                    except LimitBackendUnavailable:
                        backend_failed = True
                        status_code = 503
                        _schedule_audit(
                            scope,
                            503,
                            reason="distributed auth lockout unavailable",
                        )
                        await _send_limit_backend_unavailable(send)
                        return
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            _schedule_audit(scope, 500, reason="unhandled exception")
            raise
        if status_code in _AUDITED_STATUSES:
            _schedule_audit(scope, status_code)
        if lockout_enforced:
            if self._backend is None:
                self._record_auth_result(scope, status_code=status_code, now=time.monotonic())
            elif status_code != 401 and not backend_failed:
                state = scope.get("state")
                authenticated = isinstance(state, Mapping) and state.get("identity") is not None
                if authenticated:
                    try:
                        await self._backend.clear_auth(keys[1:])
                    except LimitBackendUnavailable:
                        # The preflight was authoritative. A failed success-reset is
                        # conservative: stale credential failures expire in Redis.
                        pass

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
    """Per-source/credential limits, Redis-shared in locked/HA deployments."""

    def __init__(
        self,
        app: ASGIApp,
        settings: RateLimitSettings,
        backend: DistributedLimitBackend | None = None,
    ) -> None:
        self._app = app
        self._settings = settings
        if settings.backend == "redis" and backend is None:
            raise ValueError("Redis request limits require a distributed limit backend")
        self._backend = backend
        self._trusted_proxy_networks = _proxy_networks(settings)
        self._buckets: dict[str, deque[float]] = {}
        self._last_sweep: float = 0.0
        self._run_create_buckets: dict[str, deque[float]] = {}
        self._run_create_last_sweep: float = 0.0
        self._stream_lock = Lock()
        self._stream_global = 0
        self._stream_counts: dict[str, int] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if _is_disabled_v2_runtime_surface(scope):
            await _reject_disabled_runtime_surface(scope, send)
            return
        if scope["type"] == "http" and _is_disabled_thread_stream(scope):
            await _send_not_found(send, "Direct thread event streams are disabled")
            return
        protected = (
            scope["type"] == "http"
            and self._settings.enabled
            and _is_rate_limited_path(scope, self._settings)
        )
        trusted_loopback = protected and _scope_is_trusted_loopback(scope)
        if protected and not trusted_loopback:
            try:
                if _is_run_creation_request(scope):
                    retry_after = await self._check_run_create_shared(scope)
                    if retry_after is not None:
                        _schedule_audit(
                            scope,
                            429,
                            reason="run creation rate limit exceeded",
                        )
                        await _send_rate_limited(send, retry_after)
                        return
                retry_after = await self._check_shared(scope)
            except LimitBackendUnavailable:
                _schedule_audit(scope, 503, reason="distributed rate limit unavailable")
                await _send_limit_backend_unavailable(send)
                return
            if retry_after is not None:
                _schedule_audit(scope, 429, reason="rate limit exceeded")
                await _send_rate_limited(send, retry_after)
                return

        if (
            protected
            and not trusted_loopback
            and scope["type"] == "http"
            and _is_long_lived_run_request(scope)
        ):
            await self._call_with_deferred_stream_admission(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _check_shared(self, scope: Mapping[str, Any]) -> int | None:
        if self._backend is None:
            return self._check(scope, now=time.monotonic())
        limit = max(int(self._settings.requests), 1)
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        keyed_limits = [(keys[0], max(limit, int(self._settings.auth_failures), 1))]
        if len(keys) > 1:
            keyed_limits.append((keys[1], limit))
        return await self._backend.check_window(
            "request",
            tuple(keyed_limits),
            window_s=self._settings.window_s,
        )

    async def _check_run_create_shared(self, scope: Mapping[str, Any]) -> int | None:
        if self._backend is None:
            return self._check_run_create(scope, now=time.monotonic())
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        limit = max(int(self._settings.run_create_requests), 1)
        return await self._backend.check_window(
            "run-create",
            tuple((key, limit) for key in keys),
            window_s=self._settings.run_create_window_s,
        )

    async def _call_with_deferred_stream_admission(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        backend = self._backend
        lease: StreamLease | None = None
        local_acquired = False
        renew_task: asyncio.Task[None] | None = None
        renewal_error: LimitBackendUnavailable | None = None
        response_started = False
        app_task: asyncio.Task[None] | None = None

        async def acquire() -> None:
            nonlocal lease, local_acquired, renew_task
            try:
                if backend is None:
                    local_acquired = self._acquire_stream(keys)
                    if not local_acquired:
                        raise _StreamAdmissionRejected
                else:
                    lease = await backend.acquire_stream(
                        keys,
                        global_limit=self._settings.sse_global_concurrency,
                        source_limit=self._settings.sse_source_concurrency,
                        credential_limit=self._settings.sse_credential_concurrency,
                        lease_ttl_s=self._settings.sse_lease_ttl_s,
                    )
                    if lease is None:
                        raise _StreamAdmissionRejected
                    assert app_task is not None
                    renew_task = asyncio.create_task(renew(lease, app_task))
            except LimitBackendUnavailable as exc:
                raise _StreamAdmissionUnavailable from exc

        async def renew(current_lease: StreamLease, current_app: asyncio.Task[None]) -> None:
            nonlocal renewal_error
            assert backend is not None
            interval = max(self._settings.sse_lease_ttl_s / 3, 1.0)
            try:
                while True:
                    await asyncio.sleep(interval)
                    if not await backend.renew_stream(
                        current_lease,
                        lease_ttl_s=self._settings.sse_lease_ttl_s,
                    ):
                        raise LimitBackendUnavailable("distributed SSE lease was lost")
            except asyncio.CancelledError:
                raise
            except LimitBackendUnavailable as exc:
                renewal_error = exc
                current_app.cancel()

        method = str(scope.get("method") or "").upper()
        body_complete = method != "POST" or _content_length(scope.get("headers") or []) == 0
        admission = _DeferredStreamAdmission(acquire, body_complete=body_complete)
        previous_admission = scope.get(_STREAM_ADMISSION_SCOPE_KEY)
        scope[_STREAM_ADMISSION_SCOPE_KEY] = admission

        async def tracked_receive() -> dict[str, Any]:
            return await admission.receive(receive)

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                if 200 <= status_code < 300:
                    await admission.ensure_before_success(receive)
                response_started = True
            await send(message)

        async def run_app() -> None:
            await self._app(scope, tracked_receive, tracked_send)

        app_task = asyncio.create_task(run_app())
        try:
            try:
                await app_task
            except _StreamAdmissionRejected:
                if response_started:
                    raise
                _schedule_audit(scope, 429, reason="SSE concurrency limit exceeded")
                await _send_rate_limited(send, 1)
            except _StreamAdmissionUnavailable:
                if response_started:
                    raise
                _schedule_audit(scope, 503, reason="distributed SSE admission unavailable")
                await _send_limit_backend_unavailable(send)
            except _StreamRequestDisconnected:
                return
            except asyncio.CancelledError:
                if renewal_error is None:
                    raise
                if response_started:
                    raise renewal_error from None
                _schedule_audit(scope, 503, reason="distributed SSE lease lost")
                await _send_limit_backend_unavailable(send)
        finally:
            if renew_task is not None and not renew_task.done():
                renew_task.cancel()
            if renew_task is not None:
                await asyncio.gather(renew_task, return_exceptions=True)
            if lease is not None and backend is not None:
                try:
                    await backend.release_stream(lease)
                except LimitBackendUnavailable:
                    # The short lease expires after a pod/backend failure.
                    pass
            elif local_acquired:
                self._release_stream(keys)
            if previous_admission is None:
                scope.pop(_STREAM_ADMISSION_SCOPE_KEY, None)
            else:
                scope[_STREAM_ADMISSION_SCOPE_KEY] = previous_admission

    def _acquire_stream(self, keys: tuple[str, ...]) -> bool:
        source = keys[0]
        credential = keys[1] if len(keys) > 1 else None
        with self._stream_lock:
            if self._stream_global >= self._settings.sse_global_concurrency:
                return False
            if self._stream_counts.get(source, 0) >= self._settings.sse_source_concurrency:
                return False
            if credential is not None and (
                self._stream_counts.get(credential, 0) >= self._settings.sse_credential_concurrency
            ):
                return False
            self._stream_global += 1
            self._stream_counts[source] = self._stream_counts.get(source, 0) + 1
            if credential is not None:
                self._stream_counts[credential] = self._stream_counts.get(credential, 0) + 1
            return True

    def _release_stream(self, keys: tuple[str, ...]) -> None:
        with self._stream_lock:
            self._stream_global = max(self._stream_global - 1, 0)
            for key in keys:
                count = self._stream_counts.get(key, 0)
                if count <= 1:
                    self._stream_counts.pop(key, None)
                else:
                    self._stream_counts[key] = count - 1

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

    def _check_run_create(self, scope: Mapping[str, Any], *, now: float) -> int | None:
        limit = max(int(self._settings.run_create_requests), 1)
        window = max(float(self._settings.run_create_window_s), 1.0)
        if now - self._run_create_last_sweep >= window:
            self._run_create_last_sweep = now
            for key, bucket in list(self._run_create_buckets.items()):
                while bucket and now - bucket[0] >= window:
                    bucket.popleft()
                if not bucket:
                    del self._run_create_buckets[key]
        keys = _rate_keys(scope, trusted_proxy_networks=self._trusted_proxy_networks)
        allocated: list[deque[float]] = []
        max_buckets = max(int(self._settings.max_buckets), 1)
        for index, key in enumerate(keys):
            bucket = self._run_create_buckets.get(key)
            if bucket is None:
                if len(self._run_create_buckets) >= max_buckets:
                    # The source bucket is mandatory. At capacity, the optional
                    # credential bucket can be omitted because the source still
                    # constrains run creation (and max_buckets=1 remains usable).
                    if index == 0:
                        return int(window)
                    continue
                bucket = deque()
                self._run_create_buckets[key] = bucket
            while bucket and now - bucket[0] >= window:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(int(window - (now - bucket[0])), 1)
            allocated.append(bucket)
        for bucket in allocated:
            bucket.append(now)
        return None

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


def _is_run_creation_request(scope: Mapping[str, Any]) -> bool:
    if str(scope.get("method") or "").upper() != "POST":
        return False
    path = _native_path_without_trailing_slash(str(scope.get("path") or ""))
    if path in {
        "/runs",
        "/runs/stream",
        "/runs/wait",
        "/threads",
        "/v1/pipelines",
        "/v1/context/summaries",
    }:
        return True
    parts = path.split("/")
    if (
        len(parts) in {4, 5}
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and (len(parts) == 4 or parts[4] in {"stream", "wait"})
    ):
        return True
    if (
        len(parts) == 5
        and parts[1:3] == ["v1", "prompts"]
        and bool(parts[3])
        and parts[4] == "test"
    ):
        return True
    return (
        len(parts) == 7
        and parts[1:3] == ["v1", "pipelines"]
        and bool(parts[3])
        and parts[4] == "gates"
        and bool(parts[5])
        and parts[6] == "resume"
    )


def _is_native_cron_path(scope: Mapping[str, Any]) -> bool:
    path = str(scope.get("path") or "")
    parts = path.split("/")
    if len(parts) >= 3 and parts[1:3] == ["runs", "crons"]:
        return True
    return (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3:5] == ["runs", "crons"]
    )


def _disabled_native_thread_primitive(scope: Mapping[str, Any]) -> str | None:
    """Reject unused native operations with disproportionate storage/CPU cost."""

    if str(scope.get("method") or "").upper() != "POST":
        return None
    path = str(scope.get("path") or "")
    if path == "/threads/prune":
        return "Native thread pruning is disabled"
    parts = path.split("/")
    if (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3:5] == ["state", "checkpoint"]
    ):
        return "Native checkpoint-body reads are disabled"
    if len(parts) == 4 and parts[1] == "threads" and bool(parts[2]) and parts[3] == "copy":
        return "Native thread copying is disabled"
    return None


def _is_disabled_thread_stream(scope: Mapping[str, Any]) -> bool:
    if str(scope.get("method") or "").upper() != "GET":
        return False
    parts = str(scope.get("path") or "").split("/")
    return len(parts) == 4 and parts[1] == "threads" and bool(parts[2]) and parts[3] == "stream"


def _is_disabled_v2_runtime_surface(scope: Mapping[str, Any]) -> bool:
    if scope.get("type") not in {"http", "websocket"}:
        return False
    path = str(scope.get("path") or "").rstrip("/")
    if path == "/commands":
        return True
    parts = path.split("/")
    return (
        len(parts) == 4 and parts[1] == "threads" and bool(parts[2]) and parts[3] == "commands"
    ) or (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3:5] == ["stream", "events"]
    )


async def _reject_disabled_runtime_surface(scope: Mapping[str, Any], send: Any) -> None:
    if scope.get("type") == "websocket":
        await send(
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "LangGraph v2 event streaming is disabled",
            }
        )
        return
    await _send_not_found(send, "LangGraph v2 event streaming is disabled")


def _is_long_lived_run_request(scope: Mapping[str, Any]) -> bool:
    method = str(scope.get("method") or "").upper()
    path = _native_path_without_trailing_slash(str(scope.get("path") or ""))
    if method == "POST" and path in {"/runs/stream", "/runs/wait"}:
        return True
    parts = str(scope.get("path") or "").split("/")
    if method == "POST":
        return (
            len(parts) == 5
            and parts[1] == "threads"
            and bool(parts[2])
            and parts[3] == "runs"
            and parts[4] in {"stream", "wait"}
        )
    if method == "GET":
        return _is_run_item_stream_path(path) or _is_run_item_join_path(path)
    return False


def _schedule_audit(scope: Mapping[str, Any], status_code: int, reason: str | None = None) -> None:
    try:
        # Audit is deliberately best-effort. Never let a slow database turn an
        # unauthenticated stream of 401/429 responses into unbounded tasks.
        if len(_PENDING_AUDIT) >= _MAX_PENDING_AUDIT:
            return
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


async def _send_limit_backend_unavailable(send: Any) -> None:
    payload = {
        "type": "about:blank",
        "title": "Service Unavailable",
        "status": 503,
        "detail": "Shared admission control is temporarily unavailable",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"retry-after", b"1"),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_not_found(send: Any, detail: str) -> None:
    payload = {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": detail,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_payload_too_large(send: Any, limit: int) -> None:
    payload = {
        "type": "about:blank",
        "title": "Payload Too Large",
        "status": 413,
        "detail": f"Request body exceeds the {limit}-byte limit",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_request_timeout(send: Any, timeout_s: float) -> None:
    payload = {
        "type": "about:blank",
        "title": "Request Timeout",
        "status": 408,
        "detail": f"Request body was not received within {timeout_s:g} seconds",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 408,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_bad_request(send: Any, detail: str) -> None:
    payload = {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": detail,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_unprocessable_entity(send: Any, detail: str) -> None:
    payload = {
        "type": "about:blank",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": detail,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 422,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer"),
                (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _request_target_contains_nul(scope: Mapping[str, Any]) -> bool:
    path = str(scope.get("path") or "")
    if "\x00" in path:
        return True
    for name in ("raw_path", "query_string"):
        value = scope.get(name) or b""
        raw = value if isinstance(value, bytes) else str(value).encode("latin-1", errors="ignore")
        if b"\x00" in raw or b"%00" in raw.lower():
            return True
    return False


def _is_json_mutation_request(scope: Mapping[str, Any]) -> bool:
    if str(scope.get("method") or "").upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    for raw_name, raw_value in scope.get("headers") or []:
        name = (
            raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
        ).casefold()
        if name != "content-type":
            continue
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
        media_type = value.split(";", 1)[0].strip().casefold()
        if media_type == "application/json" or media_type.endswith("+json"):
            return True
    return False


def _json_nesting_limit_error(body: bytes) -> str | None:
    return _JsonNestingPrecheck(_MAX_JSON_BODY_DEPTH).feed(body)


def _credential_header_error(scope: Mapping[str, Any]) -> str | None:
    """Reject ambiguous credentials while raw duplicate ASGI headers are intact."""

    counts = {b"x-api-key": 0, b"authorization": 0}
    for raw_name, _raw_value in scope.get("headers") or []:
        if not isinstance(raw_name, bytes):
            continue
        name = raw_name.lower()
        if name in counts:
            counts[name] += 1
    if counts[b"x-api-key"] > 1:
        return "Duplicate x-api-key headers are not allowed"
    if counts[b"authorization"] > 1:
        return "Duplicate authorization headers are not allowed"
    if counts[b"x-api-key"] and counts[b"authorization"]:
        return "x-api-key and authorization headers cannot be combined"
    return None


def _is_thread_history_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 4 and parts[1] == "threads" and bool(parts[2]) and parts[3] == "history"


def _is_thread_runs_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 4 and parts[1] == "threads" and bool(parts[2]) and parts[3] == "runs"


def _is_run_create_path(path: str) -> bool:
    return path == "/runs" or _is_thread_runs_path(path)


def _public_native_json_projection_kind(
    method: str,
    path: str,
    *,
    trusted_loopback: bool,
) -> str | None:
    """Select the fail-closed public projection for native success bodies."""

    if trusted_loopback:
        return None
    if method == "POST" and path == "/threads":
        return "thread"
    if method == "POST" and _is_run_create_path(path):
        return "run"
    if method == "GET" and _is_thread_runs_path(path):
        return "run_list"
    if method == "POST" and path == "/threads/search":
        return "thread_list"
    if method == "POST" and path == "/assistants/search":
        return "assistant_list"
    if method == "GET" and _assistant_structure_path_kind(path) is not None:
        return "assistant_structure"
    if (method in {"GET", "PATCH"} and _is_assistant_item_path(path)) or (
        method == "POST" and path == "/assistants"
    ):
        return "assistant"
    return None


def _is_run_create_stream_path(path: str) -> bool:
    path = _native_path_without_trailing_slash(path)
    if path == "/runs/stream":
        return True
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and parts[4] == "stream"
    )


def _is_run_item_stream_path(path: str) -> bool:
    path = _native_path_without_trailing_slash(path)
    parts = path.split("/")
    return (len(parts) == 4 and parts[1] == "runs" and bool(parts[2]) and parts[3] == "stream") or (
        len(parts) == 6
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and bool(parts[4])
        and parts[5] == "stream"
    )


def _native_path_without_trailing_slash(path: str) -> str:
    """Match the single canonical route even when the ASGI router redirects slashes."""

    return path.rstrip("/") or "/"


def _is_thread_item_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 3 and parts[1] == "threads" and bool(parts[2])


def _blocked_public_native_projection(scope: Mapping[str, Any]) -> str | None:
    """Hide native endpoints whose responses contain unprojected graph state."""

    if _scope_is_trusted_loopback(scope):
        return None
    method = str(scope.get("method") or "").upper()
    path = str(scope.get("path") or "")
    parts = path.split("/")
    if method == "GET" and _is_thread_item_path(path):
        # The native Thread schema includes ``values`` and interrupts; it is not
        # a metadata-only get even though the SDK's example omits those fields.
        return "Native thread state reads are disabled; use the bounded /v1 pipeline API"
    if (
        method == "GET"
        and len(parts) in {4, 5}
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "state"
    ):
        return "Native thread state reads are disabled; use the bounded /v1 pipeline API"
    if (
        method in {"GET", "POST"}
        and len(parts) == 4
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "history"
    ):
        return "Native thread history reads are disabled"
    if method == "GET" and _is_run_item_join_path(path):
        return "Native run result reads are disabled; use the bounded /v1 pipeline API"
    if method == "GET" and _is_run_item_path(path):
        return "Native run detail reads are disabled; use the bounded /v1 pipeline API"
    if method == "POST" and _is_run_wait_path(path):
        return "Native run wait responses are disabled; use the bounded /v1 pipeline API"
    if method == "POST" and _is_run_cancel_path(path):
        return "Native run cancellation is disabled; use the bounded /v1 pipeline abort API"
    if method == "DELETE" and (_is_run_item_path(path) or _is_thread_item_path(path)):
        return "Native thread/run deletion is disabled; use the bounded /v1 lifecycle API"
    if (
        method in {"POST", "PATCH"}
        and len(parts) == 4
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "state"
    ):
        return "Native thread state mutations are disabled; use the bounded /v1 pipeline API"
    if method == "POST" and _is_assistant_versions_path(path):
        return "Native assistant version history is disabled on the public API"
    if method == "POST" and _is_assistant_latest_path(path):
        return "Native assistant version selection is disabled on the public API"
    return None


def _is_run_item_join_path(path: str) -> bool:
    parts = path.split("/")
    return (len(parts) == 4 and parts[1] == "runs" and bool(parts[2]) and parts[3] == "join") or (
        len(parts) == 6
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and bool(parts[4])
        and parts[5] == "join"
    )


def _is_run_item_path(path: str) -> bool:
    parts = path.split("/")
    return (len(parts) == 3 and parts[1] == "runs" and bool(parts[2])) or (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and bool(parts[4])
    )


def _is_run_wait_path(path: str) -> bool:
    path = _native_path_without_trailing_slash(path)
    if path == "/runs/wait":
        return True
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3:5] == ["runs", "wait"]
    )


def _is_run_cancel_path(path: str) -> bool:
    if path == "/runs/cancel":
        return True
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[1] == "threads"
        and bool(parts[2])
        and parts[3] == "runs"
        and bool(parts[4])
        and parts[5] == "cancel"
    )


def _sse_event_boundary(buffer: bytearray) -> tuple[int, int] | None:
    """Return the earliest complete SSE event boundary in ``buffer``."""

    lf = buffer.find(b"\n\n")
    crlf = buffer.find(b"\r\n\r\n")
    candidates = [(lf, 2), (crlf, 4)]
    present = [(offset, size) for offset, size in candidates if offset >= 0]
    return min(present, default=None)


def _safe_stream_error_event() -> bytes:
    return b'event: error\ndata: {"error":"Run failed; inspect the pipeline snapshot"}\n\n'


def _safe_native_problem_body(status: int) -> bytes:
    title = "Native LangGraph request failed" if status >= 500 else "Native request rejected"
    payload = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": "The native LangGraph request could not be completed",
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _safe_native_problem_start(message: dict[str, Any]) -> dict[str, Any]:
    """Discard provider/runtime error headers as well as its reflective body."""

    projected = {
        **message,
        "headers": [(b"content-type", b"application/problem+json")],
    }
    projected.pop("trailers", None)
    return projected


def _safe_projected_json_start(
    message: dict[str, Any],
    *,
    preserve_run_location: bool = False,
) -> dict[str, Any]:
    """Expose only fixed JSON metadata alongside a projected native body."""

    headers = [(b"content-type", b"application/json")]
    if preserve_run_location:
        locations: list[bytes] = []
        for raw_name, raw_value in message.get("headers") or []:
            name = raw_name if isinstance(raw_name, bytes) else str(raw_name).encode("latin-1")
            if name.lower() != b"content-location":
                continue
            value = raw_value if isinstance(raw_value, bytes) else str(raw_value).encode("latin-1")
            locations.append(value)
        if len(locations) == 1 and _safe_run_content_location(locations[0]):
            headers.append((b"content-location", locations[0]))
    projected = {**message, "headers": headers}
    projected.pop("trailers", None)
    return projected


def _safe_empty_projection_start(message: dict[str, Any]) -> dict[str, Any]:
    """Turn a mutation that may return native state into an opaque success."""

    projected = {**message, "status": 204, "headers": []}
    projected.pop("trailers", None)
    return projected


_NATIVE_THREAD_SUMMARY_FIELDS = frozenset(
    {"thread_id", "created_at", "updated_at", "state_updated_at", "status"}
)
_NATIVE_RUN_SUMMARY_FIELDS = frozenset(
    {
        "run_id",
        "thread_id",
        "assistant_id",
        "created_at",
        "updated_at",
        "status",
        "multitask_strategy",
    }
)
_NATIVE_ASSISTANT_SUMMARY_FIELDS = frozenset(
    {
        "assistant_id",
        "graph_id",
        "name",
        "description",
        "created_at",
        "updated_at",
        "version",
    }
)


def _safe_run_content_location(value: bytes) -> bool:
    """Accept only the SDK's bounded relative run identity header."""

    if not value or len(value) > 1_024 or any(byte < 0x21 or byte > 0x7E for byte in value):
        return False
    text = value.decode("ascii")
    if any(marker in text for marker in ("?", "#", "@", "\\")):
        return False
    parts = text.split("/")
    identifiers: tuple[str, ...]
    if len(parts) == 3 and parts[1] == "runs":
        identifiers = (parts[2],)
    elif len(parts) == 5 and parts[1] == "threads" and parts[3] == "runs":
        identifiers = (parts[2], parts[4])
    else:
        return False
    return all(_safe_native_identifier(identifier) for identifier in identifiers)


def _safe_native_identifier(value: str) -> bool:
    return bool(
        value
        and len(value) <= _MAX_LANGGRAPH_IDENTIFIER_CHARS
        and all(character.isalnum() or character in "-_.:" for character in value)
    )


def _safe_run_stream_location(value: bytes) -> bytes | None:
    """Canonicalize a relative reconnect path onto the custom-only channel."""

    if not value or len(value) > 1_024 or any(byte < 0x21 or byte > 0x7E for byte in value):
        return None
    text = value.decode("ascii")
    if any(marker in text for marker in ("#", "@", "\\")):
        return None
    path, separator, query = text.partition("?")
    if separator and parse_qs(query, keep_blank_values=True) != {"stream_mode": ["custom"]}:
        return None
    parts = path.split("/")
    identifiers: tuple[str, ...]
    if len(parts) == 4 and parts[1] == "runs" and parts[3] == "stream":
        identifiers = (parts[2],)
    elif len(parts) == 6 and parts[1] == "threads" and parts[3] == "runs" and parts[5] == "stream":
        identifiers = (parts[2], parts[4])
    else:
        return None
    if not all(_safe_native_identifier(identifier) for identifier in identifiers):
        return None
    return f"{path}?stream_mode=custom".encode("ascii")


def _safe_public_stream_start(message: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the two bounded SDK stream headers and fixed content type."""

    content_locations: list[bytes] = []
    reconnect_locations: list[bytes] = []
    for raw_name, raw_value in message.get("headers") or []:
        name = raw_name if isinstance(raw_name, bytes) else str(raw_name).encode("latin-1")
        value = raw_value if isinstance(raw_value, bytes) else str(raw_value).encode("latin-1")
        if name.lower() == b"content-location":
            content_locations.append(value)
        elif name.lower() == b"location":
            reconnect_locations.append(value)
    headers = [(b"content-type", b"text/event-stream")]
    if len(content_locations) == 1 and _safe_run_content_location(content_locations[0]):
        headers.append((b"content-location", content_locations[0]))
    if len(reconnect_locations) == 1:
        if safe_location := _safe_run_stream_location(reconnect_locations[0]):
            headers.append((b"location", safe_location))
    projected = {**message, "headers": headers}
    projected.pop("trailers", None)
    return projected


def _empty_native_projection(kind: str) -> bytes:
    return b"[]" if kind.endswith("_list") else b"{}"


def _safe_native_projection_response(kind: str, body: bytes) -> bytes:
    """Project native success bodies without trusting runtime select handling."""

    if kind in {"assistant", "assistant_structure"}:
        return _safe_assistant_response(body)
    try:
        parsed = json.loads(body)
        validate_json_object(
            {"items": parsed},
            label="native response",
            max_bytes=_MAX_NATIVE_PROJECTION_JSON_BYTES,
            max_nodes=_MAX_NATIVE_PROJECTION_JSON_NODES,
            max_depth=_MAX_JSON_BODY_DEPTH,
        )
        if kind.endswith("_list"):
            if not isinstance(parsed, list):
                return b"[]"
            fields = {
                "thread_list": _NATIVE_THREAD_SUMMARY_FIELDS,
                "run_list": _NATIVE_RUN_SUMMARY_FIELDS,
                "assistant_list": _NATIVE_ASSISTANT_SUMMARY_FIELDS,
            }[kind]
            projected: Any = [
                _project_native_summary(item, fields) for item in parsed if isinstance(item, dict)
            ]
        else:
            if not isinstance(parsed, dict):
                return b"{}"
            fields = {
                "thread": _NATIVE_THREAD_SUMMARY_FIELDS,
                "run": _NATIVE_RUN_SUMMARY_FIELDS,
            }[kind]
            projected = _project_native_summary(parsed, fields)
        return json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, RecursionError, TypeError, ValueError, json.JSONDecodeError):
        return _empty_native_projection(kind)


def _project_native_summary(value: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {field: _redact_public_json(value[field]) for field in fields if field in value}


def _safe_assistant_response(body: bytes) -> bytes:
    """Redact a legacy assistant item before it crosses the native boundary."""

    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            return b"{}"
        validate_json_object(
            parsed,
            label="assistant response",
            max_bytes=_MAX_NATIVE_PROJECTION_JSON_BYTES,
            max_nodes=_MAX_NATIVE_PROJECTION_JSON_NODES,
            max_depth=_MAX_JSON_BODY_DEPTH,
        )
        projected = _redact_public_json(parsed)
        return json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        return b"{}"


def _redact_public_json(value: Any) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_name, nested in value.items():
            name = bounded_diagnostic(raw_name, max_chars=max(1, len(raw_name)))
            if is_credential_field(name):
                continue
            projected[name] = _redact_public_json(nested)
        return projected
    if isinstance(value, list):
        return [_redact_public_json(item) for item in value]
    if isinstance(value, str):
        return bounded_diagnostic(value, max_chars=max(1, len(value)))
    return value


def _sanitize_public_sse_event(event: bytes) -> bytes:
    """Allow only redacted custom events; drop every raw runtime state channel."""

    normalized = event.replace(b"\r\n", b"\n")
    event_name: bytes | None = None
    for line in normalized.split(b"\n"):
        if line.startswith(b"event:"):
            event_name = line[6:].strip().lower()
            break
    if event_name == b"error":
        return _safe_stream_error_event()
    if event_name == b"custom" or (event_name is not None and event_name.startswith(b"custom|")):
        return _sanitize_public_custom_event(normalized, event_name)
    lines = [line for line in normalized.split(b"\n") if line]
    if lines and all(line.startswith(b":") for line in lines):
        return b": heartbeat\n\n"
    # LangGraph may internally add updates and emit interrupt state as `values`
    # even when the join requested custom only. Unknown event names fail closed.
    return b""


def _sanitize_public_custom_event(normalized: bytes, event_name: bytes) -> bytes:
    """Parse and project custom JSON instead of trusting raw SSE text."""

    data_lines: list[bytes] = []
    event_id: bytes | None = None
    for line in normalized.split(b"\n"):
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip(b" "))
        elif line.startswith(b"id:"):
            event_id = line[3:].strip()
    if not data_lines:
        return b""
    try:
        data = json.loads(b"\n".join(data_lines))
        if not isinstance(data, dict):
            return b""
        validate_json_object(
            {"data": data},
            label="custom stream event",
            max_bytes=_MAX_PUBLIC_STREAM_EVENT_BYTES,
            max_nodes=_MAX_NATIVE_PROJECTION_JSON_NODES,
            max_depth=_MAX_JSON_BODY_DEPTH,
        )
        projected = _redact_public_json(data)
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        return b""
    safe_name = bounded_diagnostic(
        event_name.decode("utf-8", errors="replace"),
        max_chars=512,
    ).encode("utf-8")
    lines = [b"event: " + safe_name]
    if event_id is not None:
        decoded_id = event_id.decode("ascii", errors="ignore")
        if len(decoded_id) <= 64 and _is_valid_redis_stream_id(decoded_id):
            lines.append(b"id: " + decoded_id.encode("ascii"))
    lines.append(b"data: " + encoded)
    return b"\n".join(lines) + b"\n\n"


def _is_assistant_versions_path(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) == 4 and parts[1] == "assistants" and bool(parts[2]) and parts[3] == "versions"
    )


def _is_assistant_latest_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 4 and parts[1] == "assistants" and bool(parts[2]) and parts[3] == "latest"


def _is_assistant_item_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 3 and parts[1] == "assistants" and bool(parts[2])


def _assistant_structure_path_kind(path: str) -> str | None:
    """Return the bounded structural assistant-read surface selected by a path."""

    parts = _native_path_without_trailing_slash(path).split("/")
    if len(parts) == 4 and parts[1] == "assistants" and bool(parts[2]):
        if parts[3] in {"graph", "schemas", "subgraphs"}:
            return parts[3]
    if (
        len(parts) == 5
        and parts[1] == "assistants"
        and bool(parts[2])
        and parts[3] == "subgraphs"
        and bool(parts[4])
    ):
        return "subgraphs"
    return None


def _is_cron_item_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 4 and parts[1] == "runs" and parts[2] == "crons" and bool(parts[3])


def _direct_langgraph_read_body_path(scope: Mapping[str, Any]) -> str | None:
    method = str(scope.get("method") or "").upper()
    path = str(scope.get("path") or "")
    if method == "PATCH" and _is_thread_item_path(path):
        return "thread_update"
    if method == "PATCH" and _is_assistant_item_path(path):
        return "assistant_write"
    if method == "PATCH" and _is_cron_item_path(path):
        return "cron_write"
    if method != "POST":
        return None
    if _is_run_create_stream_path(path):
        return "run_stream_create"
    if path == "/threads":
        return "thread_create"
    if path == "/assistants":
        return "assistant_write"
    if path == "/runs/crons":
        return "cron_write"
    if path == "/threads/search":
        return "search"
    if path == "/runs/batch":
        return "runs_batch"
    if path == "/assistants/search":
        return "assistant_search"
    if path == "/assistants/count":
        return "assistant_count"
    if _is_assistant_versions_path(path):
        return "assistant_versions"
    if _is_thread_history_path(path):
        return "history"
    return None


def _direct_langgraph_query_limit_error(scope: Mapping[str, Any]) -> str | None:
    method = str(scope.get("method") or "").upper()
    path = str(scope.get("path") or "")
    if path == "/ready" or path.startswith("/v1/"):
        return None
    raw_query = scope.get("query_string") or b""
    query = raw_query.decode("latin-1") if isinstance(raw_query, bytes) else str(raw_query)
    if len(query) > _MAX_LANGGRAPH_QUERY_CHARS:
        return "Native query string exceeds the bounded request limit"
    try:
        params = parse_qs(
            query,
            keep_blank_values=True,
            max_num_fields=_MAX_LANGGRAPH_QUERY_FIELDS,
        )
    except ValueError:
        return "Native query string contains too many fields"
    if method == "DELETE" and _is_assistant_item_path(path):
        if set(params) - {"delete_threads"}:
            return "Native assistant deletion contains unsupported query controls"
        delete_threads = params.get("delete_threads")
        if delete_threads is not None and delete_threads != ["false"]:
            return (
                "Assistant deletion cannot cascade to threads; abort and delete pipeline "
                "resources through the bounded /v1 lifecycle API"
            )
        return None
    if method != "GET":
        return None
    assistant_structure = _assistant_structure_path_kind(path)
    if assistant_structure is not None:
        parts = _native_path_without_trailing_slash(path).split("/")
        if len(parts[2]) > _MAX_LANGGRAPH_IDENTIFIER_CHARS or (
            len(parts) == 5 and len(parts[4]) > _MAX_LANGGRAPH_IDENTIFIER_CHARS
        ):
            return "Native assistant structural read contains an oversized identifier"
        if _scope_is_trusted_loopback(scope):
            return None
        if assistant_structure == "schemas":
            if params:
                return "Native assistant schemas contain unsupported query controls"
            return None
        allowed_query = {"xray"} if assistant_structure == "graph" else {"recurse"}
        if set(params) - allowed_query:
            return "Native assistant structural read contains unsupported query controls"
        if assistant_structure == "subgraphs":
            recurse = params.get("recurse")
            if recurse is not None and recurse not in (["false"], ["False"]):
                return "Public assistant subgraph reads cannot recurse without a depth bound"
            return None
        xray = params.get("xray")
        if xray is None:
            return None
        if len(xray) != 1:
            return "Native assistant graph xray must be supplied at most once"
        value = xray[0]
        if value in {"false", "False"}:
            return None
        if not value.isdecimal():
            return "Native assistant graph xray must be a bounded positive integer"
        depth = int(value)
        if not 1 <= depth <= _MAX_ASSISTANT_XRAY_DEPTH:
            return (
                "Native assistant graph xray depth must be between 1 and "
                f"{_MAX_ASSISTANT_XRAY_DEPTH}"
            )
        return None
    is_run_stream = _is_run_item_stream_path(path)
    is_history = _is_thread_history_path(path)
    is_runs = _is_thread_runs_path(path)
    if not is_run_stream and not is_history and not is_runs:
        return None
    if is_run_stream:
        if error := _last_event_id_error(scope):
            return error
    if is_run_stream and not _scope_is_trusted_loopback(scope):
        if params.get("streamMode") is not None:
            return "LangGraph run streams require the canonical stream_mode parameter"
        if params.get("stream_mode") != ["custom"]:
            return "Public LangGraph run streams expose exactly the custom event mode"
        cancel_values = params.get("cancel_on_disconnect")
        if cancel_values is not None and cancel_values != ["false"]:
            return (
                "Native cancel_on_disconnect is disabled; use false or omit it so "
                "external engine cleanup cannot be bypassed"
            )
    if not is_history and not is_runs:
        return None
    resource = "run list" if is_runs else "history"
    max_limit = _MAX_LANGGRAPH_READ_PAGE_SIZE if is_runs else _MAX_LANGGRAPH_HISTORY_PAGE_SIZE
    limits = params.get("limit")
    if limits is not None:
        if len(limits) != 1:
            return f"LangGraph {resource} limit must be supplied at most once"
        try:
            limit = int(limits[0])
        except ValueError:
            return f"LangGraph {resource} limit must be an integer"
        if not 1 <= limit <= max_limit:
            return f"LangGraph {resource} limit must be between 1 and {max_limit}"
    if is_runs and (offsets := params.get("offset")) is not None:
        if len(offsets) != 1:
            return "LangGraph run list offset must be supplied at most once"
        try:
            offset = int(offsets[0])
        except ValueError:
            return "LangGraph run list offset must be an integer"
        if not 0 <= offset <= _MAX_LANGGRAPH_READ_OFFSET:
            return f"LangGraph run list offset must be between 0 and {_MAX_LANGGRAPH_READ_OFFSET}"
    if is_runs and (statuses := params.get("status")) is not None:
        if len(statuses) != 1:
            return "LangGraph run list status must be supplied at most once"
        if statuses[0] not in _LANGGRAPH_RUN_STATUSES:
            allowed = ", ".join(sorted(_LANGGRAPH_RUN_STATUSES))
            return f"LangGraph run list status must be one of: {allowed}"
    if is_history and (before := params.get("before")) is not None:
        if len(before) != 1:
            return "LangGraph history before must be supplied at most once"
        if len(before[0]) > _MAX_LANGGRAPH_IDENTIFIER_CHARS:
            return (
                "LangGraph history before must contain at most "
                f"{_MAX_LANGGRAPH_IDENTIFIER_CHARS} characters"
            )
    if is_runs:
        select = params.get("select")
        allowed_select = {
            "run_id",
            "thread_id",
            "assistant_id",
            "created_at",
            "updated_at",
            "multitask_strategy",
            "status",
            "run_started_at",
            "run_ended_at",
            "webhook_sent_at",
        }
        if (
            select is None
            or not {"run_id", "status"}.issubset(select)
            or len(select) > len(allowed_select)
            or any(field not in allowed_select for field in select)
            or len(set(select)) != len(select)
        ):
            return (
                "LangGraph run list select must contain run_id and status and be a unique "
                "subset of the bounded run summary fields"
            )
    return None


def _last_event_id_error(scope: Mapping[str, Any]) -> str | None:
    """Bound the resumable Redis cursor before it reaches the runtime backend."""

    values: list[str] = []
    for raw_name, raw_value in scope.get("headers") or []:
        name = raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
        if name.casefold() != "last-event-id":
            continue
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
        values.append(value)
    if not values:
        return None
    if len(values) != 1:
        return "Last-Event-ID must be supplied at most once"
    value = values[0]
    if not value:
        return None
    if len(value) > 64:
        return "Last-Event-ID must be a bounded Redis stream ID"
    if not _is_valid_redis_stream_id(value):
        return "Last-Event-ID must be a valid Redis stream ID"
    return None


def _is_valid_redis_stream_id(value: str) -> bool:
    if value == "-":
        return True
    timestamp, separator, sequence = value.partition("-")

    def ascii_digits(part: str) -> bool:
        return bool(part) and all("0" <= char <= "9" for char in part)

    return ascii_digits(timestamp) and (not separator or sequence == "*" or ascii_digits(sequence))


def _direct_langgraph_body_limit_error(
    kind: str,
    body: bytes,
    *,
    trusted_loopback: bool = False,
) -> str | None:
    if error := _json_nesting_limit_error(body):
        return error
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if kind == "runs_batch":
        return "Direct LangGraph run batches are disabled; create one bounded run per request"
    if not isinstance(payload, dict):
        return None
    if kind == "run_stream_create":
        if trusted_loopback:
            return None
        if "streamMode" in payload:
            return "LangGraph run streams require the canonical stream_mode field"
        stream_mode = payload.get("stream_mode")
        if stream_mode != "custom" and stream_mode != ["custom"]:
            return "Public LangGraph run streams expose exactly the custom event mode"
        if "on_disconnect" in payload and payload["on_disconnect"] != "continue":
            return (
                "Native on_disconnect cancellation is disabled; use 'continue' or omit it so "
                "external engine cleanup cannot be bypassed"
            )
        return None
    if kind in {"thread_create", "thread_update"}:
        if trusted_loopback:
            return None
        if payload.get("ttl") is not None:
            return "Native thread ttl is disabled on the public API"
        if payload.get("supersteps") is not None:
            return "Native thread supersteps are disabled on the public API"
        if kind == "thread_create" and payload.get("thread_id") is not None:
            return "Native caller-selected thread IDs are disabled; use the generated thread_id"
        if kind == "thread_create" and payload.get("if_exists") is not None:
            return "Native thread collision controls are disabled on the public API"
        return None
    if kind == "assistant_write":
        for field, max_chars in (
            ("graph_id", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
            ("name", _MAX_LANGGRAPH_IDENTIFIER_CHARS),
            ("description", _MAX_LANGGRAPH_DESCRIPTION_CHARS),
        ):
            if error := _bounded_optional_text_error(
                payload.get(field),
                label=f"LangGraph assistant {field}",
                max_chars=max_chars,
            ):
                return error
        return None
    if kind == "cron_write":
        return _bounded_optional_text_error(
            payload.get("timezone"),
            label="LangGraph cron timezone",
            max_chars=_MAX_LANGGRAPH_IDENTIFIER_CHARS,
        )
    if kind in {"assistant_search", "assistant_count"}:
        for field in ("graph_id", "name"):
            if error := _bounded_optional_text_error(
                payload.get(field),
                label=f"LangGraph {kind} {field}",
                max_chars=_MAX_LANGGRAPH_IDENTIFIER_CHARS,
            ):
                return error
        if kind == "assistant_count":
            return _bounded_json_object_error(
                payload.get("metadata"),
                label="LangGraph assistant_count metadata",
            )
    default_limit = 1 if kind == "history" else 10
    limit = payload.get("limit", default_limit)
    page_size = {
        "history": _MAX_LANGGRAPH_HISTORY_PAGE_SIZE,
        "assistant_search": _MAX_LANGGRAPH_ASSISTANT_PAGE_SIZE,
        "assistant_versions": _MAX_LANGGRAPH_ASSISTANT_VERSIONS_PAGE_SIZE,
    }.get(kind, _MAX_LANGGRAPH_READ_PAGE_SIZE)
    if kind == "search" and not trusted_loopback:
        page_size = 10
    if type(limit) is not int or not 1 <= limit <= page_size:
        return f"LangGraph {kind} limit must be between 1 and {page_size}"
    if kind in {"search", "assistant_search", "assistant_versions"}:
        offset = payload.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= _MAX_LANGGRAPH_READ_OFFSET:
            return f"LangGraph {kind} offset must be between 0 and {_MAX_LANGGRAPH_READ_OFFSET}"
    if kind == "search":
        ids = payload.get("ids")
        if isinstance(ids, list) and len(ids) > _MAX_LANGGRAPH_THREAD_IDS:
            return (
                f"LangGraph thread search ids must contain at most "
                f"{_MAX_LANGGRAPH_THREAD_IDS} entries"
            )
        if not trusted_loopback:
            if payload.get("extract") is not None:
                return "LangGraph thread search extract is disabled on the public list surface"
            select = payload.get("select")
            allowed_select = {
                "thread_id",
                "created_at",
                "updated_at",
                "status",
            }
            if (
                not isinstance(select, list)
                or not select
                or "thread_id" not in select
                or len(select) > len(allowed_select)
                or any(type(field) is not str or field not in allowed_select for field in select)
                or len(set(select)) != len(select)
            ):
                return (
                    "LangGraph thread search select must contain thread_id and be a unique "
                    "subset of the bounded thread summary fields"
                )
    if kind == "history":
        for field in ("before", "checkpoint"):
            if error := _bounded_checkpoint_config_error(
                payload.get(field),
                label=f"LangGraph history {field}",
            ):
                return error
    if kind == "assistant_search":
        select = payload.get("select")
        allowed_select = {
            "assistant_id",
            "graph_id",
            "name",
            "description",
            "created_at",
            "updated_at",
            "version",
        }
        if (
            not isinstance(select, list)
            or not select
            or "assistant_id" not in select
            or len(select) > len(allowed_select)
            or any(type(field) is not str or field not in allowed_select for field in select)
            or len(set(select)) != len(select)
        ):
            return (
                "LangGraph assistant_search select must be a non-empty unique subset of "
                "the bounded assistant summary fields"
            )
    if kind in {"history", "assistant_search", "assistant_versions"}:
        if error := _bounded_json_object_error(
            payload.get("metadata"),
            label=f"LangGraph {kind} metadata",
        ):
            return error
    return None


def _bounded_json_object_error(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{label} must be a JSON object"
    try:
        validate_json_object(
            value,
            label=label,
            max_bytes=_MAX_LANGGRAPH_FILTER_BYTES,
            max_nodes=_MAX_LANGGRAPH_FILTER_NODES,
        )
    except ValueError as exc:
        return bounded_diagnostic(exc, max_chars=1_024)
    return None


def _bounded_checkpoint_config_error(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{label} must be a JSON object"
    allowed = {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map"}
    unknown_count = sum(key not in allowed for key in value)
    if unknown_count:
        return f"{label} contains {unknown_count} unsupported field(s)"
    for field in ("thread_id", "checkpoint_ns", "checkpoint_id"):
        if error := _bounded_optional_text_error(
            value.get(field),
            label=f"{label} {field}",
            max_chars=_MAX_LANGGRAPH_IDENTIFIER_CHARS,
        ):
            return error
    checkpoint_map = value.get("checkpoint_map")
    if checkpoint_map not in (None, {}):
        return f"{label} checkpoint_map is disabled"
    return None


def _bounded_optional_text_error(value: Any, *, label: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return f"{label} must be a string"
    if len(value) > max_chars:
        return f"{label} must contain at most {max_chars} characters"
    if "\x00" in value:
        return f"{label} must not contain U+0000"
    return None


def _langsmith_baggage_error(scope: Mapping[str, Any]) -> str | None:
    """Validate LangSmith metadata before the runtime's uncaught JSON decode."""

    headers: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name = (
            raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
        ).casefold()
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
        headers.setdefault(name, []).append(value)
    if "langsmith-trace" not in headers:
        return None
    for baggage in headers.get("baggage", []):
        for item in baggage.split(","):
            if "=" not in item:
                continue
            key, encoded_value = item.split("=", 1)
            if key != "langsmith-metadata":
                continue
            try:
                decoded_value = unquote(encoded_value)
                if error := _json_nesting_limit_error(decoded_value.encode("utf-8")):
                    return f"LangSmith baggage metadata: {error}"
                metadata = json.loads(decoded_value)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return "LangSmith baggage metadata must contain valid URL-encoded JSON"
            if error := _bounded_json_object_error(
                metadata,
                label="LangSmith baggage metadata",
            ):
                return error
    return None


def _scope_is_trusted_loopback(scope: Mapping[str, Any]) -> bool:
    headers: dict[bytes, bytes] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name = raw_name if isinstance(raw_name, bytes) else str(raw_name).encode("latin-1")
        value = raw_value if isinstance(raw_value, bytes) else str(raw_value).encode("latin-1")
        headers[name.lower()] = value
    return is_trusted_loopback(headers)


def _content_length(headers: Any) -> int | None:
    values: list[int] = []
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
        if name.casefold() != "content-length":
            continue
        value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if parsed >= 0:
            values.append(parsed)
    return max(values) if values else None


def _set_header(headers: _HeaderList, key: bytes, value: bytes) -> None:
    lower = key.lower()
    for index, (existing, _) in enumerate(headers):
        if existing.lower() == lower:
            headers[index] = (key, value)
            return
    headers.append((key, value))


def _append_vary(headers: _HeaderList, *names: str) -> None:
    existing_values = [value.decode("latin-1") for key, value in headers if key.lower() == b"vary"]
    tokens: list[str] = []
    seen: set[str] = set()
    for value in (*existing_values, *names):
        for token in value.split(","):
            normalized = token.strip()
            folded = normalized.casefold()
            if normalized and folded not in seen:
                tokens.append(normalized)
                seen.add(folded)
    if tokens:
        _set_header(headers, b"vary", ", ".join(tokens).encode("latin-1"))


def _scope_has_api_credentials(scope: Mapping[str, Any]) -> bool:
    return any(
        isinstance(name, bytes) and name.lower() in {b"x-api-key", b"authorization"}
        for name, _value in scope.get("headers") or []
    )
