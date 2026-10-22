"""Request guards survive the task-group boundaries used by native routes."""

import asyncio
import json
from typing import Any

import anyio
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router

from apex.app import security as security_module
from apex.app.security import RequestBodyLimitMiddleware
from apex.settings import RequestBodySettings


async def invoke(app: Any, body: bytes, messages: list[dict[str, Any]], *, stalled: bool = False):
    async def receive():
        if stalled:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/assistants/search",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )


@pytest.mark.parametrize("depth", [1, 3])
@pytest.mark.parametrize(
    ("body", "stalled", "status"),
    [
        (b'{"limit":6,"select":["assistant_id"]}', False, 422),
        (b'{"limit":5}', False, 422),
        (b'{"secret-canary":' + b"[" * 65 + b"0" + b"]" * 65 + b"}", False, 422),
        (b"x" * 1_025, False, 413),
        (b"", True, 408),
        (b'{"limit":5,"select":["assistant_id"]}', False, 200),
    ],
    ids=["excessive-limit", "missing-select", "too-deep", "oversized-body", "stalled", "valid"],
)
async def test_native_read_guards_across_real_anyio_task_groups(body, stalled, status, depth):
    route_effects = []

    async def endpoint(request: Request):
        async def read(level: int):
            if level:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(read, level - 1)
            else:
                route_effects.append(await request.json())

        await read(depth)
        return JSONResponse([])

    route_app: Any = Router([Route("/assistants/search", endpoint, methods=["POST"])])
    app = RequestBodyLimitMiddleware(
        route_app,
        RequestBodySettings(max_bytes=1_024, timeout_s=0.02),
    )
    messages: list[dict[str, Any]] = []
    await invoke(app, body, messages, stalled=stalled)
    assert [
        message["status"] for message in messages if message["type"] == "http.response.start"
    ] == [status]
    assert bool(route_effects) is (status == 200)
    response = b"".join(message.get("body", b"") for message in messages)
    assert b"secret-canary" not in response
    if status != 200:
        assert json.loads(response)["status"] == status
        assert (b"content-type", b"application/problem+json") in messages[0]["headers"]
    else:
        assert route_effects == [json.loads(body)]
        assert response == b"[]"


@pytest.mark.parametrize("extra", [None, RuntimeError("private failure"), asyncio.CancelledError()])
async def test_nested_groups_do_not_hide_unrelated_failures_or_cancellation(extra):
    raised_group = None

    async def inner(scope, receive, send):
        nonlocal raised_group
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(receive)
        except BaseExceptionGroup as rejected:
            raised_group = BaseExceptionGroup(
                "private wrapper must not be disclosed",
                [rejected, *([extra] if extra is not None else [])],
            )
            raise raised_group from rejected

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1_024))
    messages: list[dict[str, Any]] = []
    if extra is None:
        await invoke(app, b'{"limit":6}', messages)
        assert messages[0]["status"] == 422
        assert b"private" not in messages[1]["body"]
    else:
        with pytest.raises(BaseExceptionGroup) as raised:
            await invoke(app, b'{"limit":6}', messages)
        assert raised.value is raised_group
        assert messages == []


@pytest.mark.parametrize("status", [200, 403, 500])
async def test_grouped_rejection_after_response_start_does_not_send_a_second_response(status):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(receive)

    app = RequestBodyLimitMiddleware(inner, RequestBodySettings(max_bytes=1_024))
    messages: list[dict[str, Any]] = []
    with pytest.raises(BaseExceptionGroup):
        await invoke(app, b'{"limit":6}', messages)
    assert len(messages) == 1
    assert messages[0]["status"] == status


async def test_grouped_non_guard_failure_is_re_raised_unchanged():
    failure = ExceptionGroup("server failure", [ValueError("private detail")])

    async def inner(scope, receive, send):
        raise failure

    messages: list[dict[str, Any]] = []
    with pytest.raises(ExceptionGroup) as raised:
        await invoke(RequestBodyLimitMiddleware(inner, RequestBodySettings()), b"{}", messages)
    assert raised.value is failure
    assert messages == []


async def test_multiple_guard_rejections_use_the_first_failure_without_wrapper_disclosure():
    failure = ExceptionGroup(
        "private wrapper",
        [
            ExceptionGroup("nested private wrapper", [security_module._RequestBodyTooLarge()]),
            security_module._RequestInputRejected("Request input is invalid"),
        ],
    )

    async def inner(scope, receive, send):
        raise failure

    messages: list[dict[str, Any]] = []
    await invoke(RequestBodyLimitMiddleware(inner, RequestBodySettings()), b"{}", messages)
    assert messages[0]["status"] == 413
    assert b"private" not in messages[1]["body"]
