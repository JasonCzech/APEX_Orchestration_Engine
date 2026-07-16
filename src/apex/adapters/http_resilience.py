"""Small shared retry/backoff/circuit-breaker helpers for HTTP adapters."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any

import httpx

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})
STREAM_ERROR_PREVIEW_BYTES = 64 * 1024
DEFAULT_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
HARD_MAX_BUFFERED_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_JSON_MAX_DEPTH = 64
DEFAULT_JSON_MAX_TOKENS = 100_000
HARD_JSON_MAX_DEPTH = 128
HARD_JSON_MAX_TOKENS = 1_000_000
MAX_JSON_NUMBER_CHARS = 256
_TOTAL_DEADLINE_EXTENSION = "apex.total_deadline_monotonic"


class CircuitOpenError(RuntimeError):
    """Raised when an adapter poll path is temporarily short-circuited."""


class ResponseTooLargeError(RuntimeError):
    """A provider response exceeded the caller's decoded-body budget."""


class InvalidJsonResponseError(RuntimeError):
    """A provider returned JSON that is malformed or unsafe to materialize."""


def parse_json_response(
    response: httpx.Response,
    *,
    context: str,
    max_depth: int = DEFAULT_JSON_MAX_DEPTH,
    max_tokens: int = DEFAULT_JSON_MAX_TOKENS,
) -> Any:
    """Decode one buffered provider JSON body under structural resource limits.

    The HTTP adapters already cap response bytes before this function is called,
    but a small deeply nested document can still overflow Python's recursive JSON
    decoder and a flat array can expand into far more heap objects than wire bytes.
    A non-recursive lexical pass rejects both shapes before ``json.loads``. JSON
    provider APIs are UTF-8 by contract; accepting charset guessing here would make
    validation dependent on ambiguous or duplicated response headers.
    """

    _validate_json_limits(max_depth=max_depth, max_tokens=max_tokens)
    _require_json_content_type(response, context=context)
    text: str | None = None
    try:
        text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if text is None:
        # Raise after leaving the handler. UnicodeDecodeError retains the entire
        # provider byte payload on ``.object`` and must not survive as context.
        raise InvalidJsonResponseError(f"{context} returned non-UTF-8 JSON") from None
    parsed: Any = None
    invalid = False
    try:
        _validate_json_wire_limits(text, max_depth=max_depth, max_tokens=max_tokens)
        parsed = _strict_json_loads(text)
    except (ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        # JSONDecodeError retains the complete provider body on ``.doc``. Keep
        # neither it nor duplicate-key/provider-value errors in the public chain.
        raise InvalidJsonResponseError(f"{context} returned invalid JSON") from None
    return parsed


def parse_json_bytes(
    payload: bytes | bytearray | memoryview | str,
    *,
    context: str,
    max_depth: int = DEFAULT_JSON_MAX_DEPTH,
    max_tokens: int = DEFAULT_JSON_MAX_TOKENS,
) -> Any:
    """Decode non-HTTP JSON with the same non-recursive structural limits."""

    _validate_json_limits(max_depth=max_depth, max_tokens=max_tokens)
    if isinstance(payload, str):
        text: str | None = payload
    else:
        text = None
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            pass
        if text is None:
            raise InvalidJsonResponseError(f"{context} returned non-UTF-8 JSON") from None
    parsed: Any = None
    invalid = False
    try:
        _validate_json_wire_limits(text, max_depth=max_depth, max_tokens=max_tokens)
        parsed = _strict_json_loads(text)
    except (ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise InvalidJsonResponseError(f"{context} returned invalid JSON") from None
    return parsed


def _validate_json_limits(*, max_depth: int, max_tokens: int) -> None:
    if not 1 <= max_depth <= HARD_JSON_MAX_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {HARD_JSON_MAX_DEPTH}")
    if not 1 <= max_tokens <= HARD_JSON_MAX_TOKENS:
        raise ValueError(f"max_tokens must be between 1 and {HARD_JSON_MAX_TOKENS}")


def _strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-standard JSON constant {value!r}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object member {key!r}")
            result[key] = value
        return result

    def bounded_int(raw: str) -> int:
        if len(raw) > MAX_JSON_NUMBER_CHARS:
            raise ValueError("JSON integer exceeds the numeric token limit")
        return int(raw)

    def bounded_float(raw: str) -> float:
        if len(raw) > MAX_JSON_NUMBER_CHARS:
            raise ValueError("JSON number exceeds the numeric token limit")
        exact: Decimal | None = None
        invalid_number = False
        try:
            exact = Decimal(raw)
        except InvalidOperation:
            invalid_number = True
        if invalid_number or exact is None:
            raise ValueError("invalid JSON number")
        value = float(exact)
        if not exact.is_finite() or not math.isfinite(value):
            raise ValueError("JSON number is outside the finite float range")
        if value == 0.0 and exact != 0:
            raise ValueError("JSON number underflows the finite float range")
        return value

    return json.loads(
        text,
        parse_constant=reject_constant,
        parse_float=bounded_float,
        parse_int=bounded_int,
        object_pairs_hook=unique_object,
    )


def _require_json_content_type(response: httpx.Response, *, context: str) -> None:
    values = response.headers.get_list("content-type")
    if not values:
        # Several legacy control-plane APIs omit Content-Type. The body still has
        # to pass strict UTF-8 JSON validation, so absence is unambiguous.
        return
    if len(values) != 1:
        raise InvalidJsonResponseError(f"{context} returned ambiguous Content-Type headers")
    parts = [part.strip().casefold() for part in values[0].split(";")]
    media_type = parts[0]
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise InvalidJsonResponseError(f"{context} returned a non-JSON Content-Type")
    charsets: list[str] = []
    for parameter in parts[1:]:
        name, separator, raw_value = parameter.partition("=")
        if name.strip() != "charset":
            continue
        if not separator:
            raise InvalidJsonResponseError(f"{context} returned an unsupported JSON charset")
        charsets.append(raw_value.strip().strip('"').strip())
    if len(charsets) > 1 or (charsets and charsets[0] not in {"utf-8", "utf8", "us-ascii"}):
        raise InvalidJsonResponseError(f"{context} returned an unsupported JSON charset")


def _validate_json_wire_limits(text: str, *, max_depth: int, max_tokens: int) -> None:
    """Count JSON containers/scalars iteratively without materializing values."""

    depth = 0
    tokens = 0
    in_string = False
    escaped = False
    in_scalar = False

    def consume_token() -> None:
        nonlocal tokens
        tokens += 1
        if tokens > max_tokens:
            raise InvalidJsonResponseError(
                f"provider JSON exceeds the {max_tokens}-token structural limit"
            )

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if in_scalar:
            if char not in " \t\r\n,]}:":
                continue
            in_scalar = False

        if char == '"':
            consume_token()
            in_string = True
        elif char in "[{":
            consume_token()
            depth += 1
            if depth > max_depth:
                raise InvalidJsonResponseError(
                    f"provider JSON exceeds the {max_depth}-level nesting limit"
                )
        elif char in "]}":
            depth -= 1
        elif char in " \t\r\n,:":
            continue
        else:
            consume_token()
            in_scalar = True


class _DeadlineStream(httpx.AsyncByteStream):
    """Enforce one absolute retry+headers+body deadline across stream yields."""

    def __init__(
        self,
        source: httpx.AsyncByteStream,
        *,
        deadline: float,
    ) -> None:
        self._source = source
        self._deadline = deadline

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        timed_out = False
        try:
            async with asyncio.timeout_at(self._deadline):
                async for chunk in self._source:
                    yield chunk
        except TimeoutError:
            timed_out = True
        if timed_out:
            await self._source.aclose()
            raise httpx.ReadTimeout(
                "total request timeout exceeded while reading response body",
            )

    async def aclose(self) -> None:
        await self._source.aclose()


async def close_response_definitively(response: httpx.Response) -> None:
    """Close a provider response even if its consuming request is cancelled again.

    A first cancellation commonly enters a caller's ``finally`` block. A second
    cancellation can interrupt an ordinary ``await response.aclose()`` and leak
    the pool slot permanently. The close task owns the response until completion;
    shielding lets caller cancellation propagate without cancelling cleanup.
    """

    close_task = asyncio.create_task(response.aclose())
    interrupted = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break

    error: BaseException | None = None
    try:
        close_task.result()
    except BaseException as exc:
        error = exc
    if interrupted:
        raise asyncio.CancelledError from None
    if error is not None:
        raise error


def require_identity_content_encoding(response: httpx.Response) -> None:
    """Reject compressed provider bodies before any decoder can allocate them."""

    content_encoding = response.headers.get("content-encoding", "").strip().casefold()
    if content_encoding not in {"", "identity"}:
        raise ResponseTooLargeError(
            "provider ignored Accept-Encoding: identity; compressed responses are not accepted"
        )


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 0.5
    # A server-sent Retry-After is honored up to this cap (the local backoff cap
    # max_delay_s is far too small to respect a real throttle signal). Still bounded
    # by total_timeout_s / the per-request deadline.
    retry_after_cap_s: float = 30.0
    total_timeout_s: float | None = 10.0
    transient_statuses: frozenset[int] = TRANSIENT_STATUSES
    retry_methods: frozenset[str] = IDEMPOTENT_METHODS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempts, int)
            or isinstance(self.attempts, bool)
            or self.attempts < 1
        ):
            raise ValueError("retry attempts must be a positive integer")
        for name in ("base_delay_s", "max_delay_s", "retry_after_cap_s"):
            value: float | None = None
            invalid_value = False
            try:
                value = float(getattr(self, name))
            except (TypeError, ValueError):
                invalid_value = True
            if invalid_value or value is None:
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
            setattr(self, name, value)
        if self.total_timeout_s is not None:
            timeout: float | None = None
            invalid_timeout = False
            try:
                timeout = float(self.total_timeout_s)
            except (TypeError, ValueError):
                invalid_timeout = True
            if invalid_timeout or timeout is None:
                raise ValueError("total_timeout_s must be a finite non-negative number or None")
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("total_timeout_s must be a finite non-negative number or None")
            self.total_timeout_s = timeout


@dataclass(slots=True)
class CircuitBreaker:
    """A lightweight in-process breaker for hot polling loops."""

    name: str
    failure_threshold: int = 5
    reset_after_s: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None
    _lock: Lock = field(default_factory=Lock, repr=False)

    def before_request(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at < self.reset_after_s:
                raise CircuitOpenError(f"HTTP circuit {self.name!r} is open")
            self._opened_at = None
            self._failures = 0

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
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
    max_response_bytes: int = DEFAULT_JSON_RESPONSE_BYTES,
    **kwargs: Any,
) -> httpx.Response:
    """Run an HTTP request with bounded retries for transient failures.

    Callers still own domain-specific status mapping. This helper only retries
    transport errors and retryable HTTP statuses, then returns the final
    response for the adapter to interpret.
    """

    if (
        type(max_response_bytes) is not int
        or not 1 <= max_response_bytes <= HARD_MAX_BUFFERED_RESPONSE_BYTES
    ):
        raise ValueError(
            f"max_response_bytes must be between 1 and {HARD_MAX_BUFFERED_RESPONSE_BYTES}"
        )
    response = await resilient_stream_request(
        client,
        method,
        url,
        retry=retry,
        breaker=breaker,
        random_fn=random_fn,
        sleep_fn=sleep_fn,
        **kwargs,
    )
    return await read_bounded_response(response, max_bytes=max_response_bytes)


async def resilient_stream_request(
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
    """Open a response stream with the same bounded resilience as normal requests.

    The returned response remains open and must be closed by the caller. Responses
    discarded for a retry are always closed first, so a transient status cannot
    leak a connection from the client's pool. Only response headers are received
    before this helper returns; the successful body is never buffered in memory.
    """

    policy = retry or RetryPolicy()
    normalized_method = method.upper()
    max_attempts = max(policy.attempts, 1)
    can_retry = normalized_method in policy.retry_methods
    deadline = (
        time.monotonic() + max(policy.total_timeout_s, 0.0)
        if policy.total_timeout_s is not None
        else None
    )

    for attempt in range(1, max_attempts + 1):
        if breaker is not None:
            breaker.before_request()
        response: httpx.Response | None = None
        deadline_expired = False
        transport_failed = False
        try:
            response = await _stream_request_with_deadline(
                client, normalized_method, url, deadline=deadline, **kwargs
            )
        except TimeoutError:
            deadline_expired = True
        except httpx.HTTPError:
            transport_failed = True
        if transport_failed:
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                raise httpx.TransportError("provider transport request failed")
            await _sleep_with_deadline(_delay(policy, attempt, random_fn), deadline, sleep_fn)
            continue
        if deadline_expired:
            timeout = httpx.TimeoutException("total retry timeout exceeded")
            if breaker is not None:
                breaker.record_failure()
            raise timeout
        if response is None:  # pragma: no cover - request contract invariant
            raise RuntimeError("resilient request completed without a response")

        if response.status_code in policy.transient_statuses:
            if not can_retry or attempt >= max_attempts:
                if breaker is not None:
                    breaker.record_failure()
                return _attach_response_deadline(response, deadline)
            # The response owns a pool slot until it is closed.  Close it before
            # interpreting attacker-controlled retry metadata so malformed
            # headers (or a caller-supplied jitter hook) cannot leak the slot by
            # raising during delay calculation.
            await close_response_definitively(response)
            delay = _retry_delay(response, policy, attempt, random_fn)
            await _sleep_with_deadline(delay, deadline, sleep_fn)
            continue

        if breaker is not None:
            breaker.record_success()
        return _attach_response_deadline(response, deadline)

    raise RuntimeError("resilient_stream_request exhausted attempts without a response")


async def read_stream_error_preview(
    response: httpx.Response, *, max_bytes: int = STREAM_ERROR_PREVIEW_BYTES
) -> bytes:
    """Read at most ``max_bytes`` from an error stream for diagnostics.

    Callers must close the response in ``finally``. Stopping at the preview cap
    avoids buffering or decompressing an attacker-controlled error body merely
    to build an exception message.
    """

    if type(max_bytes) is not int or not 1 <= max_bytes <= STREAM_ERROR_PREVIEW_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {STREAM_ERROR_PREVIEW_BYTES}")
    deadline_expired = False
    try:
        _raise_if_response_deadline_expired(response)
    except httpx.ReadTimeout:
        deadline_expired = True
    if deadline_expired:
        await close_response_definitively(response)
        raise httpx.ReadTimeout("total request timeout exceeded while reading response body")
    preview = bytearray()
    async for chunk in response.aiter_raw():
        remaining = max_bytes - len(preview)
        if remaining <= 0:
            break
        preview.extend(chunk[:remaining])
        if len(preview) >= max_bytes:
            break
    return bytes(preview)


async def read_bounded_response(
    response: httpx.Response, *, max_bytes: int = DEFAULT_JSON_RESPONSE_BYTES
) -> httpx.Response:
    """Buffer at most ``max_bytes`` decoded bytes and close the source stream.

    ``httpx``'s ordinary request path eagerly buffers the entire response before a
    caller can inspect it. Engine adapters use this after ``resilient_stream_request``
    so a compromised provider cannot turn a small JSON endpoint into an unbounded
    heap allocation. The returned response has the same status, headers and request
    and can be consumed with the normal ``.json()``/``.text`` APIs.
    """

    if type(max_bytes) is not int or not 1 <= max_bytes <= HARD_MAX_BUFFERED_RESPONSE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {HARD_MAX_BUFFERED_RESPONSE_BYTES}")
    deadline_expired = False
    try:
        _raise_if_response_deadline_expired(response)
    except httpx.ReadTimeout:
        deadline_expired = True
    if deadline_expired:
        await close_response_definitively(response)
        raise httpx.ReadTimeout("total request timeout exceeded while reading response body")
    unsupported_encoding = False
    try:
        require_identity_content_encoding(response)
    except ResponseTooLargeError:
        unsupported_encoding = True
    if unsupported_encoding:
        await close_response_definitively(response)
        raise ResponseTooLargeError(
            "provider ignored Accept-Encoding: identity; compressed responses are not accepted"
        )

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > max_bytes:
            await close_response_definitively(response)
            raise ResponseTooLargeError(
                f"provider response declared {declared_length} bytes; limit is {max_bytes}"
            )

    body = bytearray()
    try:
        if response.is_stream_consumed:
            # Mock/custom transports can return a response whose in-memory stream
            # was consumed before ``send(stream=True)`` returns. Real network
            # responses stay unconsumed here; retain compatibility while enforcing
            # the same postcondition for test/downstream transports.
            content = response.content
            if len(content) > max_bytes:
                raise ResponseTooLargeError(
                    f"provider response exceeded decoded-body limit of {max_bytes} bytes"
                )
            body.extend(content)
        else:
            # Raw and decoded bytes are identical because non-identity encodings were
            # rejected above. Using the raw iterator avoids an oversized intermediate
            # allocation inside httpx's gzip/brotli decoder.
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > max_bytes:
                    raise ResponseTooLargeError(
                        f"provider response exceeded decoded-body limit of {max_bytes} bytes"
                    )
                body.extend(chunk)
    finally:
        await close_response_definitively(response)

    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=bytes(body),
        request=response.request,
    )


def retry_policy(
    *,
    attempts: int = 3,
    retry_methods: Iterable[str] = IDEMPOTENT_METHODS,
    total_timeout_s: float | None = 10.0,
) -> RetryPolicy:
    return RetryPolicy(
        attempts=attempts,
        retry_methods=frozenset(m.upper() for m in retry_methods),
        total_timeout_s=total_timeout_s,
    )


def _attach_response_deadline(response: httpx.Response, deadline: float | None) -> httpx.Response:
    if deadline is None:
        return response
    response.extensions[_TOTAL_DEADLINE_EXTENSION] = deadline
    if not response.is_stream_consumed and isinstance(response.stream, httpx.AsyncByteStream):
        response.stream = _DeadlineStream(
            response.stream,
            deadline=deadline,
        )
    return response


def _raise_if_response_deadline_expired(response: httpx.Response) -> None:
    deadline = response.extensions.get(_TOTAL_DEADLINE_EXTENSION)
    if isinstance(deadline, int | float) and time.monotonic() >= float(deadline):
        raise httpx.ReadTimeout(
            "total request timeout exceeded while reading response body",
        )


def _delay(policy: RetryPolicy, attempt: int, random_fn: Any) -> float:
    jitter = float(random_fn())
    if not math.isfinite(jitter) or not 0.0 <= jitter <= 1.0:
        raise ValueError("retry jitter must be a finite number between 0 and 1")
    exponent = max(attempt - 1, 0)
    try:
        base = math.ldexp(float(policy.base_delay_s), exponent)
    except OverflowError:
        base = float(policy.max_delay_s)
    base = min(base, float(policy.max_delay_s))
    delay = min(base * (1 + jitter * 0.25), float(policy.max_delay_s))
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("computed retry delay must be finite and non-negative")
    return delay


async def _stream_request_with_deadline(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline: float | None,
    **kwargs: Any,
) -> httpx.Response:
    supplied_headers = kwargs.pop("headers", None)
    headers = httpx.Headers(supplied_headers or {})
    # A response decompressor can allocate a huge decoded chunk before a caller's
    # byte counter observes it. Control-plane adapters therefore request identity
    # bodies and reject providers/proxies that ignore the request.
    headers["Accept-Encoding"] = "identity"
    request = client.build_request(method, url, headers=headers, **kwargs)
    if deadline is None:
        return await client.send(request, stream=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(client.send(request, stream=True), timeout=remaining)


async def _sleep_with_deadline(delay: float, deadline: float | None, sleep_fn: Any) -> None:
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("retry delay must be finite and non-negative")
    if deadline is None:
        await sleep_fn(delay)
        return
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise httpx.TimeoutException("total retry timeout exceeded")
    timed_out = False
    try:
        await asyncio.wait_for(sleep_fn(min(delay, remaining)), timeout=remaining)
    except TimeoutError:
        timed_out = True
    if timed_out:
        raise httpx.TimeoutException("total retry timeout exceeded")


def _retry_delay(
    response: httpx.Response,
    policy: RetryPolicy,
    attempt: int,
    random_fn: Any,
) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after is None:
        return _delay(policy, attempt, random_fn)
    cap = max(policy.retry_after_cap_s, policy.max_delay_s)
    try:
        numeric_delay = float(retry_after)
    except (TypeError, ValueError):
        pass
    else:
        if math.isfinite(numeric_delay):
            return min(max(numeric_delay, 0.0), cap)
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, OverflowError):
        return _delay(policy, attempt, random_fn)
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return min(max((retry_at - datetime.now(UTC)).total_seconds(), 0.0), cap)
