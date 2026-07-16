"""Application-owned structlog configuration."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from apex.domain.diagnostics import bounded_diagnostic, is_credential_field
from apex.settings import get_settings

_SECRET_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "key_hash",
    "credential",
    "signature",
)
_SECRET_KEYS = frozenset(
    {
        "cookie",
        "set-cookie",
        "set_cookie",
        "sig",
        "x-apex-trusted-loopback",
        "x_apex_trusted_loopback",
        "x-api-key",
        "x_api_key",
    }
)
_REDACTED = "[redacted]"
_TRUNCATED = "[truncated]"
_UNSUPPORTED_CONTAINER = "[unsupported container]"
_UNSUPPORTED_KEY = "[unsupported credential key]"
_UNSUPPORTED_VALUE = "[unsupported value]"
_MAX_REDACTION_DEPTH = 8
_MAX_REDACTION_NODES = 256
_MAX_COLLECTION_ITEMS = 64
_MAX_LOG_STRING_CHARS = 4_096
_MAX_LOG_KEY_CHARS = 512


@dataclass
class _RedactionState:
    nodes: int = 0
    active_container_ids: set[int] = field(default_factory=set)


def configure_logging() -> None:
    # Locked-down (production/staging) envs get structured JSON at an INFO floor;
    # local/dev keeps the human-readable console renderer at DEBUG.
    locked_down = get_settings().is_locked_down
    renderer: Any = (
        structlog.processors.JSONRenderer() if locked_down else structlog.dev.ConsoleRenderer()
    )
    min_level = logging.INFO if locked_down else logging.DEBUG
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_event_dict,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        cache_logger_on_first_use=True,
    )


def _redact_event_dict(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    if type(event_dict) is not dict:
        # Processor contracts normally provide a plain dict. Fail closed rather
        # than invoking provider-controlled mapping methods if that invariant is
        # ever violated by a custom processor.
        return {"event": _UNSUPPORTED_CONTAINER}
    state = _RedactionState()
    rendered: EventDict = {}
    for key, value in islice(event_dict.items(), _MAX_COLLECTION_ITEMS):
        safe_key = _key_text(key)
        rendered[safe_key] = _redact_value(safe_key, value, depth=0, state=state)
    return rendered


def _redact_value(
    key: Any,
    value: Any,
    *,
    depth: int,
    state: _RedactionState,
) -> Any:
    state.nodes += 1
    if state.nodes > _MAX_REDACTION_NODES:
        return _TRUNCATED
    lowered = _key_text(key).lower()
    if (
        lowered in _SECRET_KEYS
        or is_credential_field(lowered)
        or any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)
    ):
        return _REDACTED
    value_type = type(value)
    if value_type in {bytes, bytearray, memoryview}:
        value = bytes(value[:_MAX_LOG_STRING_CHARS]).decode("utf-8", errors="replace")
        value_type = str
    if value_type is str:
        return _redact_string(value)
    # ``isinstance(provider_value, ...)`` reads a spoofable ``__class__``
    # attribute after an exact-type miss.  Classify subclasses from their real
    # type so logging never executes a provider-owned descriptor merely while
    # deciding how to redact it.
    if issubclass(value_type, BaseException):
        return _safe_exception_text(value)
    is_container = value_type in {dict, list, tuple, set, frozenset}
    is_container_subclass = issubclass(
        value_type,
        (Mapping, list, tuple, set, frozenset, bytes, bytearray, memoryview, str),
    )
    if is_container_subclass and not is_container:
        # Do not call items(), __iter__(), __len__(), str(), or repr() on an
        # untrusted container subclass. A fixed marker is both safe and useful.
        return _UNSUPPORTED_CONTAINER
    if depth >= _MAX_REDACTION_DEPTH and is_container:
        return _TRUNCATED
    container_id = id(value)
    if is_container:
        if container_id in state.active_container_ids:
            return _TRUNCATED
        state.active_container_ids.add(container_id)
    try:
        return _redact_container_value(key, value, depth=depth, state=state)
    finally:
        state.active_container_ids.discard(container_id)


def _redact_container_value(
    key: Any,
    value: Any,
    *,
    depth: int,
    state: _RedactionState,
) -> Any:
    if type(value) is dict:
        rendered = {}
        for child_key, child in islice(value.items(), _MAX_COLLECTION_ITEMS):
            safe_child_key = _key_text(child_key)
            rendered[safe_child_key] = _redact_value(
                safe_child_key,
                child,
                depth=depth + 1,
                state=state,
            )
        if len(value) > _MAX_COLLECTION_ITEMS:
            rendered["__truncated__"] = _TRUNCATED
        return rendered
    if type(value) is list:
        rendered = [
            _redact_value(key, child, depth=depth + 1, state=state)
            for child in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            rendered.append(_TRUNCATED)
        return rendered
    if type(value) is tuple:
        if len(value) == 2:
            header_key = _key_text(value[0])
            return (
                header_key,
                _redact_value(header_key, value[1], depth=depth + 1, state=state),
            )
        rendered = tuple(
            _redact_value(key, child, depth=depth + 1, state=state)
            for child in value[:_MAX_COLLECTION_ITEMS]
        )
        return rendered + ((_TRUNCATED,) if len(value) > _MAX_COLLECTION_ITEMS else ())
    if type(value) in {set, frozenset}:
        rendered = {
            _redact_value(key, child, depth=depth + 1, state=state)
            for child in islice(value, _MAX_COLLECTION_ITEMS)
        }
        if len(value) > _MAX_COLLECTION_ITEMS:
            rendered.add(_TRUNCATED)
        return frozenset(rendered) if type(value) is frozenset else rendered
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if value.bit_length() <= 256 else _UNSUPPORTED_VALUE
    if type(value) is float:
        return value if math.isfinite(value) else _UNSUPPORTED_VALUE
    # JSONRenderer stringifies unsupported objects after this processor runs.
    # Normalize them here so custom ``str``/``repr`` implementations cannot
    # smuggle credentials around the shared diagnostic scrubber.
    return _UNSUPPORTED_VALUE


def _safe_exception_text(value: BaseException) -> str:
    """Render only an exact string exception argument, bypassing custom hooks."""

    args_descriptor = BaseException.__dict__["args"]
    try:
        args = args_descriptor.__get__(value, type(value))
    except Exception:  # noqa: BLE001 - logging must never mask a request failure
        return _UNSUPPORTED_VALUE
    if type(args) is tuple and len(args) == 1 and type(args[0]) is str:
        return _redact_string(args[0])
    return _UNSUPPORTED_VALUE


def _key_text(key: Any) -> str:
    if type(key) is bytes:
        rendered = key[:_MAX_LOG_KEY_CHARS].decode("utf-8", errors="replace")
    elif type(key) is str:
        rendered = key
    elif key is None or type(key) is bool:
        rendered = str(key)
    elif type(key) is int:
        if key.bit_length() > 256:
            return _UNSUPPORTED_KEY
        rendered = str(key)
    elif type(key) is float:
        if not math.isfinite(key):
            return _UNSUPPORTED_KEY
        rendered = str(key)
    else:
        # Unknown provider key types may carry arbitrary __str__/__repr__ hooks.
        # Treat their values as credential-bearing and never invoke those hooks.
        return _UNSUPPORTED_KEY
    return bounded_diagnostic(rendered, max_chars=_MAX_LOG_KEY_CHARS)


def _redact_string(value: str) -> str:
    # Reuse the diagnostic boundary so credentials embedded in otherwise generic
    # fields (``reason``, ``detail``, exception strings, etc.) get the same
    # key/value, JSON, auth-scheme, URL-userinfo, and signed-query protection as
    # provider errors and checkpoints.  The helper redacts before truncating.
    return bounded_diagnostic(value, max_chars=_MAX_LOG_STRING_CHARS)
