from typing import Any

import pytest

from apex.app.security import AuthAuditMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from apex.settings import RateLimitSettings, SecurityHeadersSettings


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
            "method": "GET",
            "path": path,
            "headers": headers,
            "client": (client_ip, 12345),
        },
        receive,
        send,
    )
    return messages


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_limit() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    first = await call_app(app)
    second = await call_app(app)

    assert first[0]["status"] == 200
    assert second[0]["status"] == 429
    assert (b"content-type", b"application/problem+json") in second[0]["headers"]


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
async def test_rate_limit_ignores_unprotected_paths() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=1, window_s=60))

    first = await call_app(app, path="/health")
    second = await call_app(app, path="/health")

    assert first[0]["status"] == 200
    assert second[0]["status"] == 200


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
