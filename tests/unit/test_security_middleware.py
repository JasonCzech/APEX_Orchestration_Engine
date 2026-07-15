import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from apex.app.distributed_limits import LimitBackendUnavailable, StreamLease
from apex.app.security import (
    AuthAuditMiddleware,
    RateLimitMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    mark_stream_request_authenticated,
)
from apex.settings import RateLimitSettings, RequestBodySettings, SecurityHeadersSettings


async def ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def unauthorized_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 401, "headers": []})
    await send({"type": "http.response.body", "body": b"no"})


async def call_app(
    app: Any,
    *,
    path: str = "/v1/system/info",
    key: str = "k",
    client_ip: str = "203.0.113.10",
    forwarded_for: str | None = None,
    method: str = "GET",
) -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"x-api-key", key.encode("utf-8"))]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
            "client": (client_ip, 12345),
        },
        receive,
        send,
    )
    return messages


class SharedLimitBackend:
    """Small atomic test double shared by independently constructed middleware."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.windows: dict[tuple[str, str], int] = {}
        self.failures: dict[str, int] = {}
        self.lockouts: set[str] = set()
        self.stream_counts: dict[str, int] = {}
        self.stream_leases: dict[str, tuple[str, ...]] = {}
        self.next_lease = 0
        self.unavailable = False

    def _ensure_available(self) -> None:
        if self.unavailable:
            raise LimitBackendUnavailable("test backend unavailable")

    async def check_ready(self) -> None:
        self._ensure_available()

    async def check_window(
        self,
        namespace: str,
        keyed_limits: tuple[tuple[str, int], ...],
        *,
        window_s: int,
    ) -> int | None:
        self._ensure_available()
        async with self.lock:
            if any(self.windows.get((namespace, key), 0) >= limit for key, limit in keyed_limits):
                return window_s
            for key, _limit in keyed_limits:
                bucket = (namespace, key)
                self.windows[bucket] = self.windows.get(bucket, 0) + 1
        return None

    async def auth_retry_after(self, keys: tuple[str, ...]) -> int | None:
        self._ensure_available()
        return 30 if any(key in self.lockouts for key in keys) else None

    async def record_auth_failure(
        self,
        keys: tuple[str, ...],
        *,
        limit: int,
        window_s: int,
        lockout_s: int,
    ) -> None:
        del window_s, lockout_s
        self._ensure_available()
        async with self.lock:
            for key in keys:
                self.failures[key] = self.failures.get(key, 0) + 1
                if self.failures[key] >= limit:
                    self.lockouts.add(key)

    async def clear_auth(self, keys: tuple[str, ...]) -> None:
        self._ensure_available()
        async with self.lock:
            for key in keys:
                self.failures.pop(key, None)
                self.lockouts.discard(key)

    async def acquire_stream(
        self,
        keys: tuple[str, ...],
        *,
        global_limit: int,
        source_limit: int,
        credential_limit: int,
        lease_ttl_s: int,
    ) -> StreamLease | None:
        del lease_ttl_s
        self._ensure_available()
        async with self.lock:
            limits = [("global", global_limit), (keys[0], source_limit)]
            if len(keys) > 1:
                limits.append((keys[1], credential_limit))
            if any(self.stream_counts.get(key, 0) >= limit for key, limit in limits):
                return None
            self.next_lease += 1
            lease = StreamLease(id=f"lease-{self.next_lease}", redis_keys=keys)
            self.stream_leases[lease.id] = tuple(key for key, _limit in limits)
            for key, _limit in limits:
                self.stream_counts[key] = self.stream_counts.get(key, 0) + 1
            return lease

    async def renew_stream(self, lease: StreamLease, *, lease_ttl_s: int) -> bool:
        del lease_ttl_s
        self._ensure_available()
        return lease.id in self.stream_leases

    async def release_stream(self, lease: StreamLease) -> None:
        self._ensure_available()
        async with self.lock:
            for key in self.stream_leases.pop(lease.id, ()):
                count = self.stream_counts.get(key, 0)
                if count <= 1:
                    self.stream_counts.pop(key, None)
                else:
                    self.stream_counts[key] = count - 1

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_limit() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    first = await call_app(app)
    second = await call_app(app)

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429
    assert (b"content-type", b"application/problem+json") in second[0]["headers"]


async def test_request_rate_limit_is_shared_across_middleware_instances() -> None:
    backend = SharedLimitBackend()
    settings = RateLimitSettings(requests=1, window_s=60)
    first_pod = RateLimitMiddleware(ok_app, settings, backend=backend)
    second_pod = RateLimitMiddleware(ok_app, settings, backend=backend)

    first = await call_app(first_pod, key="shared-key")
    second = await call_app(second_pod, key="shared-key")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429


@pytest.mark.parametrize("path", ["/v1/pipelines", "/threads"])
async def test_run_create_limit_is_shared_across_middleware_instances(path: str) -> None:
    backend = SharedLimitBackend()
    settings = RateLimitSettings(
        requests=100,
        run_create_requests=1,
        run_create_window_s=60,
    )
    first_pod = RateLimitMiddleware(ok_app, settings, backend=backend)
    second_pod = RateLimitMiddleware(ok_app, settings, backend=backend)

    first = await call_app(first_pod, path=path, method="POST")
    second = await call_app(second_pod, path=path, method="POST")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429


async def test_native_thread_creation_uses_local_expensive_creation_limit() -> None:
    app = RateLimitMiddleware(
        ok_app,
        RateLimitSettings(
            requests=100,
            run_create_requests=1,
            run_create_window_s=60,
        ),
    )

    first = await call_app(app, path="/threads", method="POST")
    second = await call_app(app, path="/threads", method="POST")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429


async def test_trusted_loopback_thread_creation_bypasses_expensive_creation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apex.app.security._scope_is_trusted_loopback",
        lambda scope: scope.get("path") == "/threads",
    )
    app = RateLimitMiddleware(
        ok_app,
        RateLimitSettings(requests=1, run_create_requests=1),
    )

    assert (await call_app(app, path="/threads", method="POST"))[0]["status"] == 200
    assert (await call_app(app, path="/threads", method="POST"))[0]["status"] == 200
    assert app._run_create_buckets == {}


async def test_distributed_rate_limit_fails_closed_when_backend_is_unavailable() -> None:
    backend = SharedLimitBackend()
    backend.unavailable = True
    app = RateLimitMiddleware(ok_app, RateLimitSettings(), backend=backend)

    response = await call_app(app)

    assert response[0]["status"] == 503
    assert (b"retry-after", b"1") in response[0]["headers"]


@pytest.mark.asyncio
async def test_rate_limit_uses_api_key_not_shared_ip() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    await call_app(app, key="key-a")
    second_key = await call_app(app, key="key-b")

    assert second_key[0]["status"] == 200


@pytest.mark.asyncio
async def test_rate_limit_covers_langgraph_paths() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    first = await call_app(app, path="/threads")
    second = await call_app(app, path="/threads")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429


@pytest.mark.asyncio
async def test_trusted_loopback_is_not_double_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apex.app.security._scope_is_trusted_loopback",
        lambda scope: str(scope.get("path") or "").startswith("/threads/"),
    )
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    outer = await call_app(app, path="/v1/pipelines", key="same-key")
    loopback = await call_app(
        app,
        path="/threads/thread-1/runs",
        key="same-key",
        client_ip="127.0.0.1",
    )
    repeated_outer = await call_app(app, path="/v1/pipelines", key="same-key")

    assert outer[0]["status"] == 200
    assert loopback[0]["status"] == 200
    assert repeated_outer[0]["status"] == 429
    assert all("127.0.0.1" not in key for key in app._buckets)


@pytest.mark.asyncio
async def test_rate_limit_ignores_unprotected_paths() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    first = await call_app(app, path="/health")
    second = await call_app(app, path="/health")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 200


@pytest.mark.asyncio
async def test_rate_limit_disables_unbounded_thread_event_stream() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RateLimitMiddleware(inner, RateLimitSettings())
    messages = await call_app(app, path="/threads/thread-1/stream")

    assert reached is False
    assert messages[0]["status"] == 404


@pytest.mark.parametrize(
    "path",
    [
        "/commands",
        "/threads/thread-1/commands",
        "/threads/thread-1/commands/",
        "/threads/thread-1/stream/events",
    ],
)
@pytest.mark.parametrize("layer", ["body", "rate"])
async def test_security_layers_block_v2_event_streaming_http(
    path: str,
    layer: str,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app: Any
    if layer == "body":
        app = RequestBodyLimitMiddleware(inner, RequestBodySettings())
    else:
        app = RateLimitMiddleware(inner, RateLimitSettings())

    messages = await call_app(app, path=path, method="POST")

    assert reached is False
    assert messages[0]["status"] == 404


@pytest.mark.parametrize(
    "path",
    [
        "/commands",
        "/threads/thread-1/commands",
        "/threads/thread-1/commands/",
        "/threads/thread-1/stream/events",
    ],
)
@pytest.mark.parametrize("layer", ["body", "rate"])
async def test_security_layers_block_v2_event_streaming_websocket(
    path: str,
    layer: str,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app: Any
    if layer == "body":
        app = RequestBodyLimitMiddleware(inner, RequestBodySettings())
    else:
        app = RateLimitMiddleware(inner, RateLimitSettings())
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {"type": "websocket", "path": path, "headers": []},
        receive,
        send,
    )

    assert reached is False
    assert messages == [
        {
            "type": "websocket.close",
            "code": 1008,
            "reason": "LangGraph v2 event streaming is disabled",
        }
    ]


def test_langgraph_v2_event_streaming_is_disabled_in_all_runtime_configs() -> None:
    repository = Path(__file__).resolve().parents[2]
    langgraph = json.loads((repository / "langgraph.json").read_text())

    assert langgraph["http"]["disable_event_streaming"] is True
    for relative_path, setting in {
        ".env.example": "FF_V2_EVENT_STREAMING=false",
        "docker-compose.yaml": 'FF_V2_EVENT_STREAMING: "false"',
        "deploy/compose-ha/docker-compose.ha.yaml": 'FF_V2_EVENT_STREAMING: "false"',
        "deploy/helm/apex-orchestration-engine/values.yaml": ('FF_V2_EVENT_STREAMING: "false"'),
    }.items():
        contents = (repository / relative_path).read_text()
        assert setting in contents


def test_sse_admission_enforces_global_source_and_credential_caps() -> None:
    app = RateLimitMiddleware(
        ok_app,
        RateLimitSettings(
            sse_global_concurrency=3,
            sse_source_concurrency=2,
            sse_credential_concurrency=1,
        ),
    )

    assert app._acquire_stream(("ip:one", "key:a")) is True
    assert app._acquire_stream(("ip:one", "key:a")) is False
    assert app._acquire_stream(("ip:one", "key:b")) is True
    assert app._acquire_stream(("ip:one", "key:c")) is False
    assert app._acquire_stream(("ip:two", "key:c")) is True
    assert app._acquire_stream(("ip:three", "key:d")) is False

    app._release_stream(("ip:one", "key:a"))
    assert app._acquire_stream(("ip:three", "key:d")) is True


@pytest.mark.asyncio
async def test_sse_admission_is_held_until_stream_closes_and_then_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def capture_audit(event: Any) -> None:
        return None

    async def streaming_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        entered.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = RateLimitMiddleware(
        streaming_app,
        RateLimitSettings(
            requests=100,
            sse_global_concurrency=1,
            sse_source_concurrency=1,
            sse_credential_concurrency=1,
        ),
    )
    path = "/threads/thread-1/runs/run-1/stream"

    first_task = asyncio.create_task(call_app(app, path=path, key="stream-key"))
    await entered.wait()
    rejected = await call_app(app, path=path, key="stream-key")
    assert rejected[0]["status"] == 429

    release.set()
    first = await first_task
    assert first[0]["status"] == 200
    admitted_after_close = await call_app(app, path=path, key="stream-key")
    assert admitted_after_close[0]["status"] == 200


async def test_sse_admission_is_shared_across_middleware_instances() -> None:
    backend = SharedLimitBackend()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def streaming_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        entered.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b""})

    settings = RateLimitSettings(
        requests=100,
        sse_global_concurrency=1,
        sse_source_concurrency=1,
        sse_credential_concurrency=1,
        sse_lease_ttl_s=5,
    )
    first_pod = RateLimitMiddleware(streaming_app, settings, backend=backend)
    second_pod = RateLimitMiddleware(streaming_app, settings, backend=backend)
    path = "/threads/thread-1/runs/run-1/stream"

    first_task = asyncio.create_task(call_app(first_pod, path=path, key="stream-key"))
    await entered.wait()
    rejected = await call_app(second_pod, path=path, key="stream-key")

    assert rejected[0]["status"] == 429
    release.set()
    assert (await first_task)[0]["status"] == 200
    assert (await call_app(second_pod, path=path, key="stream-key"))[0]["status"] == 200


@pytest.mark.parametrize("trusted_loopback", [False, True])
async def test_sse_admission_is_skipped_when_disabled_or_trusted(
    monkeypatch: pytest.MonkeyPatch,
    trusted_loopback: bool,
) -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(enabled=trusted_loopback))
    if trusted_loopback:
        monkeypatch.setattr(
            "apex.app.security._scope_is_trusted_loopback",
            lambda _scope: True,
        )

    def unexpected_admission(_keys: tuple[str, ...]) -> bool:
        raise AssertionError("bypassed stream must not consume public SSE capacity")

    monkeypatch.setattr(app, "_acquire_stream", unexpected_admission)

    response = await call_app(
        app,
        path="/threads/thread-1/runs/run-1/stream",
        key="internal-key",
    )

    assert response[0]["status"] == 200


@pytest.mark.parametrize("shared", [False, True], ids=["local", "shared"])
async def test_stalled_stream_body_does_not_consume_established_capacity(
    shared: bool,
) -> None:
    backend = SharedLimitBackend() if shared else None
    pending_authenticated = asyncio.Event()
    pending_chunk_read = asyncio.Event()
    established_started = asyncio.Event()
    release_established = asyncio.Event()

    async def streaming_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await mark_stream_request_authenticated(scope)
        headers = dict(scope.get("headers") or [])
        if headers.get(b"x-api-key") == b"pending-key":
            pending_authenticated.set()
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        established_started.set()
        await release_established.wait()
        await send({"type": "http.response.body", "body": b""})

    settings = RateLimitSettings(
        requests=100,
        run_create_requests=100,
        sse_global_concurrency=1,
        sse_source_concurrency=1,
        sse_credential_concurrency=1,
        sse_lease_ttl_s=5,
    )
    pending_limiter = RateLimitMiddleware(streaming_app, settings, backend=backend)
    established_limiter = (
        RateLimitMiddleware(streaming_app, settings, backend=backend) if shared else pending_limiter
    )
    body_settings = RequestBodySettings(max_bytes=1024, timeout_s=0.2)
    pending_app = RequestBodyLimitMiddleware(pending_limiter, body_settings)
    established_app = RequestBodyLimitMiddleware(established_limiter, body_settings)
    pending_messages: list[dict[str, Any]] = []
    first_chunk = True

    async def stalled_receive() -> dict[str, Any]:
        nonlocal first_chunk
        if first_chunk:
            first_chunk = False
            pending_chunk_read.set()
            return {"type": "http.request", "body": b"{", "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def pending_send(message: dict[str, Any]) -> None:
        pending_messages.append(message)

    pending_task = asyncio.create_task(
        pending_app(
            {
                "type": "http",
                "method": "POST",
                "path": "/runs/stream",
                "headers": [(b"x-api-key", b"pending-key")],
                "client": ("203.0.113.10", 12345),
            },
            stalled_receive,
            pending_send,
        )
    )
    await pending_authenticated.wait()
    await pending_chunk_read.wait()

    if backend is None:
        assert pending_limiter._stream_global == 0
    else:
        assert backend.stream_counts == {}

    established_task = asyncio.create_task(
        call_app(
            established_app,
            path="/runs/stream",
            key="established-key",
            client_ip="203.0.113.11",
            method="POST",
        )
    )
    await established_started.wait()
    if backend is None:
        assert established_limiter._stream_global == 1
    else:
        assert backend.stream_counts.get("global") == 1

    rejected = await call_app(
        pending_app,
        path="/runs/stream",
        key="other-key",
        client_ip="203.0.113.12",
        method="POST",
    )
    assert rejected[0]["status"] == 429

    release_established.set()
    assert (await established_task)[0]["status"] == 200
    await pending_task
    assert pending_messages[0]["status"] == 408


def test_rate_limit_sweeps_expired_distinct_key_buckets() -> None:
    app = RateLimitMiddleware(
        ok_app,
        RateLimitSettings(requests=10, window_s=1, auth_failures=100),
    )

    for index in range(100):
        scope = {
            "type": "http",
            "path": "/v1/system/info",
            "headers": [(b"x-api-key", f"key-{index}".encode())],
            "client": ("203.0.113.10", 12345),
        }
        assert app._check(scope, now=0.0) is None

    assert len(app._buckets) == 101  # one source bucket plus per-key buckets
    scope = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"fresh-key")],
        "client": ("203.0.113.10", 12345),
    }
    assert app._check(scope, now=2.0) is None
    assert len(app._buckets) == 2


def test_rate_limit_caps_rotating_keys_from_one_source() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    for index in range(10):
        scope = {
            "type": "http",
            "path": "/v1/system/info",
            "headers": [(b"x-api-key", f"rotated-{index}".encode())],
            "client": ("203.0.113.10", 12345),
        }
        assert app._check(scope, now=float(index) / 100) is None

    rotated = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"rotated-again")],
        "client": ("203.0.113.10", 12345),
    }
    assert app._check(rotated, now=0.2) == 59


def test_rate_limit_rejects_new_bucket_after_cap() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=10, window_s=60, max_buckets=1))
    first = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"first")],
        "client": ("203.0.113.10", 12345),
    }
    second = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"second")],
        "client": ("203.0.113.11", 12345),
    }

    assert app._check(first, now=0.0) is None
    assert app._check(second, now=0.1) == 60


def test_run_create_rate_limit_allows_keyed_request_with_one_bucket() -> None:
    app = RateLimitMiddleware(
        ok_app,
        RateLimitSettings(
            run_create_requests=2,
            run_create_window_s=60,
            max_buckets=1,
        ),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/pipelines",
        "headers": [(b"x-api-key", b"credential")],
        "client": ("203.0.113.10", 12345),
    }

    assert app._check_run_create(scope, now=0.0) is None
    assert app._check_run_create(scope, now=0.1) is None
    assert app._check_run_create(scope, now=0.2) == 59
    assert len(app._run_create_buckets) == 1


@pytest.mark.asyncio
async def test_auth_audit_locks_out_repeated_401s(monkeypatch: pytest.MonkeyPatch) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    first = await call_app(app, key="bad-key")
    second = await call_app(app, key="bad-key")
    third = await call_app(app, key="bad-key")

    assert first[0]["status"] == 401
    assert second[0]["status"] == 401
    assert third[0]["status"] == 429
    assert (b"retry-after", b"29") in third[0]["headers"] or (
        b"retry-after",
        b"30",
    ) in third[0]["headers"]


async def test_auth_lockout_is_shared_across_middleware_instances() -> None:
    backend = SharedLimitBackend()
    settings = RateLimitSettings(auth_failures=1, auth_failure_window_s=60, auth_lockout_s=30)
    first_pod = AuthAuditMiddleware(unauthorized_app, settings, backend=backend)
    second_pod = AuthAuditMiddleware(unauthorized_app, settings, backend=backend)

    first = await call_app(first_pod, key="bad-key")
    second = await call_app(second_pod, key="bad-key")

    assert first[0]["status"] == 401
    assert second[0]["status"] == 429


async def test_trusted_loopback_bypasses_local_auth_lockout_but_remains_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audited: list[Any] = []

    async def capture_audit(event: Any) -> None:
        audited.append(event)

    monkeypatch.setattr(
        "apex.app.security._scope_is_trusted_loopback",
        lambda scope: str(scope.get("path") or "").startswith("/threads/"),
    )
    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    for _ in range(3):
        loopback = await call_app(
            app,
            path="/threads/thread-1/runs",
            key="same-key",
            client_ip="127.0.0.1",
        )
        assert loopback[0]["status"] == 401

    assert app._failure_buckets == {}
    assert app._lockouts == {}

    assert (await call_app(app, key="same-key"))[0]["status"] == 401
    assert (await call_app(app, key="same-key"))[0]["status"] == 401
    assert (await call_app(app, key="same-key"))[0]["status"] == 429
    assert (
        await call_app(
            app,
            path="/threads/thread-1/runs",
            key="same-key",
            client_ip="127.0.0.1",
        )
    )[0]["status"] == 401

    await asyncio.sleep(0)
    assert sum(event.status_code == 401 for event in audited) == 6
    assert sum(event.status_code == 429 for event in audited) == 1


async def test_trusted_loopback_bypasses_shared_auth_lockout_but_remains_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audited: list[Any] = []

    async def capture_audit(event: Any) -> None:
        audited.append(event)

    monkeypatch.setattr(
        "apex.app.security._scope_is_trusted_loopback",
        lambda scope: str(scope.get("path") or "").startswith("/threads/"),
    )
    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    backend = SharedLimitBackend()
    settings = RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30)
    first_pod = AuthAuditMiddleware(unauthorized_app, settings, backend=backend)
    second_pod = AuthAuditMiddleware(unauthorized_app, settings, backend=backend)

    for pod in (first_pod, second_pod, first_pod):
        loopback = await call_app(
            pod,
            path="/threads/thread-1/runs",
            key="same-key",
            client_ip="127.0.0.1",
        )
        assert loopback[0]["status"] == 401

    assert backend.failures == {}
    assert backend.lockouts == set()

    assert (await call_app(first_pod, key="same-key"))[0]["status"] == 401
    assert (await call_app(second_pod, key="same-key"))[0]["status"] == 401
    assert (await call_app(first_pod, key="same-key"))[0]["status"] == 429
    assert (
        await call_app(
            second_pod,
            path="/threads/thread-1/runs",
            key="same-key",
            client_ip="127.0.0.1",
        )
    )[0]["status"] == 401

    await asyncio.sleep(0)
    assert sum(event.status_code == 401 for event in audited) == 6
    assert sum(event.status_code == 429 for event in audited) == 1


def test_auth_audit_lockout_expires_before_failure_sweep_window() -> None:
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(
            auth_failures=1,
            auth_failure_window_s=300,
            auth_lockout_s=30,
        ),
    )
    scope = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"bad")],
        "client": ("203.0.113.10", 12345),
    }
    app._record_auth_result(scope, status_code=401, now=1000.0)

    assert app._auth_lockout_retry_after(scope, now=1029.0) == 1
    assert app._auth_lockout_retry_after(scope, now=1030.0) is None


@pytest.mark.asyncio
async def test_auth_audit_locks_out_rotating_bad_keys_by_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    assert (await call_app(app, key="bad-one"))[0]["status"] == 401
    assert (await call_app(app, key="bad-two"))[0]["status"] == 401
    assert (await call_app(app, key="bad-three"))[0]["status"] == 429


@pytest.mark.asyncio
async def test_auth_audit_uses_forwarded_client_only_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(
            auth_failures=1,
            auth_failure_window_s=60,
            auth_lockout_s=30,
            trusted_proxy_cidrs=["10.0.0.0/8"],
        ),
    )

    first = await call_app(
        app,
        key="bad-one",
        client_ip="10.0.0.5",
        forwarded_for="198.51.100.10, 10.1.0.8",
    )
    other_client = await call_app(
        app,
        key="bad-two",
        client_ip="10.0.0.5",
        forwarded_for="198.51.100.11, 10.1.0.8",
    )

    assert first[0]["status"] == 401
    assert other_client[0]["status"] == 401
    assert "ip:198.51.100.10" in app._lockouts  # noqa: SLF001
    assert "ip:198.51.100.11" in app._lockouts  # noqa: SLF001


@pytest.mark.asyncio
async def test_auth_audit_ignores_forwarded_client_from_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(
            auth_failures=1,
            auth_failure_window_s=60,
            auth_lockout_s=30,
            trusted_proxy_cidrs=["10.0.0.0/8"],
        ),
    )

    first = await call_app(
        app,
        key="bad-one",
        client_ip="203.0.113.5",
        forwarded_for="198.51.100.10",
    )
    spoofed = await call_app(
        app,
        key="bad-two",
        client_ip="203.0.113.5",
        forwarded_for="198.51.100.11",
    )

    assert first[0]["status"] == 401
    assert spoofed[0]["status"] == 429
    assert "ip:203.0.113.5" in app._lockouts  # noqa: SLF001


@pytest.mark.asyncio
async def test_auth_audit_failure_bucket_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        unauthorized_app,
        RateLimitSettings(auth_failures=1000, max_buckets=3),
    )

    for index in range(20):
        assert (await call_app(app, key=f"bad-{index}"))[0]["status"] == 401

    assert len(app._failure_buckets) == 3


@pytest.mark.asyncio
async def test_auth_audit_success_does_not_reset_source_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    statuses = [401, 200, 401, 401]

    async def app_under_test(scope: dict[str, Any], receive: Any, send: Any) -> None:
        status = statuses.pop(0)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        app_under_test,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    assert (await call_app(app, key="flaky"))[0]["status"] == 401
    assert (await call_app(app, key="flaky"))[0]["status"] == 200
    assert (await call_app(app, key="flaky"))[0]["status"] == 401
    assert (await call_app(app, key="flaky"))[0]["status"] == 429
    assert app._auth_lockout_retry_after(  # noqa: SLF001 - focused middleware invariant
        {
            "type": "http",
            "path": "/v1/system/info",
            "headers": [(b"x-api-key", b"flaky")],
            "client": ("203.0.113.10", 12345),
        },
        now=0,
    )


@pytest.mark.asyncio
async def test_auth_audit_valid_key_does_not_reset_source_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    async def authenticate_by_key(scope: dict[str, Any], receive: Any, send: Any) -> None:
        headers = dict(scope.get("headers") or [])
        status = 200 if headers.get(b"x-api-key") == b"valid" else 401
        if status == 200:
            scope.setdefault("state", {})["identity"] = object()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        authenticate_by_key,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    assert (await call_app(app, key="bad-one"))[0]["status"] == 401
    assert (await call_app(app, key="valid"))[0]["status"] == 200
    assert (await call_app(app, key="bad-two"))[0]["status"] == 401
    assert (await call_app(app, key="bad-three"))[0]["status"] == 429


@pytest.mark.asyncio
async def test_auth_audit_unauthenticated_404_does_not_reset_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture_audit(event: Any) -> None:
        return None

    statuses = [401, 404, 401]

    async def status_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        status = statuses.pop(0)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr("apex.app.security.append_audit_event_best_effort", capture_audit)
    app = AuthAuditMiddleware(
        status_app,
        RateLimitSettings(auth_failures=2, auth_failure_window_s=60, auth_lockout_s=30),
    )

    assert (await call_app(app, key="bad"))[0]["status"] == 401
    assert (await call_app(app, key="bad"))[0]["status"] == 404
    assert (await call_app(app, key="bad"))[0]["status"] == 401
    assert (await call_app(app, key="bad"))[0]["status"] == 429


@pytest.mark.asyncio
async def test_security_headers_are_added() -> None:
    app = SecurityHeadersMiddleware(
        ok_app, SecurityHeadersSettings(content_security_policy="default-src 'none'")
    )

    messages = await call_app(app)
    headers = dict(messages[0]["headers"])

    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"content-security-policy"] == b"default-src 'none'"
    assert headers[b"strict-transport-security"] == b"max-age=31536000; includeSubDomains"


@pytest.mark.asyncio
async def test_authenticated_native_streams_are_private_no_store_across_credentials() -> None:
    async def cacheable_stream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"cache-control", b"public, max-age=3600"),
                    (b"vary", b"Origin"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"second"})

    # Cache isolation is a credential boundary, not an optional browser-header
    # feature, so it remains active even when the latter are disabled in dev.
    app = SecurityHeadersMiddleware(
        cacheable_stream,
        SecurityHeadersSettings(enabled=False),
    )

    first = await call_app(
        app,
        path="/threads/thread-1/runs/run-1/stream",
        key="principal-a",
    )
    second = await call_app(
        app,
        path="/threads/thread-1/runs/run-1/stream",
        key="principal-b",
    )

    for messages in (first, second):
        headers = dict(messages[0]["headers"])
        assert headers[b"cache-control"] == b"private, no-store"
        assert headers[b"pragma"] == b"no-cache"
        vary = {value.strip().casefold() for value in headers[b"vary"].decode().split(",")}
        assert vary == {"origin", "authorization", "x-api-key"}
        assert [message["body"] for message in messages[1:]] == [b"first", b"second"]


@pytest.mark.asyncio
async def test_bearer_authenticated_response_is_private_no_store() -> None:
    app = SecurityHeadersMiddleware(ok_app, SecurityHeadersSettings())
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/system/info",
            "headers": [(b"authorization", b"Bearer opaque-secret")],
        },
        receive,
        send,
    )

    headers = dict(messages[0]["headers"])
    assert headers[b"cache-control"] == b"private, no-store"
    assert headers[b"pragma"] == b"no-cache"


@pytest.mark.asyncio
async def test_body_limit_rejects_declared_length_before_inner_app() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("declared oversized body must not be read")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/runs",
            "headers": [(b"content-length", b"1025")],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "raw_path", "query_string"),
    [
        ("/v1/prompts/\x00", b"/v1/prompts/%00", b""),
        ("/v1/prompts", b"/v1/prompts", b"q=%00"),
    ],
)
async def test_body_middleware_rejects_nul_request_targets_before_routing(
    path: str,
    raw_path: bytes,
    query_string: bytes,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("invalid request target must not reach the app")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": raw_path,
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 400


@pytest.mark.parametrize("path", ["/v1/system/info", "/threads/thread-1"])
@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        (
            [(b"x-api-key", b"first"), (b"X-API-KEY", b"second")],
            b"Duplicate x-api-key",
        ),
        (
            [
                (b"authorization", b"Bearer first"),
                (b"Authorization", b"Bearer second"),
            ],
            b"Duplicate authorization",
        ),
        (
            [(b"x-api-key", b"first"), (b"authorization", b"Bearer second")],
            b"cannot be combined",
        ),
    ],
)
async def test_body_middleware_rejects_ambiguous_raw_credentials_on_all_surfaces(
    path: str,
    headers: list[tuple[bytes, bytes]],
    detail: bytes,
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings())
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 400
    assert detail in messages[-1]["body"]


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("/v1/prompts", "application/json"),
        ("/threads/search", "application/vnd.apex.request+json; charset=utf-8"),
    ],
)
async def test_body_middleware_rejects_deep_json_before_route_parsing(
    path: str,
    media_type: str,
) -> None:
    body = b"[" * 65 + b"0" + b"]" * 65
    chunks = [body[:31], body[31:63], body[63:]]
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        reached = True

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(chunks),
        }

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [
                (b"content-type", media_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422
    assert b"nesting must not exceed 64 levels" in messages[-1]["body"]


async def test_body_middleware_rejects_flat_json_token_amplification_before_app() -> None:
    body = ("[" + ",".join("0" for _ in range(20_001)) + "]").encode()
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1_000_000))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/search",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422
    assert b"must not exceed 20000 tokens" in messages[-1]["body"]


async def test_body_middleware_rejects_oversized_json_scalar_before_app() -> None:
    body = b'{"value":"' + b"x" * 512_001 + b'"}'
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1_000_000))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/search",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422
    assert b"JSON scalar must not exceed 512000 bytes" in messages[-1]["body"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
async def test_body_middleware_rejects_non_utf8_deep_json_before_scanning(
    encoding: str,
) -> None:
    body = ("[" * 65 + "0" + "]" * 65).encode(encoding)
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=2048))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/search",
            "headers": [
                (b"content-type", f"application/json; charset={encoding}".encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422
    assert b"JSON body must be UTF-8 encoded" in messages[-1]["body"]


async def test_body_middleware_ignores_json_delimiters_inside_strings() -> None:
    body = json.dumps({"text": '[\\"{' * 200}).encode()
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    await app(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/v1/drafts/draft-1",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


async def test_direct_langgraph_deep_json_is_bounded_without_content_type() -> None:
    body = b"{" * 1_000 + b"0" + b"}" * 1_000
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/search",
            "headers": [(b"content-length", str(len(body)).encode("ascii"))],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422
    assert b"nesting must not exceed 64 levels" in messages[-1]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/threads/thread-1/copy",
        "/threads/prune",
        "/threads/thread-1/state/checkpoint",
    ],
)
async def test_body_middleware_disables_expensive_native_thread_primitives(path: str) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("disabled native operation must not reach the runtime")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 404


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/threads", {"ttl": 60}),
        ("PATCH", "/threads/thread-1", {"ttl": {"strategy": "delete", "ttl": 60}}),
        ("POST", "/threads", {"supersteps": []}),
        ("PATCH", "/threads/thread-1", {"supersteps": [{"updates": []}]}),
    ],
)
async def test_body_middleware_rejects_public_native_thread_lifecycle_controls(
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    reached = False
    body = json.dumps(payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


async def test_body_middleware_allows_trusted_loopback_thread_lifecycle_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached = False
    body = json.dumps({"ttl": 60, "supersteps": []}).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(
        "apex.app.security._scope_is_trusted_loopback",
        lambda scope: True,
    )
    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.parametrize(
    ("path", "on_disconnect", "expected_status"),
    [
        ("/runs/stream", "cancel", 422),
        ("/runs/wait", None, 404),
        ("/threads/thread-1/runs/stream", "cancel", 422),
        ("/threads/thread-1/runs/wait", "stop", 404),
    ],
)
async def test_body_middleware_rejects_public_native_disconnect_cancellation(
    path: str,
    on_disconnect: Any,
    expected_status: int,
) -> None:
    reached = False
    body = json.dumps(
        {
            "assistant_id": "pipeline",
            "on_disconnect": on_disconnect,
            "stream_mode": "custom",
        }
    ).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == expected_status


@pytest.mark.parametrize("on_disconnect", ["continue", pytest.param("absent", id="absent")])
@pytest.mark.parametrize("stream_mode", ["custom", ["custom"]])
async def test_body_middleware_allows_safe_public_native_disconnect_behavior(
    on_disconnect: str,
    stream_mode: str | list[str],
) -> None:
    reached = False
    payload = {"assistant_id": "pipeline", "stream_mode": stream_mode}
    if on_disconnect != "absent":
        payload["on_disconnect"] = on_disconnect
    body = json.dumps(payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/thread-1/runs/stream",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.parametrize(
    "query_string",
    [
        b"stream_mode=custom&cancel_on_disconnect=true",
        b"stream_mode=custom&cancel_on_disconnect=1",
        b"stream_mode=custom&cancel_on_disconnect=",
        b"stream_mode=custom&cancel_on_disconnect=false&cancel_on_disconnect=false",
    ],
)
async def test_body_middleware_rejects_run_stream_disconnect_query_controls(
    query_string: bytes,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("unsafe query must be rejected before routing")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/runs/run-1/stream",
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize(
    "query_string",
    [b"stream_mode=custom", b"stream_mode=custom&cancel_on_disconnect=false"],
)
@pytest.mark.parametrize(
    "path",
    ["/threads/thread-1/runs/run-1/stream", "/runs/run-1/stream"],
)
async def test_body_middleware_allows_non_cancelling_run_stream_query(
    query_string: bytes,
    path: str,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.parametrize(
    "headers",
    [
        [(b"last-event-id", b"not-a-cursor")],
        [(b"last-event-id", b"1-0"), (b"Last-Event-ID", b"2-0")],
        [(b"last-event-id", b"1-0-extra")],
        [(b"last-event-id", b"1" * 65)],
    ],
)
async def test_body_middleware_rejects_ambiguous_or_invalid_last_event_id(
    headers: list[tuple[bytes, bytes]],
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        raise AssertionError("invalid cursor must be rejected before routing")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/runs/run-1/stream",
            "query_string": b"stream_mode=custom",
            "headers": headers,
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize("last_event_id", [b"-", b"123", b"123-0", b"123-*"])
async def test_body_middleware_allows_bounded_last_event_id(last_event_id: bytes) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        raise AssertionError("the test runtime emits no response")

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/runs/run-1/stream",
            "query_string": b"stream_mode=custom",
            "headers": [(b"last-event-id", last_event_id)],
        },
        receive,
        send,
    )

    assert reached is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/threads/thread-1"),
        ("GET", "/threads/thread-1/state"),
        ("GET", "/threads/thread-1/state/checkpoint-1"),
        ("GET", "/threads/thread-1/history"),
        ("POST", "/threads/thread-1/history"),
        ("GET", "/threads/thread-1/runs/run-1"),
        ("GET", "/threads/thread-1/runs/run-1/join"),
        ("GET", "/runs/run-1"),
        ("GET", "/runs/run-1/join"),
        ("POST", "/threads/thread-1/runs/wait"),
        ("POST", "/runs/wait"),
        ("POST", "/threads/thread-1/runs/run-1/cancel"),
        ("POST", "/runs/cancel"),
        ("DELETE", "/threads/thread-1"),
        ("DELETE", "/threads/thread-1/runs/run-1"),
        ("POST", "/threads/thread-1/state"),
        ("PATCH", "/threads/thread-1/state"),
        ("POST", "/assistants/assistant-1/versions"),
        ("POST", "/assistants/assistant-1/latest"),
    ],
)
async def test_public_native_unsafe_state_operations_are_hidden(method: str, path: str) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        raise AssertionError("blocked native read must not consume a request body")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 404


async def test_trusted_loopback_can_read_native_checkpoint_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    monkeypatch.setattr("apex.app.security._scope_is_trusted_loopback", lambda _scope: True)
    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/state",
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.parametrize(
    "query_string",
    [
        b"delete_threads=true",
        b"delete_threads=1",
        b"delete_threads=false&delete_threads=false",
        b"delete_threads=false&cascade=true",
    ],
)
async def test_public_assistant_delete_cannot_cascade_to_threads(
    query_string: bytes,
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        raise AssertionError("unsafe assistant deletion must not consume a body")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/assistants/assistant-1",
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize(
    "query_string",
    [
        b"",
        b"stream_mode=updates",
        b"stream_mode=custom&stream_mode=custom",
        b"stream_mode=custom&stream_mode=updates",
        b"stream_mode=custom,updates",
        b"streamMode=custom",
    ],
)
@pytest.mark.parametrize(
    "path",
    ["/threads/thread-1/runs/run-1/stream", "/runs/run-1/stream"],
)
async def test_public_join_stream_requires_exact_canonical_custom_mode(
    query_string: bytes,
    path: str,
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, Any]:
        raise AssertionError("unsafe stream mode must not reach the runtime")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"assistant_id": "pipeline"},
        {"assistant_id": "pipeline", "stream_mode": "updates"},
        {"assistant_id": "pipeline", "stream_mode": "messages"},
        {"assistant_id": "pipeline", "stream_mode": ["custom", "updates"]},
        {"assistant_id": "pipeline", "stream_mode": ["custom", "messages"]},
        {"assistant_id": "pipeline", "stream_mode": ["custom"] * 2},
        {"assistant_id": "pipeline", "streamMode": "custom"},
    ],
)
@pytest.mark.parametrize(
    "content_type",
    [b"application/json", b"text/plain", b"not-a-media-type", None],
    ids=["json", "plain", "malformed", "absent"],
)
@pytest.mark.parametrize(
    "path",
    [
        "/threads/thread-1/runs/stream",
        "/threads/thread-1/runs/stream/",
        "/runs/stream",
        "/runs/stream/",
    ],
)
async def test_public_create_stream_requires_exact_custom_mode_for_any_content_type(
    payload: dict[str, Any],
    content_type: bytes | None,
    path: str,
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []
    body = json.dumps(payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"content-length", str(len(body)).encode())]
    if content_type is not None:
        headers.append((b"content-type", content_type))

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize(
    ("method", "path", "content_location", "reconnect_location"),
    [
        (
            "GET",
            "/threads/thread-1/runs/run-1/stream",
            b"/threads/thread-1/runs/run-1",
            b"/threads/thread-1/runs/run-1/stream",
        ),
        (
            "GET",
            "/threads/thread-1/runs/run-1/stream/",
            b"/threads/thread-1/runs/run-1",
            b"/threads/thread-1/runs/run-1/stream",
        ),
        ("GET", "/runs/run-1/stream", b"/runs/run-1", b"/runs/run-1/stream"),
        ("GET", "/runs/run-1/stream/", b"/runs/run-1", b"/runs/run-1/stream"),
        (
            "POST",
            "/threads/thread-1/runs/stream",
            b"/threads/thread-1/runs/run-1",
            b"/threads/thread-1/runs/run-1/stream",
        ),
        (
            "POST",
            "/threads/thread-1/runs/stream/",
            b"/threads/thread-1/runs/run-1",
            b"/threads/thread-1/runs/run-1/stream",
        ),
        ("POST", "/runs/stream", b"/runs/run-1", b"/runs/run-1/stream"),
        ("POST", "/runs/stream/", b"/runs/run-1", b"/runs/run-1/stream"),
    ],
)
async def test_public_custom_stream_drops_raw_state_and_sanitizes_errors(
    method: str,
    path: str,
    content_location: bytes,
    reconnect_location: bytes,
) -> None:
    secret = b"stream-provider-token-canary"
    messages: list[dict[str, Any]] = []
    request_body = json.dumps({"assistant_id": "pipeline", "stream_mode": "custom"}).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if method == "POST":
            assert (await receive())["body"] == request_body
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "trailers": True,
                "headers": [
                    (b"content-type", b"text/event-stream; runtime-secret=1"),
                    (b"content-location", content_location),
                    (b"location", reconnect_location),
                    (b"content-length", b"999999"),
                    (b"set-cookie", b"session=" + secret),
                    (b"x-runtime-state", b"password=" + secret),
                ],
            }
        )
        payload = (
            b": password="
            + secret
            + b"\n\n"
            + b'event: custom\ndata: "'
            + secret
            + b'"\n\n'
            + b'event: values\ndata: {"engine_handle":{"provider_token":"'
            + secret
            + b'"}}\n\n'
            + b"event: custom\nid: password="
            + secret
            + b'\ndata: {"provider_token":"'
            + secret
            + b'","safe":"kept"}\n\n'
            + b'event: updates\ndata: {"run_config":"'
            + secret
            + b'"}\n\n'
            + b"event: error\ndata: Authorization: Bearer "
            + secret
            + b"\n\n"
        )
        midpoint = payload.index(secret) + 3
        await send({"type": "http.response.body", "body": payload[:midpoint], "more_body": True})
        await send({"type": "http.response.body", "body": payload[midpoint:]})
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"x-runtime-trailer", b"password=" + secret)],
                "more_trailers": False,
            }
        )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"stream_mode=custom" if method == "GET" else b"",
            "headers": [(b"content-length", str(len(request_body)).encode())]
            if method == "POST"
            else [],
        },
        receive,
        send,
    )
    body = b"".join(message.get("body", b"") for message in messages)

    assert secret not in body
    assert b"event: values" not in body
    assert b"event: updates" not in body
    assert b"event: custom" in body
    assert b"id:" not in body
    assert b'"safe":"kept"' in body
    assert b"inspect the pipeline snapshot" in body
    assert b": heartbeat\n\n" in body
    assert messages[0]["headers"] == [
        (b"content-type", b"text/event-stream"),
        (b"content-location", content_location),
        (b"location", reconnect_location + b"?stream_mode=custom"),
    ]
    assert "trailers" not in messages[0]
    assert secret not in repr(messages).encode()


async def test_public_native_error_response_never_reflects_rejected_input() -> None:
    secret = b"native-validation-secret-canary"
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 422,
                "trailers": True,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-error-detail", b"password=" + secret),
                    (b"set-cookie", b"session=" + secret),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"password=' + secret[:8],
                "more_body": True,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": secret[8:] + b'"}',
                "more_body": False,
            }
        )
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"x-error-trailer", b"password=" + secret)],
                "more_trailers": False,
            }
        )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/assistants/assistant-1",
            "headers": [],
        },
        receive,
        send,
    )
    body = b"".join(message.get("body", b"") for message in messages)

    assert messages[0]["status"] == 422
    assert secret not in body
    assert b"could not be completed" in body
    headers = dict(messages[0]["headers"])
    assert headers == {b"content-type": b"application/problem+json"}
    assert "trailers" not in messages[0]


async def test_public_thread_metadata_update_never_returns_native_state() -> None:
    secret = b"thread-update-state-secret-canary"
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "trailers": True,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-thread-state", b"password=" + secret),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"thread_id":"thread-1","values":{"provider_token":"' + secret[:8],
                "more_body": True,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": secret[8:] + b'"}}',
                "more_body": False,
            }
        )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/threads/thread-1",
            "headers": [(b"content-length", b"2")],
        },
        receive,
        send,
    )

    body = b"".join(message.get("body", b"") for message in messages)
    assert messages[0]["status"] == 204
    assert messages[0]["headers"] == []
    assert "trailers" not in messages[0]
    assert body == b""
    assert secret not in repr(messages).encode()


@pytest.mark.parametrize(
    "payload",
    [
        {"thread_id": "caller-selected"},
        {"if_exists": "do_nothing"},
        {"thread_id": "existing-thread", "if_exists": "do_nothing"},
    ],
)
async def test_public_thread_create_rejects_caller_identity_and_collision_controls(
    payload: dict[str, Any],
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []
    body = json.dumps(payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.parametrize(
    ("method", "path", "query_string", "request_payload", "runtime_payload", "expected"),
    [
        (
            "POST",
            "/threads",
            b"",
            {},
            {
                "thread_id": "thread-1",
                "status": "idle",
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {"api_key": "native-projection-secret-canary"},
                "values": {"provider_token": "native-projection-secret-canary"},
                "interrupts": [{"value": "native-projection-secret-canary"}],
            },
            {
                "thread_id": "thread-1",
                "status": "idle",
                "created_at": "2026-01-01T00:00:00Z",
            },
        ),
        (
            "POST",
            "/runs",
            b"",
            {"assistant_id": "pipeline", "input": {}},
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "pipeline",
                "status": "pending",
                "multitask_strategy": "reject",
                "kwargs": {"config": {"api_key": "native-projection-secret-canary"}},
            },
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "pipeline",
                "status": "pending",
                "multitask_strategy": "reject",
            },
        ),
        (
            "POST",
            "/threads/thread-1/runs",
            b"",
            {"assistant_id": "pipeline", "input": {}},
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "pipeline",
                "status": "pending",
                "metadata": {"provider_token": "native-projection-secret-canary"},
            },
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "assistant_id": "pipeline",
                "status": "pending",
            },
        ),
        (
            "GET",
            "/threads/thread-1/runs",
            b"limit=1&select=run_id&select=status",
            None,
            [
                {
                    "run_id": "run-1",
                    "status": "success",
                    "kwargs": {"token": "native-projection-secret-canary"},
                }
            ],
            [{"run_id": "run-1", "status": "success"}],
        ),
        (
            "POST",
            "/threads/search",
            b"",
            {"limit": 1, "select": ["thread_id", "status"]},
            [
                {
                    "thread_id": "thread-1",
                    "status": "idle",
                    "values": {"password": "native-projection-secret-canary"},
                }
            ],
            [{"thread_id": "thread-1", "status": "idle"}],
        ),
        (
            "POST",
            "/assistants/search",
            b"",
            {"limit": 1, "select": ["assistant_id", "name"]},
            [
                {
                    "assistant_id": "assistant-1",
                    "name": "Pipeline",
                    "config": {"configurable": {"api_key": "native-projection-secret-canary"}},
                    "context": {"note": "native-projection-secret-canary"},
                }
            ],
            [{"assistant_id": "assistant-1", "name": "Pipeline"}],
        ),
    ],
)
async def test_public_native_successes_use_strict_server_owned_projections(
    method: str,
    path: str,
    query_string: bytes,
    request_payload: dict[str, Any] | None,
    runtime_payload: Any,
    expected: Any,
) -> None:
    secret = b"native-projection-secret-canary"
    messages: list[dict[str, Any]] = []
    request_body = json.dumps(request_payload).encode() if request_payload is not None else b""
    runtime_body = json.dumps(runtime_payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if method == "POST":
            assert (await receive())["body"] == request_body
        content_location = (
            b"/threads/thread-1/runs/run-1" if path.startswith("/threads/") else b"/runs/run-1"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "trailers": True,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(runtime_body)).encode()),
                    (b"content-location", content_location),
                    (b"set-cookie", b"session=" + secret),
                    (b"x-runtime-state", b"password=" + secret),
                ],
            }
        )
        split = runtime_body.index(secret) + 5
        await send({"type": "http.response.body", "body": runtime_body[:split], "more_body": True})
        await send({"type": "http.response.body", "body": runtime_body[split:]})
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"x-runtime-trailer", b"password=" + secret)],
                "more_trailers": False,
            }
        )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": [(b"content-length", str(len(request_body)).encode())]
            if method == "POST"
            else [],
        },
        receive,
        send,
    )

    response_body = b"".join(message.get("body", b"") for message in messages)
    assert json.loads(response_body) == expected
    assert secret not in repr(messages).encode()
    expected_headers = [(b"content-type", b"application/json")]
    if method == "POST" and (path == "/runs" or path.endswith("/runs")):
        expected_headers.append(
            (
                b"content-location",
                b"/threads/thread-1/runs/run-1" if path.startswith("/threads/") else b"/runs/run-1",
            )
        )
    assert messages[0]["headers"] == expected_headers
    assert "trailers" not in messages[0]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/assistants/assistant-1"),
        ("PATCH", "/assistants/assistant-1"),
        ("POST", "/assistants"),
    ],
)
async def test_public_assistant_response_redacts_legacy_credential_subtrees(
    method: str,
    path: str,
) -> None:
    secret = b"legacy-assistant-secret-canary"
    messages: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"999"),
                    (b"x-assistant-config", b"password=" + secret),
                ],
            }
        )
        payload = json.dumps(
            {
                "assistant_id": "assistant-1",
                "graph_id": "pipeline",
                "config": {
                    "configurable": {
                        "phases": ["reporting"],
                        "api_key": secret.decode(),
                        "cookie": secret.decode(),
                        "set-cookie": secret.decode(),
                        "passphrase": secret.decode(),
                        "private_key": secret.decode(),
                        "privateKey": secret.decode(),
                        "signing_key": secret.decode(),
                        "encryption_key": secret.decode(),
                        "connection_string": secret.decode(),
                        "connectionString": secret.decode(),
                        "database_url": secret.decode(),
                        "database_uri": secret.decode(),
                        "dsn": secret.decode(),
                        "stripeApiKey": secret.decode(),
                        "serviceAccountPrivateKey": secret.decode(),
                        "databasePassword": secret.decode(),
                        "oauthRefreshToken": secret.decode(),
                        "sessionCookie": secret.decode(),
                        "cookieJar": secret.decode(),
                    }
                },
                "context": {"note": f"Authorization: Bearer {secret.decode()}"},
                "metadata": {"project_id": "p1"},
            }
        ).encode()
        split = payload.index(secret) + 4
        await send({"type": "http.response.body", "body": payload[:split], "more_body": True})
        await send({"type": "http.response.body", "body": payload[split:]})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
        },
        receive,
        send,
    )
    body = b"".join(message.get("body", b"") for message in messages)
    projected = json.loads(body)

    assert secret not in body
    for credential_field in {
        "api_key",
        "cookie",
        "set-cookie",
        "passphrase",
        "private_key",
        "privateKey",
        "signing_key",
        "encryption_key",
        "connection_string",
        "connectionString",
        "database_url",
        "database_uri",
        "dsn",
        "stripeApiKey",
        "serviceAccountPrivateKey",
        "databasePassword",
        "oauthRefreshToken",
        "sessionCookie",
        "cookieJar",
    }:
        assert credential_field not in projected["config"]["configurable"]
    assert projected["config"]["configurable"]["phases"] == ["reporting"]
    assert projected["context"]["note"] == "Authorization: [REDACTED]"
    headers = dict(messages[0]["headers"])
    assert headers == {b"content-type": b"application/json"}


@pytest.mark.parametrize(
    ("path", "select"),
    [
        ("/threads/search", ["thread_id", "metadata"]),
        ("/assistants/search", ["assistant_id", "metadata"]),
    ],
)
async def test_public_native_search_cannot_select_arbitrary_metadata(
    path: str,
    select: list[str],
) -> None:
    reached = False
    messages: list[dict[str, Any]] = []
    body = json.dumps({"limit": 1, "select": select}).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.asyncio
async def test_body_middleware_rejects_unbounded_get_thread_history_limit() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("unbounded history request must not reach the app")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/history",
            "query_string": b"limit=6",
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 404


@pytest.mark.asyncio
async def test_body_middleware_rejects_oversized_get_history_checkpoint() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("oversized checkpoint must not reach the runtime")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/history",
            "query_string": b"before=" + b"x" * 256,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_string",
    [
        b"limit=101&select=run_id&select=status",
        b"limit=-1&select=run_id&select=status",
        b"limit=abc&select=run_id&select=status",
        b"limit=1&limit=2&select=run_id&select=status",
        b"offset=10001&select=run_id&select=status",
        b"offset=-1&select=run_id&select=status",
        b"offset=abc&select=run_id&select=status",
        b"offset=1&offset=2&select=run_id&select=status",
        b"status=bogus&select=run_id&select=status",
        b"status=running&status=success&select=run_id&select=status",
    ],
)
async def test_body_middleware_rejects_unbounded_thread_run_list(
    query_string: bytes,
) -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("unbounded run-list request must not reach the app")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/runs",
            "query_string": query_string,
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.asyncio
async def test_body_middleware_allows_projected_thread_run_list() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"[]"})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/threads/thread-1/runs",
            "query_string": b"limit=10&status=running&select=run_id&select=status",
            "headers": [],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.asyncio
async def test_body_middleware_rejects_oversized_direct_run_batch() -> None:
    reached = False
    body = json.dumps([{"assistant_id": "pipeline"} for _ in range(26)]).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/runs/batch",
            "query_string": b"",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.asyncio
async def test_body_middleware_rejects_even_small_direct_run_batch() -> None:
    reached = False
    body = json.dumps([{"assistant_id": "pipeline"} for _ in range(25)]).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/runs/batch",
            "query_string": b"",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/threads/search", {"limit": 11, "select": ["thread_id"]}),
        (
            "/threads/search",
            {"limit": 10, "offset": 10_001, "select": ["thread_id"]},
        ),
        (
            "/threads/search",
            {
                "ids": [f"id-{index}" for index in range(101)],
                "select": ["thread_id"],
            },
        ),
        ("/threads/thread-1/history", {"limit": 6}),
        ("/threads/thread-1/history", {"metadata": {"note": "bad\x00filter"}}),
        (
            "/threads/thread-1/history",
            {"before": {"checkpoint_id": "x" * 256}},
        ),
        (
            "/threads/thread-1/history",
            {"checkpoint": {"checkpoint_map": {"branch": "checkpoint"}}},
        ),
        (
            "/threads/thread-1/history",
            {"checkpoint": {"unexpected": "value"}},
        ),
        ("/assistants/search", {"limit": 6, "select": ["assistant_id"]}),
        (
            "/assistants/search",
            {"offset": 10_001, "select": ["assistant_id"]},
        ),
        (
            "/assistants/search",
            {"metadata": {"bad\x00key": "value"}, "select": ["assistant_id"]},
        ),
        ("/assistants/search", {"limit": 5}),
        ("/assistants/search", {"limit": 5, "select": ["context"]}),
        ("/assistants/search", {"name": "x" * 256, "select": ["assistant_id"]}),
        ("/assistants/count", {"graph_id": "x" * 256}),
        ("/assistants/assistant-1/versions", {"limit": 3}),
        ("/assistants/assistant-1/versions", {"offset": 10_001}),
        (
            "/assistants/assistant-1/versions",
            {"metadata": {"note": "bad\x00filter"}},
        ),
    ],
)
async def test_body_middleware_rejects_unbounded_direct_langgraph_reads(
    path: str,
    payload: dict[str, Any],
) -> None:
    reached = False
    body = json.dumps(payload).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is False
    expected_status = 404 if "/history" in path or path.endswith("/versions") else 422
    assert messages[0]["status"] == expected_status


@pytest.mark.asyncio
async def test_body_middleware_counts_unknown_checkpoint_fields_without_reflecting_names() -> None:
    reached = False
    canary = "CANARY_CHECKPOINT_CONFIG_SECRET"
    checkpoint = {
        **{f"unknown_{index}": index for index in range(100)},
        f"{canary}_{'x' * 512}": True,
    }
    body = json.dumps({"checkpoint": checkpoint}).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        await receive()
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=100_000))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/thread-1/history",
            "query_string": b"",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    assert reached is False
    assert messages[0]["status"] == 404
    assert canary.encode() not in response_body
    assert len(response_body) <= 512


@pytest.mark.asyncio
async def test_body_middleware_allows_bounded_direct_langgraph_search() -> None:
    reached = False
    body = json.dumps(
        {
            "limit": 10,
            "offset": 10_000,
            "ids": ["id-1"],
            "select": ["thread_id", "status"],
        }
    ).encode()

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        message = await receive()
        assert message["body"] == body
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/search",
            "query_string": b"",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200


@pytest.mark.asyncio
async def test_body_middleware_rejects_malformed_langsmith_baggage_before_runtime() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("malformed tracing baggage must not reach the runtime")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/thread-1/runs",
            "query_string": b"",
            "headers": [
                (b"langsmith-trace", b"trace-id"),
                (b"baggage", b"langsmith-metadata=not-json"),
            ],
        },
        receive,
        send,
    )

    assert reached is False
    assert messages[0]["status"] == 400


@pytest.mark.asyncio
async def test_body_middleware_allows_bounded_langsmith_metadata() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=4096))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/threads/thread-1/runs",
            "query_string": b"",
            "headers": [
                (b"langsmith-trace", b"trace-id"),
                (b"baggage", b"langsmith-metadata=%7B%22project%22%3A%22apex%22%7D"),
            ],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
async def test_body_limit_rejects_chunked_body_before_route_effect() -> None:
    route_effect = False
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 800, "more_body": True},
            {"type": "http.request", "body": b"b" * 300, "more_body": False},
        ]
    )

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal route_effect
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        route_effect = True

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1024))
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(chunks)

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {"type": "http", "method": "POST", "path": "/threads", "headers": []},
        receive,
        send,
    )

    assert route_effect is False
    assert messages[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_limit_rejects_drip_fed_body_at_one_overall_deadline() -> None:
    route_effect = False
    first_chunk = True

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal route_effect
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        route_effect = True

    app = RequestBodyLimitMiddleware(
        inner,
        RequestBodySettings(max_bytes=1024, timeout_s=0.02),
    )
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal first_chunk
        if first_chunk:
            first_chunk = False
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {"type": "http", "method": "POST", "path": "/threads", "headers": []},
        receive,
        send,
    )

    assert route_effect is False
    assert messages[0]["status"] == 408
    assert b"within 0.02 seconds" in messages[1]["body"]


@pytest.mark.asyncio
async def test_body_limit_allows_document_upload_override() -> None:
    reached = False

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(
        inner,
        RequestBodySettings(max_bytes=1024, document_upload_max_bytes=2048),
    )
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/documents",
            "headers": [(b"content-length", b"1500")],
        },
        receive,
        send,
    )

    assert reached is True
    assert messages[0]["status"] == 200
