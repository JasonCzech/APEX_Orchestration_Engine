"""Application-owned structlog configuration."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from apex.settings import get_settings

_SECRET_KEY_FRAGMENTS = ("secret", "token", "password", "authorization", "api_key", "key_hash")
_REDACTED = "[redacted]"
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[-._~+/A-Za-z0-9]+=*")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:api[_-]?key|token|password|secret)=)[^&#\s]+")
_URL_USERINFO_RE = re.compile(r"://[^/@\s]+@")


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
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def _redact_value(key: Any, value: Any) -> Any:
    lowered = _key_text(key).lower()
    if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, child) for child in value]
    if isinstance(value, tuple):
        if len(value) == 2:
            header_key = _key_text(value[0])
            return (value[0], _redact_value(header_key, value[1]))
        return tuple(_redact_value(key, child) for child in value)
    if isinstance(value, set):
        return {_redact_value(key, child) for child in value}
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _key_text(key: Any) -> str:
    if isinstance(key, bytes):
        return key.decode("utf-8", errors="replace")
    return str(key)


def _redact_string(value: str) -> str:
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    value = _QUERY_SECRET_RE.sub(r"\1[redacted]", value)
    return _URL_USERINFO_RE.sub("://[redacted]@", value)
