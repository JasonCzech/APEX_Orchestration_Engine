from typing import Any

import pytest

from apex.app.security import RateLimitMiddleware, SecurityHeadersMiddleware
from apex.settings import RateLimitSettings, SecurityHeadersSettings


async def ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def call_app(app: Any, *, path: str = "/v1/system/info", key: str = "k") -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"x-api-key", key.encode("utf-8"))],
            "client": ("203.0.113.10", 12345),
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


def test_rate_limit_sweeps_expired_distinct_key_buckets() -> None:
    app = RateLimitMiddleware(ok_app, RateLimitSettings(requests=10, window_s=1))

    for index in range(100):
        scope = {
            "type": "http",
            "path": "/v1/system/info",
            "headers": [(b"x-api-key", f"key-{index}".encode())],
            "client": ("203.0.113.10", 12345),
        }
        assert app._check(scope, now=0.0) is None

    assert len(app._buckets) == 100
    scope = {
        "type": "http",
        "path": "/v1/system/info",
        "headers": [(b"x-api-key", b"fresh-key")],
        "client": ("203.0.113.10", 12345),
    }
    assert app._check(scope, now=2.0) is None
    assert len(app._buckets) == 1


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
async def test_security_headers_are_added() -> None:
    app = SecurityHeadersMiddleware(
        ok_app, SecurityHeadersSettings(content_security_policy="default-src 'none'")
    )

    messages = await call_app(app)
    headers = dict(messages[0]["headers"])

    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"content-security-policy"] == b"default-src 'none'"
