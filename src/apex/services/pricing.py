"""Small in-repo pricing table for agent analytics cost snapshots.

Prices are USD per million tokens. They are intentionally local and best-effort:
unknown models return no cost instead of blocking analytics capture.
"""

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

MAX_TOKEN_COUNT = 10_000_000_000
_MAX_TOKEN_TEXT_CHARS = 32
MAX_USAGE_METADATA_FIELDS = 64
MAX_USAGE_METADATA_KEY_CHARS = 128

MODEL_PRICING_USD_PER_MTOK: dict[str, dict[str, Decimal]] = {
    # Current Claude catalog (USD per million tokens). Cache reads bill at ~0.1x
    # input; 5-minute cache writes at ~1.25x input.
    "claude-opus-4-8": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
        "cache_read": Decimal("0.50"),
        "cache_creation": Decimal("6.25"),
    },
    "claude-opus-4-7": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
        "cache_read": Decimal("0.50"),
        "cache_creation": Decimal("6.25"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_creation": Decimal("3.75"),
    },
    "claude-haiku-4-5": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_read": Decimal("0.10"),
        "cache_creation": Decimal("1.25"),
    },
    "claude-3-5-haiku-20241022": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
        "cache_read": Decimal("0.08"),
        "cache_creation": Decimal("1.00"),
    },
    "claude-fable-5": {
        "input": Decimal("10.00"),
        "output": Decimal("50.00"),
        "cache_read": Decimal("1.00"),
        "cache_creation": Decimal("12.50"),
    },
    "gpt-4o": {
        "input": Decimal("2.50"),
        "output": Decimal("10.00"),
        "cache_read": Decimal("1.25"),
        "cache_creation": Decimal("2.50"),
    },
    "gpt-4o-mini": {
        "input": Decimal("0.15"),
        "output": Decimal("0.60"),
        "cache_read": Decimal("0.075"),
        "cache_creation": Decimal("0.15"),
    },
}

# Keep aliases exact and reviewable. Prefix matching would silently price unknown
# future model generations or provider-specific deployment names at stale rates.
MODEL_PRICING_ALIASES: dict[str, str] = {
    "claude-sonnet-4-5": "claude-sonnet-4-6",
    "claude-sonnet-4-20250514": "claude-sonnet-4-6",
    "claude-3-5-sonnet-latest": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-5-haiku-latest": "claude-3-5-haiku-20241022",
}


def coerce_token_count(value: Any) -> int:
    """Return a finite, non-negative token count safe for storage and costing."""

    if value is None or type(value) is bool:
        return 0
    if type(value) is int:
        return min(max(value, 0), MAX_TOKEN_COUNT)
    if type(value) is Decimal:
        if not value.is_finite() or value <= 0:
            return 0
        if value >= MAX_TOKEN_COUNT:
            return MAX_TOKEN_COUNT
        return int(value)
    if type(value) is float and not math.isfinite(value):
        return 0
    if type(value) is float:
        parsed = int(value)
        return min(max(parsed, 0), MAX_TOKEN_COUNT)
    if type(value) not in {str, bytes, bytearray}:
        return 0
    text_value = cast(str | bytes | bytearray, value)
    if len(text_value) > _MAX_TOKEN_TEXT_CHARS:
        return 0
    try:
        parsed = int(text_value)
    except (OverflowError, TypeError, ValueError):
        return 0
    return min(max(parsed, 0), MAX_TOKEN_COUNT)


def normalize_usage_mapping(value: Any) -> dict[str, Any] | None:
    """Copy a small exact-key metadata dict without invoking attacker key hooks."""

    if type(value) is not dict or len(value) > MAX_USAGE_METADATA_FIELDS:
        return None
    normalized: dict[str, Any] = {}
    # ``dict.items`` on an exact built-in dictionary yields stored references;
    # it does not hash, compare, or stringify custom key objects. Reject every
    # non-exact key before inserting into the trusted copy used for lookups.
    for key, item in dict.items(value):
        if type(key) is not str or len(key) > MAX_USAGE_METADATA_KEY_CHARS:
            return None
        normalized[key] = item
    return normalized


def normalize_cache_token_counts(
    input_tokens: int,
    cache_read_tokens: Any,
    cache_creation_tokens: Any,
) -> tuple[int, int]:
    """Return a deterministic cache breakdown contained by total input tokens.

    LangChain defines both cache fields as details of ``input_tokens``. Provider
    drift or malformed metadata can nevertheless report a detail sum larger than
    the total. Preserve the reported ratio when scaling that impossible breakdown
    back to the input total; assign the integer remainder to cache creation so the
    cost snapshot is conservative for the models in the local pricing table.
    """

    bounded_input = coerce_token_count(input_tokens)
    cache_read = coerce_token_count(cache_read_tokens)
    cache_creation = coerce_token_count(cache_creation_tokens)
    detail_total = cache_read + cache_creation
    if detail_total <= bounded_input:
        return cache_read, cache_creation
    if bounded_input == 0:
        return 0, 0
    normalized_read = cache_read * bounded_input // detail_total
    return normalized_read, bounded_input - normalized_read


def compute_cost(
    model: str | None, usage: Mapping[str, Any]
) -> tuple[Decimal | None, dict[str, str] | None]:
    """Return (cost_usd, pricing_snapshot) for known models."""
    safe_usage = normalize_usage_mapping(usage)
    if type(model) is not str or not model or safe_usage is None:
        return None, None
    pricing = MODEL_PRICING_USD_PER_MTOK.get(MODEL_PRICING_ALIASES.get(model, model))
    if pricing is None:
        return None, None
    million = Decimal("1000000")
    # In LangChain's normalized usage_metadata, `input_tokens` is the TOTAL input and
    # cache_read/cache_creation are a *subset* of it (the breakdown). Bill only the
    # uncached remainder at the full input rate, then the cached tokens at their own
    # rates — otherwise cached tokens are double-counted. Clamp so malformed/negative
    # counts can never produce a negative cost.
    input_tokens = coerce_token_count(safe_usage.get("input_tokens"))
    output_tokens = coerce_token_count(safe_usage.get("output_tokens"))
    cache_read, cache_creation = normalize_cache_token_counts(
        input_tokens,
        safe_usage.get("cache_read_tokens"),
        safe_usage.get("cache_creation_tokens"),
    )
    uncached_input = input_tokens - cache_read - cache_creation
    cost = (
        Decimal(uncached_input) * pricing["input"]
        + Decimal(output_tokens) * pricing["output"]
        + Decimal(cache_read) * pricing["cache_read"]
        + Decimal(cache_creation) * pricing["cache_creation"]
    ) / million
    snapshot = {key: str(value) for key, value in pricing.items()}
    return cost.quantize(Decimal("0.000001")), snapshot
