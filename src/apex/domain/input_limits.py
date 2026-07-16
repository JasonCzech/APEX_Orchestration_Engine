"""Shared request-shape limits and non-recursive JSON validation.

These limits sit at HTTP/domain boundaries rather than in persistence so malformed
input is rejected deterministically before database or provider I/O begins.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from pydantic import StringConstraints, ValidationError

MAX_SCOPE_ID_CHARS = 255
MAX_RECORD_ID_CHARS = 32
MAX_RESOURCE_ID_CHARS = 255
MAX_DESCRIPTION_CHARS = 20_000
MAX_JSON_BYTES = 100_000
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 2_000
MAX_JSON_KEY_CHARS = 255
MAX_CHILD_ITEMS = 256
MAX_DB_LIST_OFFSET = 10_000

_SAFE_VALIDATION_MESSAGES = {
    "assertion_error": "Invalid request value",
    "bool_type": "Input should be a valid boolean",
    "bytes_type": "Input should be valid bytes",
    "date_type": "Input should be a valid date",
    "datetime_type": "Input should be a valid datetime",
    "decimal_type": "Input should be a valid number",
    "dict_type": "Input should be a valid object",
    "enum": "Input is not an allowed value",
    "extra_forbidden": "Extra inputs are not permitted",
    "finite_number": "Input should be a finite number",
    "float_type": "Input should be a valid number",
    "int_type": "Input should be a valid integer",
    "json_invalid": "Input should be valid JSON",
    "json_type": "Input should be valid JSON",
    "list_type": "Input should be a valid list",
    "literal_error": "Input is not an allowed value",
    "missing": "Field is required",
    "model_type": "Input should be a valid object",
    "set_type": "Input should be a valid set",
    "string_type": "Input should be a valid string",
    "time_type": "Input should be a valid time",
    "tuple_type": "Input should be a valid tuple",
    "url_type": "Input should be a valid URL",
    "uuid_type": "Input should be a valid UUID",
    "value_error": "Invalid request value",
}

NoNulStr = Annotated[
    str,
    StringConstraints(pattern=r"^[^\x00]*$"),
]

RecordId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_RECORD_ID_CHARS,
        pattern=r"^[^\x00]*$",
    ),
]

ResourceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_RESOURCE_ID_CHARS,
        pattern=r"^[^\x00]*$",
    ),
]

ScopeId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_SCOPE_ID_CHARS,
        pattern=r"^[^\x00]*$",
    ),
]


def safe_validation_message(error_type: str) -> str:
    """Return a stable public message without Pydantic ``ctx`` interpolation."""

    exact = _SAFE_VALIDATION_MESSAGES.get(error_type)
    if exact is not None:
        return exact
    if error_type.startswith("string_"):
        return "Invalid string value"
    if error_type.startswith(("bytes_", "list_", "set_", "tuple_", "dict_")):
        return "Invalid collection value"
    if error_type.startswith(
        (
            "greater_than",
            "less_than",
            "multiple_of",
            "decimal_",
            "int_",
            "float_",
        )
    ):
        return "Number is outside the allowed range"
    if error_type.startswith(("date_", "datetime_", "time_")):
        return "Invalid date or time value"
    if error_type.startswith(("url_", "uuid_")):
        return "Invalid identifier value"
    return "Invalid request value"


def validation_error_summary(
    exc: ValidationError,
    *,
    max_errors: int = 8,
    max_chars: int = 2_048,
) -> str:
    """Render bounded Pydantic error classes without rejected values.

    ``str(ValidationError)`` and the default error dictionaries include the
    rejected input and validator context. Those values can contain credentials,
    so request/auth boundaries must use this projection instead.
    """

    rendered: list[str] = []
    errors = exc.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )
    for error in errors[:max_errors]:
        error_type = str(error.get("type") or "validation_error")[:128]
        # Mapping keys in ``loc`` and even some Pydantic ``msg`` variants can
        # contain the rejected value (for example discriminator tags or custom
        # validator text). Retain only sequence indices and fixed messages keyed
        # by the error class; never reflect arbitrary location/message strings.
        location = ".".join(
            f"[{part}]" if isinstance(part, int) and part >= 0 else "field"
            for part in error.get("loc", ())
        )
        message = safe_validation_message(error_type)
        detail = f"{message} [{error_type}]"
        rendered.append(f"{location}: {detail}" if location else detail)
    if len(errors) > max_errors:
        rendered.append(f"… and {len(errors) - max_errors} more validation error(s)")
    summary = "; ".join(rendered) or "Invalid value"
    return summary[:max_chars]


def validate_json_object(
    value: dict[str, Any],
    *,
    label: str,
    max_bytes: int = MAX_JSON_BYTES,
    max_nodes: int = MAX_JSON_NODES,
    max_depth: int = MAX_JSON_DEPTH,
    max_key_chars: int = MAX_JSON_KEY_CHARS,
) -> dict[str, Any]:
    """Validate a finite JSON object without recursively walking attacker input."""

    if min(max_bytes, max_nodes, max_depth, max_key_chars) < 1:
        raise ValueError("JSON validation limits must be positive")
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    total_chars = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"{label} exceeds the {max_nodes} node limit")
        if depth > max_depth:
            raise ValueError(f"{label} exceeds the {max_depth} level nesting limit")
        if type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                raise ValueError(f"{label} contains a repeated or circular container")
            seen_containers.add(identity)
            remaining = max_nodes - nodes - len(stack)
            if len(current) > remaining:
                raise ValueError(f"{label} exceeds the {max_nodes} node limit")
            for key, nested in current.items():
                if type(key) is not str:
                    raise ValueError(f"{label} keys must be strings")
                if not key or len(key) > max_key_chars:
                    raise ValueError(f"{label} keys must be 1-{max_key_chars} characters")
                if len(key) > max_bytes - total_chars:
                    raise ValueError(f"{label} exceeds the {max_bytes} byte limit")
                total_chars += len(key)
                if "\x00" in key:
                    raise ValueError(f"{label} keys must not contain U+0000")
                stack.append((nested, depth + 1))
        elif type(current) is list:
            identity = id(current)
            if identity in seen_containers:
                raise ValueError(f"{label} contains a repeated or circular container")
            seen_containers.add(identity)
            remaining = max_nodes - nodes - len(stack)
            if len(current) > remaining:
                raise ValueError(f"{label} exceeds the {max_nodes} node limit")
            for nested in current:
                stack.append((nested, depth + 1))
        elif type(current) is str:
            if len(current) > max_bytes - total_chars:
                raise ValueError(f"{label} exceeds the {max_bytes} byte limit")
            total_chars += len(current)
            if "\x00" in current:
                raise ValueError(f"{label} strings must not contain U+0000")
        elif current is None or type(current) is bool:
            continue
        elif type(current) is int:
            if current.bit_length() > 256:
                raise ValueError(f"{label} integers must not exceed 256 bits")
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError(f"{label} numbers must be finite")
        else:
            # Do not interpolate type metadata or coerce an unsupported value:
            # hostile subclasses may attach arbitrary metaclass/string hooks.
            raise ValueError(f"{label} contains unsupported values")

    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        pass
    if encoded is None:
        raise ValueError(f"{label} must contain finite JSON values")
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes} byte limit")
    return value
