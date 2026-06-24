"""Pure-function tests for agent-analytics cost + usage normalization (no DB).

These run in the coverage-gated `checks` job; the broader SQL round-trip lives in
the Postgres-gated test_agent_analytics_db.py.
"""

from decimal import Decimal

from apex.services.pricing import compute_cost
from apex.services.usage import normalize_usage_metadata


def test_compute_cost_subtracts_cached_tokens_from_full_input_rate() -> None:
    # claude-sonnet-4 per-MTok: input 3.00, output 15.00, cache_read 0.30, cache_creation 3.75.
    # input_tokens INCLUDES the cached tokens, so only the uncached remainder bills at
    # the full input rate: (1000-50-20)*3 + 100*15 + 50*0.30 + 20*3.75 = 4380 / 1e6.
    cost, snapshot = compute_cost(
        "claude-sonnet-4-20250514",
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_tokens": 50,
            "cache_creation_tokens": 20,
        },
    )
    assert cost == Decimal("0.004380")
    assert snapshot == {
        "input": "3.00",
        "output": "15.00",
        "cache_read": "0.30",
        "cache_creation": "3.75",
    }


def test_compute_cost_without_cache_details() -> None:
    cost, _ = compute_cost("gpt-4o-mini", {"input_tokens": 1000, "output_tokens": 500})
    # 1000*0.15 + 500*0.60 = 450 / 1e6
    assert cost == Decimal("0.000450")


def test_compute_cost_unknown_or_missing_model_returns_none() -> None:
    assert compute_cost("not-a-real-model", {"input_tokens": 100}) == (None, None)
    assert compute_cost(None, {"input_tokens": 100}) == (None, None)


def test_compute_cost_never_negative_on_malformed_counts() -> None:
    cost, _ = compute_cost(
        "claude-3-5-haiku-latest",
        {"input_tokens": -10, "output_tokens": -5, "cache_read_tokens": 999},
    )
    assert cost is not None
    assert cost >= 0


def test_normalize_usage_metadata_flattens_nested_details_and_aliases() -> None:
    normalized = normalize_usage_metadata(
        {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "input_token_details": {
                "cache_read_input_tokens": 200,  # provider alias for cache_read
                "cache_creation": 50,
            },
            "output_token_details": {"reasoning": 80},
        }
    )
    assert normalized == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 50,
        "reasoning_tokens": 80,
    }


def test_normalize_usage_metadata_defaults_and_total_fallback() -> None:
    assert normalize_usage_metadata(None)["total_tokens"] == 0
    # total_tokens falls back to input + output when the provider omits it.
    normalized = normalize_usage_metadata({"input_tokens": 10, "output_tokens": 4})
    assert normalized["total_tokens"] == 14
    assert normalized["cache_read_tokens"] == 0
    assert normalized["reasoning_tokens"] == 0
