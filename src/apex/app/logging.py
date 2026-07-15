"""Application-owned structlog configuration."""

from __future__ import annotations

import logging
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
    if isinstance(value, bytes | bytearray | memoryview):
        value = bytes(value[:_MAX_LOG_STRING_CHARS]).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, BaseException):
        return _redact_string(str(value))
    if depth >= _MAX_REDACTION_DEPTH and isinstance(value, Mapping | list | tuple | set):
        return _TRUNCATED
    container_id = id(value)
    if isinstance(value, Mapping | list | tuple | set):
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
    if isinstance(value, Mapping):
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
    if isinstance(value, list):
        rendered = [
            _redact_value(key, child, depth=depth + 1, state=state)
            for child in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            rendered.append(_TRUNCATED)
        return rendered
    if isinstance(value, tuple):
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
    if isinstance(value, set):
        rendered = {
            _redact_value(key, child, depth=depth + 1, state=state)
            for child in islice(value, _MAX_COLLECTION_ITEMS)
        }
        if len(value) > _MAX_COLLECTION_ITEMS:
            rendered.add(_TRUNCATED)
        return rendered
    if value is None or isinstance(value, bool | int | float):
        return value
    # JSONRenderer stringifies unsupported objects after this processor runs.
    # Normalize them here so custom ``str``/``repr`` implementations cannot
    # smuggle credentials around the shared diagnostic scrubber.
    return bounded_diagnostic(value, max_chars=_MAX_LOG_STRING_CHARS)


def _key_text(key: Any) -> str:
    if isinstance(key, bytes):
        rendered = key[:_MAX_LOG_KEY_CHARS].decode("utf-8", errors="replace")
    else:
        # Nested mappings and header-like tuples can originate in provider
        # payloads. Their keys are data too: normalize arbitrary objects before
        # JSONRenderer gets a chance to invoke an unsafe ``repr`` and scrub
        # credentials embedded in a dynamic key (for example
        # ``"authorization=Bearer ..."`` or a signed URL).
        rendered = bounded_diagnostic(key, max_chars=_MAX_LOG_KEY_CHARS)
    return bounded_diagnostic(rendered, max_chars=_MAX_LOG_KEY_CHARS)


def _redact_string(value: str) -> str:
    # Reuse the diagnostic boundary so credentials embedded in otherwise generic
    # fields (``reason``, ``detail``, exception strings, etc.) get the same
    # key/value, JSON, auth-scheme, URL-userinfo, and signed-query protection as
    # provider errors and checkpoints.  The helper redacts before truncating.
    return bounded_diagnostic(value, max_chars=_MAX_LOG_STRING_CHARS)
