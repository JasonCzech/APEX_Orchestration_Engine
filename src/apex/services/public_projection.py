"""Secret-free bounded projections for operational provider state."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import (
    MAX_JSON_KEY_CHARS,
    MAX_RESOURCE_ID_CHARS,
    validate_json_object,
)

MAX_PUBLIC_SLA_BREACHES = 16
MAX_PUBLIC_SLA_BREACH_CHARS = 512
MAX_PUBLIC_RESULT_NOTES_CHARS = 2_048
MAX_NATIVE_PAGE_BYTES = 32 * 1024 * 1024
MAX_NATIVE_PAGE_NODES = 200_000
MAX_NATIVE_PAGE_DEPTH = 32
MAX_PUBLIC_ENGINE_HANDLE_BYTES = 64 * 1024
MAX_PUBLIC_TEST_RESULT_BYTES = 512 * 1024
_NATIVE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9_.:-]+\Z")


def validated_native_identifier(value: Any, *, label: str) -> str:
    """Validate an identifier returned by the loopback LangGraph service.

    Thread and run IDs are subsequently reflected in JSON, interpolated into a
    stream URL, and sometimes sent back to the SDK for cancellation. Treat the
    loopback response as an integration boundary: malformed, credential-shaped,
    or path-delimiting IDs must fail closed instead of becoming response or
    request-target material.
    """

    if (
        type(value) is not str
        or not value
        or value in {".", ".."}
        or len(value) > MAX_RESOURCE_ID_CHARS
        or _NATIVE_IDENTIFIER.fullmatch(value) is None
        or bounded_diagnostic(value, max_chars=max(1, len(value))) != value
    ):
        raise RuntimeError(f"{label} returned an invalid identifier")
    return value


def native_run_stream_url(thread_id: Any, run_id: Any) -> str:
    """Build a relative SDK stream URL from validated, encoded path segments."""

    safe_thread_id = validated_native_identifier(thread_id, label="native thread")
    safe_run_id = validated_native_identifier(run_id, label="native run")
    return (
        f"/threads/{quote(safe_thread_id, safe='')}/runs/"
        f"{quote(safe_run_id, safe='')}/stream?stream_mode=custom"
    )


def validated_native_mapping_page(
    value: Any,
    *,
    requested_limit: int,
    label: str,
    max_bytes: int = MAX_NATIVE_PAGE_BYTES,
    max_nodes: int = MAX_NATIVE_PAGE_NODES,
    max_depth: int = MAX_NATIVE_PAGE_DEPTH,
) -> list[dict[str, Any]]:
    """Require a bounded JSON mapping page that honors its requested item cap."""

    if (
        type(requested_limit) is not int
        or requested_limit < 0
        or type(max_bytes) is not int
        or max_bytes < 1
        or type(max_nodes) is not int
        or max_nodes < 1
        or type(max_depth) is not int
        or max_depth < 0
        or type(value) is not list
        or len(value) > requested_limit
        or any(type(item) is not dict for item in value)
    ):
        raise RuntimeError(f"{label} returned an invalid or oversized page")
    if not _native_page_within_serialized_budget(
        value,
        max_bytes=max_bytes,
        max_nodes=max_nodes,
        max_depth=max_depth,
    ):
        raise RuntimeError(f"{label} returned an invalid or oversized page")
    invalid_page = False
    try:
        validate_json_object(
            {"items": value},
            label=label,
            max_bytes=max_bytes,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
    except (TypeError, ValueError):
        invalid_page = True
    if invalid_page:
        raise RuntimeError(f"{label} returned an invalid or oversized page")
    return value


def _native_page_within_serialized_budget(
    value: list[dict[str, Any]],
    *,
    max_bytes: int,
    max_nodes: int,
    max_depth: int,
) -> bool:
    """Conservatively bound compact JSON bytes before serializing the page.

    The shared validator serializes ``{"items": value}``. Count that same
    wrapper, punctuation, and JSON string escapes incrementally so a provider
    cannot turn a raw string near the limit into a much larger temporary
    allocation through quotes, backslashes, or control characters. Integer
    and float encodings use small safe upper bounds instead of allocating
    scalar strings during this preflight.
    """

    if max_bytes < 1 or max_nodes < 1 or max_depth < 0:
        return False
    remaining = max_bytes
    nodes = 0
    stack: list[tuple[Any, int]] = [({"items": value}, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        if type(current) is dict:
            if len(current) > max_nodes - nodes - len(stack):
                return False
            remaining -= 2 + max(0, len(current) - 1) + len(current)
            if remaining < 0:
                return False
            for key, nested in current.items():
                if (
                    type(key) is not str
                    or not key
                    or len(key) > MAX_JSON_KEY_CHARS
                    or "\x00" in key
                ):
                    return False
                remaining = _remaining_after_json_string(key, remaining)
                if remaining < 0:
                    return False
                stack.append((nested, depth + 1))
        elif type(current) is list:
            if len(current) > max_nodes - nodes - len(stack):
                return False
            remaining -= 2 + max(0, len(current) - 1)
            if remaining < 0:
                return False
            stack.extend((nested, depth + 1) for nested in current)
        elif type(current) is str:
            remaining = _remaining_after_json_string(current, remaining)
            if remaining < 0:
                return False
        elif current is None:
            remaining -= 4
        elif type(current) is bool:
            remaining -= 4 if current else 5
        elif type(current) is int:
            bit_length = int.bit_length(current)
            # 0.30103 is a slight upper bound on log10(2). Add one for
            # the first digit and one for a possible minus sign.
            remaining -= max(1, (bit_length * 30_103) // 100_000 + 1) + (1 if current < 0 else 0)
        elif type(current) is float:
            # Finite binary64 JSON encodings fit comfortably within 32 bytes.
            # Non-finite values are rejected by the shared validator below.
            remaining -= 32
        else:
            return False
        if remaining < 0:
            return False
    return True


def _remaining_after_json_string(value: str, remaining: int) -> int:
    """Return bytes left after a compact, non-ASCII-escaped JSON string."""

    if len(value) + 2 > remaining:
        return -1
    consumed = 2  # surrounding quotes
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or codepoint in {8, 9, 10, 12, 13}:
            consumed += 2
        elif codepoint < 0x20:
            consumed += 6
        elif codepoint <= 0x7F:
            consumed += 1
        elif codepoint <= 0x7FF:
            consumed += 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            return -1
        elif codepoint <= 0xFFFF:
            consumed += 3
        else:
            consumed += 4
        if consumed > remaining:
            return -1
    return remaining - consumed


def public_engine_handle_summary(value: Any) -> dict[str, str | None] | None:
    """Return only the two engine-handle fields consumed by dashboard reads."""

    if type(value) is not dict:
        return None
    try:
        validate_json_object(
            value,
            label="public engine handle",
            max_bytes=MAX_PUBLIC_ENGINE_HANDLE_BYTES,
            max_nodes=128,
            max_depth=4,
            max_key_chars=128,
        )
    except ValueError:
        return None
    engine = value.get("engine")
    if type(engine) is not str or not engine or len(engine) > 64 or "\x00" in engine:
        return None
    safe_engine = bounded_diagnostic(engine, max_chars=len(engine))
    external_run_id = value.get("external_run_id")
    if external_run_id is not None and (
        type(external_run_id) is not str or len(external_run_id) > 255 or "\x00" in external_run_id
    ):
        external_run_id = None
    if type(external_run_id) is str:
        external_run_id = bounded_diagnostic(
            external_run_id,
            max_chars=max(1, len(external_run_id)),
        )
    return {
        "engine": safe_engine,
        "external_run_id": external_run_id if type(external_run_id) is str else None,
    }


def public_test_result_summary(value: Any) -> dict[str, Any] | None:
    """Return only the bounded normalized result schema from durable JSON."""

    if type(value) is not dict:
        return None
    try:
        validate_json_object(
            value,
            label="public test result",
            max_bytes=MAX_PUBLIC_TEST_RESULT_BYTES,
            max_nodes=512,
            max_depth=4,
            max_key_chars=128,
        )
    except (TypeError, ValueError):
        return None

    # Legacy provider rows can predate both ``extra=forbid`` and durable
    # credential rejection. Project independently safe schema fields instead of
    # feeding the complete row to Pydantic: one credential-named extension or KPI
    # must be omitted without hiding the otherwise useful bounded summary.
    engine = value.get("engine")
    passed = value.get("passed")
    raw_kpis = value.get("kpis", {})
    raw_breaches = value.get("sla_breaches", [])
    notes = value.get("notes")
    if (
        type(engine) is not str
        or not 1 <= len(engine) <= 64
        or type(passed) is not bool
        or type(raw_kpis) is not dict
        or len(raw_kpis) > 64
        or type(raw_breaches) is not list
        or len(raw_breaches) > 128
        or (notes is not None and (type(notes) is not str or len(notes) > 20_000))
    ):
        return None
    kpis: dict[str, int | float] = {}
    for name, metric in raw_kpis.items():
        if (
            type(name) is not str
            or not name.strip()
            or len(name) > 64
            or type(metric) not in {int, float}
            or (type(metric) is int and metric.bit_length() > 256)
            or not math.isfinite(metric)
            or abs(metric) > 1_000_000_000_000
        ):
            continue
        if bounded_diagnostic(name, max_chars=max(1, len(name))) == name:
            kpis[name] = metric
    if any(type(item) is not str or not item.strip() or len(item) > 2_048 for item in raw_breaches):
        return None
    payload: dict[str, Any] = {
        "engine": bounded_diagnostic(engine, max_chars=max(1, len(engine))),
        "passed": passed,
        "kpis": kpis,
        "sla_breaches": [
            bounded_diagnostic(
                item,
                max_chars=min(max(1, len(item)), MAX_PUBLIC_SLA_BREACH_CHARS),
            )
            for item in raw_breaches[:MAX_PUBLIC_SLA_BREACHES]
        ],
        "notes": None,
    }
    if type(notes) is str:
        payload["notes"] = bounded_diagnostic(
            notes,
            max_chars=min(max(1, len(notes)), MAX_PUBLIC_RESULT_NOTES_CHARS),
        )
    return payload
