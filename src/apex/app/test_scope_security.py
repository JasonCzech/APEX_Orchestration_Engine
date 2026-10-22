"""All-participant access barrier for native and facade run surfaces."""

import json
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from apex.auth.identity import Role
from apex.auth.service import extract_api_key, get_default_resolver
from apex.services.test_scope import accessible_thread_ids, can_access_thread

MAX_SEARCH_SCAN = 10_000
SEARCH_PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 8_000_000


class TestScopeAccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "").rstrip("/")
        if path in {"/v1/pipelines/analysis/connections", "/v1/pipelines/execution-readiness"}:
            return await self.app(scope, receive, send)
        match = re.match(r"^/(?:threads|v1/pipelines)/([^/]+)(?:/|$)", path)
        thread_id = (
            match.group(1)
            if match and match.group(1) not in {"search", "count", "analysis-connections"}
            else None
        )
        search = path in {"/threads/search", "/threads/count"} and scope.get("method") == "POST"
        if not thread_id and not search:
            return await self.app(scope, receive, send)
        try:
            identity = await get_default_resolver().resolve(extract_api_key(Headers(scope=scope)))
            if identity is None or identity.is_unscoped:
                return await self.app(scope, receive, send)
            if thread_id:
                # Reject insufficient roles before opening projection sessions or
                # making native preflight requests. Native history/checkpoint POSTs
                # are reads; the remaining thread mutations require an operator.
                mutation = scope.get("method") in {"POST", "PATCH", "PUT", "DELETE"}
                if path in {
                    f"/threads/{thread_id}/history",
                    f"/threads/{thread_id}/state/checkpoint",
                }:
                    mutation = False
                if mutation and not identity.role.at_least(Role.OPERATOR):
                    return await JSONResponse(
                        {"detail": "Requires role 'operator' or higher"}, status_code=403
                    )(scope, receive, send)
                if not await can_access_thread(identity, thread_id):
                    return await self._not_found(scope, receive, send)
                # A missing projection is legacy only when the native thread also
                # has no new scope marker. Read bounded metadata before any stream
                # bytes or mutating request reach the target route.
                preflight_scope = {
                    **scope,
                    "method": "POST",
                    "path": "/threads/search",
                    "raw_path": b"/threads/search",
                    "query_string": b"",
                }
                preflight = await self._capture(
                    preflight_scope,
                    json.dumps(
                        {
                            "ids": [thread_id],
                            "limit": 1,
                            "select": ["thread_id", "metadata"],
                        }
                    ).encode(),
                )
                if not 200 <= preflight["status"] < 300:
                    return await self._unavailable(scope, receive, send)
                candidates = json.loads(preflight["body"])
                if not isinstance(candidates, list) or len(candidates) > 1:
                    raise ValueError("Invalid scope metadata preflight")
                if not candidates:
                    return await self._not_found(scope, receive, send)
                candidate = candidates[0]
                if candidate.get("thread_id") != thread_id or not isinstance(
                    candidate.get("metadata"), dict
                ):
                    raise ValueError("Invalid scope metadata preflight")
                if not await can_access_thread(
                    identity, thread_id, expected_scope=candidate["metadata"].get("test_scope")
                ):
                    return await self._not_found(scope, receive, send)
        except Exception:
            return await self._unavailable(scope, receive, send)
        if not search:
            return await self.app(scope, receive, send)
        try:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Run search exceeds request budget")
                if not message.get("more_body"):
                    break
            # Preserve native validation and authorization, including invalid original
            # pagination controls. No original response bytes or counts escape.
            original = await self._capture(scope, bytes(body))
            if not 200 <= original["status"] < 300:
                return await self._respond(original, scope, receive, send)
            payload = json.loads(body)
            counting = path == "/threads/count"
            limit = payload.get("limit", 10)
            offset = payload.get("offset", 0)
            if not counting and (
                type(limit) is not int
                or not 1 <= limit <= SEARCH_PAGE_SIZE
                or type(offset) is not int
                or not 0 <= offset <= MAX_SEARCH_SCAN
            ):
                raise ValueError("Invalid run search pagination")
            selected = payload.get("select")
            page_payload = dict(payload)
            if counting:
                page_payload["select"] = ["thread_id", "metadata"]
            elif selected:
                page_payload["select"] = list(dict.fromkeys([*selected, "thread_id", "metadata"]))
            page_payload["limit"] = SEARCH_PAGE_SIZE
            page_scope = {**scope, "path": "/threads/search", "raw_path": b"/threads/search"}
            visible_count = 0
            visible = []
            exhausted = False
            for scan_offset in range(0, MAX_SEARCH_SCAN, SEARCH_PAGE_SIZE):
                page_payload["offset"] = scan_offset
                page = await self._capture(page_scope, json.dumps(page_payload).encode())
                if not 200 <= page["status"] < 300:
                    return await self._respond(page, scope, receive, send)
                items = json.loads(page["body"])
                if not isinstance(items, list) or len(items) > SEARCH_PAGE_SIZE:
                    raise ValueError("Invalid native search page")
                expected_scopes = {}
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("thread_id"), str):
                        raise ValueError("Invalid native search identity")
                    if not isinstance(item.get("metadata"), dict):
                        raise ValueError("Native search omitted scope metadata")
                    expected_scopes[item["thread_id"]] = item["metadata"].get("test_scope")
                allowed = await accessible_thread_ids(identity, expected_scopes)
                for item in items:
                    if item["thread_id"] in allowed:
                        if not counting and offset <= visible_count < offset + limit:
                            visible.append(
                                {k: v for k, v in item.items() if not selected or k in selected}
                            )
                        visible_count += 1
                exhausted = len(items) < SEARCH_PAGE_SIZE
                if exhausted or (not counting and visible_count > offset + limit):
                    break
            if not exhausted and (counting or visible_count <= offset + limit):
                raise ValueError("Run visibility scan budget exceeded")
            # The upstream pagination headers describe primary-owner rows, including
            # inaccessible combined runs. Only emit totals after proving exhaustion.
            headers = {}
            if not counting:
                if exhausted:
                    headers["X-Pagination-Total"] = str(visible_count)
                if visible_count > offset + limit:
                    headers["X-Pagination-Next"] = str(offset + limit)
            return await JSONResponse(visible_count if counting else visible, headers=headers)(
                scope, receive, send
            )
        except Exception:
            return await self._unavailable(scope, receive, send)

    async def _capture(self, scope, body):
        response = {"status": 0, "headers": [], "body": b""}
        chunks = bytearray()
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                response.update(status=message["status"], headers=message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.extend(message.get("body", b""))
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("Run search exceeds response budget")

        headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await self.app({**scope, "headers": headers}, receive, send)
        response["body"] = bytes(chunks)
        return response

    @staticmethod
    async def _respond(response, scope, receive, send):
        result = Response(response["body"], status_code=response["status"])
        result.raw_headers = response["headers"]
        return await result(scope, receive, send)

    @staticmethod
    async def _unavailable(scope, receive, send):
        return await JSONResponse({"detail": "Run access could not be verified"}, status_code=503)(
            scope, receive, send
        )

    @staticmethod
    async def _not_found(scope, receive, send):
        return await JSONResponse({"detail": "Run not found"}, status_code=404)(
            scope, receive, send
        )
