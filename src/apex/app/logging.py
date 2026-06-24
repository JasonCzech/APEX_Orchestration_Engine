"""Application-owned structlog configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

_SECRET_KEY_FRAGMENTS = ("secret", "token", "password", "authorization", "api_key", "key_hash")
_REDACTED = "[redacted]"


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_event_dict,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=True,
    )


def _redact_event_dict(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, child) for child in value]
    return value
