"""Bounded, credential-safe values for durable audit and telemetry records."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apex.domain.diagnostics import bounded_diagnostic, is_credential_field

_POSTGRES_NUL_REPLACEMENT = "\ufffd"
_MAX_DEPTH = 16
_MAX_NODES = 2_000
_MAX_ITEMS = 256
_STRING_LIMIT = 4_096
_KEY_LIMIT = 255
_MAX_BYTES = 256 * 1_024
_PREFIX_BYTES = 8_192
_TRUNCATED_KEY = "_apex_evidence_truncated"
_REDACTED_VALUE = "[REDACTED]"
_REDACTED_KEY = "[redacted-credential-key]"


@dataclass
class _JsonBudget:
    remaining_nodes: int = _MAX_NODES
    active_container_ids: set[int] = field(default_factory=set)


def sanitize_durable_text(value: str | None, limit: int) -> str | None:
    """Return one bounded scalar with embedded credential material removed."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if value is None:
        return None
    # PostgreSQL text and JSONB cannot represent U+0000. Durable evidence is
    # attacker-influenced, so normalize it before hashing and persistence.
    value = value.replace("\x00", _POSTGRES_NUL_REPLACEMENT)
    # Bound diagnostics before field-specific truncation so auth schemes,
    # assignments, URL userinfo, and signed-query parameters are removed first.
    value = bounded_diagnostic(value, max_chars=max(limit * 4, 16_384))
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    marker = f"...[truncated:{digest}]" if limit >= 32 else f"~{digest}"
    return value[: limit - len(marker)] + marker


def sanitize_durable_object(value: Any) -> dict[str, Any]:
    """Return a bounded, cycle-safe JSON object suitable for PostgreSQL JSONB."""

    sanitized = _sanitize_json(value)
    if isinstance(sanitized, dict):
        return sanitized
    return {_TRUNCATED_KEY: {"reason": "non-object-root", "value": sanitized}}


def _sanitize_json(value: Any) -> Any:
    sanitized = _sanitize_value(value, _JsonBudget(), depth=0)
    try:
        encoded = json.dumps(
            sanitized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        return {_TRUNCATED_KEY: {"reason": "serialization-failed"}}
    if len(encoded) <= _MAX_BYTES:
        return sanitized
    return {
        _TRUNCATED_KEY: {
            "reason": "encoded-byte-limit",
            "original_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "prefix": encoded[:_PREFIX_BYTES].decode("utf-8", errors="replace"),
        }
    }


def _sanitize_value(value: Any, budget: _JsonBudget, *, depth: int) -> Any:
    if budget.remaining_nodes <= 0:
        return {_TRUNCATED_KEY: "node-limit"}
    budget.remaining_nodes -= 1

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return sanitize_durable_text(value, _STRING_LIMIT)
    if isinstance(value, int):
        if value.bit_length() <= 256:
            return value
        sign = "negative" if value < 0 else "positive"
        return f"[integer-out-of-range:{sign}:bits={value.bit_length()}]"
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite-float]"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return _sanitize_mapping(value, budget, depth=depth)
    if isinstance(value, (list, tuple)):
        return _sanitize_sequence(value, budget, depth=depth)
    type_name = sanitize_durable_text(type(value).__name__, 64) or "unknown"
    return f"[unsupported-json-value:{type_name}]"


def _sanitize_mapping(value: Mapping[Any, Any], budget: _JsonBudget, *, depth: int) -> Any:
    if depth >= _MAX_DEPTH:
        return {_TRUNCATED_KEY: "depth-limit"}
    identity = id(value)
    if identity in budget.active_container_ids:
        return {_TRUNCATED_KEY: "circular-reference"}
    budget.active_container_ids.add(identity)
    result: dict[str, Any] = {}
    try:
        try:
            iterator = iter(value.items())
        except Exception:  # noqa: BLE001 - malformed evidence must remain persistable
            return {_TRUNCATED_KEY: "unreadable-mapping"}
        for index in range(_MAX_ITEMS + 1):
            try:
                key, item = next(iterator)
            except StopIteration:
                break
            except Exception:  # noqa: BLE001 - retain a deterministic marker
                _insert_mapping_value(result, _TRUNCATED_KEY, "mapping-error")
                break
            if index == _MAX_ITEMS:
                _insert_mapping_value(result, _TRUNCATED_KEY, "item-limit")
                break
            if budget.remaining_nodes <= 0:
                _insert_mapping_value(result, _TRUNCATED_KEY, "node-limit")
                break
            _insert_mapping_value(
                result,
                _sanitize_key(key),
                (
                    _REDACTED_VALUE
                    if isinstance(key, str) and is_credential_field(key)
                    else _sanitize_value(item, budget, depth=depth + 1)
                ),
            )
        return result
    finally:
        budget.active_container_ids.discard(identity)


def _sanitize_sequence(
    value: list[Any] | tuple[Any, ...], budget: _JsonBudget, *, depth: int
) -> Any:
    if depth >= _MAX_DEPTH:
        return [{_TRUNCATED_KEY: "depth-limit"}]
    identity = id(value)
    if identity in budget.active_container_ids:
        return [{_TRUNCATED_KEY: "circular-reference"}]
    budget.active_container_ids.add(identity)
    result: list[Any] = []
    try:
        try:
            iterator = iter(value)
        except Exception:  # noqa: BLE001 - malformed evidence must remain persistable
            return [{_TRUNCATED_KEY: "unreadable-sequence"}]
        for index in range(_MAX_ITEMS + 1):
            try:
                item = next(iterator)
            except StopIteration:
                break
            except Exception:  # noqa: BLE001 - retain a deterministic marker
                result.append({_TRUNCATED_KEY: "sequence-error"})
                break
            if index == _MAX_ITEMS:
                result.append({_TRUNCATED_KEY: "item-limit"})
                break
            if budget.remaining_nodes <= 0:
                result.append({_TRUNCATED_KEY: "node-limit"})
                break
            result.append(_sanitize_value(item, budget, depth=depth + 1))
        return result
    finally:
        budget.active_container_ids.discard(identity)


def _sanitize_key(value: Any) -> str:
    if isinstance(value, str):
        if is_credential_field(value):
            return _REDACTED_KEY
        key = value
    elif value is None:
        key = "[non-string-key:null]"
    elif isinstance(value, bool):
        key = f"[non-string-key:bool:{str(value).lower()}]"
    elif isinstance(value, int) and value.bit_length() <= 256:
        key = f"[non-string-key:int:{value}]"
    elif isinstance(value, float) and math.isfinite(value):
        key = f"[non-string-key:float:{value}]"
    else:
        key = f"[non-string-key:{type(value).__name__}]"
    return sanitize_durable_text(key, _KEY_LIMIT) or "[empty-key]"


def _insert_mapping_value(result: dict[str, Any], key: str, value: Any) -> None:
    candidate = key
    collision = 2
    while candidate in result:
        suffix = f"#{collision}"
        candidate = f"{key[: _KEY_LIMIT - len(suffix)]}{suffix}"
        collision += 1
    result[candidate] = value
