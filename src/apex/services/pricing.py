"""Small in-repo pricing table for agent analytics cost snapshots.

Prices are USD per million tokens. They are intentionally local and best-effort:
unknown models return no cost instead of blocking analytics capture.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

MODEL_PRICING_USD_PER_MTOK: dict[str, dict[str, Decimal]] = {
    "claude-3-5-sonnet-latest": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_creation": Decimal("3.75"),
    },
    "claude-sonnet-4-20250514": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_creation": Decimal("3.75"),
    },
    "claude-opus-4-20250514": {
        "input": Decimal("15.00"),
        "output": Decimal("75.00"),
        "cache_read": Decimal("1.50"),
        "cache_creation": Decimal("18.75"),
    },
    "claude-3-5-haiku-latest": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
        "cache_read": Decimal("0.08"),
        "cache_creation": Decimal("1.00"),
    },
    "claude-3-5-haiku-20241022": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
        "cache_read": Decimal("0.08"),
        "cache_creation": Decimal("1.00"),
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
    cost = (
        Decimal(int(usage.get("input_tokens") or 0)) * pricing["input"]
        + Decimal(int(usage.get("output_tokens") or 0)) * pricing["output"]
        + Decimal(int(usage.get("cache_read_tokens") or 0)) * pricing["cache_read"]
        + Decimal(int(usage.get("cache_creation_tokens") or 0)) * pricing["cache_creation"]
    ) / million
    snapshot = {key: str(value) for key, value in pricing.items()}
    return cost.quantize(Decimal("0.000001")), snapshot
