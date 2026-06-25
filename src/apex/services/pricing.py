"""Small in-repo pricing table for agent analytics cost snapshots.

Prices are USD per million tokens. They are intentionally local and best-effort:
unknown models return no cost instead of blocking analytics capture.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

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


def compute_cost(
    model: str | None, usage: Mapping[str, Any]
) -> tuple[Decimal | None, dict[str, str] | None]:
    """Return (cost_usd, pricing_snapshot) for known models."""
    if not model:
        return None, None
    pricing = MODEL_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return None, None
    million = Decimal("1000000")
    # In LangChain's normalized usage_metadata, `input_tokens` is the TOTAL input and
    # cache_read/cache_creation are a *subset* of it (the breakdown). Bill only the
    # uncached remainder at the full input rate, then the cached tokens at their own
    # rates — otherwise cached tokens are double-counted. Clamp so malformed/negative
    # counts can never produce a negative cost.
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    cache_read = max(0, int(usage.get("cache_read_tokens") or 0))
    cache_creation = max(0, int(usage.get("cache_creation_tokens") or 0))
    uncached_input = max(0, input_tokens - cache_read - cache_creation)
    cost = (
        Decimal(uncached_input) * pricing["input"]
        + Decimal(output_tokens) * pricing["output"]
        + Decimal(cache_read) * pricing["cache_read"]
        + Decimal(cache_creation) * pricing["cache_creation"]
    ) / million
    snapshot = {key: str(value) for key, value in pricing.items()}
    return cost.quantize(Decimal("0.000001")), snapshot
