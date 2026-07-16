"""Pure-function tests for agent-analytics cost + usage normalization (no DB).

These run in the coverage-gated `checks` job; the broader SQL round-trip lives in
the Postgres-gated test_agent_analytics_db.py.
"""

from decimal import Decimal
from typing import Any, cast

import pytest

from apex.services.pricing import MAX_TOKEN_COUNT, compute_cost
from apex.services.usage import normalize_usage_metadata
from apex.settings import LLMSettings


def test_compute_cost_subtracts_cached_tokens_from_full_input_rate() -> None:
    # claude-sonnet-4-6 per-MTok: input 3.00, output 15.00, cache_read 0.30, cache_creation 3.75.
    # input_tokens INCLUDES the cached tokens, so only the uncached remainder bills at
    # the full input rate: (1000-50-20)*3 + 100*15 + 50*0.30 + 20*3.75 = 4380 / 1e6.
    cost, snapshot = compute_cost(
        "claude-sonnet-4-6",
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


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5",
        "claude-sonnet-4-20250514",
        "claude-3-5-sonnet-latest",
    ],
)
def test_compute_cost_prices_exact_sonnet_aliases_used_by_runtime(model: str) -> None:
    cost, snapshot = compute_cost(model, {"input_tokens": 1_000, "output_tokens": 100})

    assert cost == Decimal("0.004500")
    assert snapshot is not None
    assert snapshot["input"] == "3.00"
    assert snapshot["output"] == "15.00"


def test_compute_cost_prices_allowed_claude_35_haiku_alias() -> None:
    cost, snapshot = compute_cost(
        "claude-3-5-haiku-latest",
        {"input_tokens": 1_000, "output_tokens": 100},
    )

    assert cost == Decimal("0.001200")
    assert snapshot == {
        "input": "0.80",
        "output": "4.00",
        "cache_read": "0.08",
        "cache_creation": "1.00",
    }


def test_compute_cost_does_not_prefix_match_unreviewed_model_names() -> None:
    assert compute_cost("claude-sonnet-4-5-custom", {"input_tokens": 100}) == (None, None)


def test_every_default_allowed_runtime_model_has_reviewed_pricing() -> None:
    for model in LLMSettings().allowed_models:
        cost, snapshot = compute_cost(model, {"input_tokens": 1})
        assert cost is not None, model
        assert snapshot is not None, model


def test_compute_cost_never_negative_on_malformed_counts() -> None:
    cost, _ = compute_cost(
        "claude-haiku-4-5",
        {"input_tokens": -10, "output_tokens": -5, "cache_read_tokens": 999},
    )
    assert cost is not None
    assert cost >= 0


def test_compute_cost_bounds_malformed_or_oversized_provider_counts() -> None:
    cost, _ = compute_cost(
        "claude-haiku-4-5",
        {
            "input_tokens": Decimal("Infinity"),
            "output_tokens": "9" * 100_000,
            "cache_read_tokens": float("inf"),
            "cache_creation_tokens": MAX_TOKEN_COUNT + 1,
        },
    )

    # Cache details are subsets of input_tokens. An impossible cache-creation
    # count cannot create cost when the provider reports zero input.
    assert cost == Decimal("0.000000")


def test_compute_cost_scales_impossible_cache_breakdown_within_input_total() -> None:
    cost, _ = compute_cost(
        "claude-sonnet-4-6",
        {
            "input_tokens": 10,
            "output_tokens": 0,
            "cache_read_tokens": 100,
            "cache_creation_tokens": 300,
        },
    )

    # The 1:3 reported ratio becomes 2 read + 8 creation tokens (integer
    # remainder conservatively assigned to creation), never 400 billable inputs.
    assert cost == Decimal("0.000031")


def test_compute_cost_does_not_execute_hostile_provider_scalar_or_mapping_hooks() -> None:
    class HostileCount:
        called = False

        def __int__(self) -> int:
            self.called = True
            raise AssertionError("provider scalar conversion must not execute")

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).called = True
                raise AssertionError("provider __class__ descriptor must not execute")
            return object.__getattribute__(self, name)

    class HostileUsage(dict[str, Any]):
        called = False

        def get(self, key: str, default: Any = None) -> Any:
            del key, default
            self.called = True
            raise AssertionError("provider mapping hooks must not execute")

    count = HostileCount()
    cost, _snapshot = compute_cost("gpt-4o", {"input_tokens": count})

    assert cost == Decimal("0.000000")
    assert count.called is False
    usage = HostileUsage(input_tokens=1)
    assert compute_cost("gpt-4o", usage) == (None, None)
    assert usage.called is False


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


def test_normalize_usage_metadata_bounds_non_finite_and_oversized_counts() -> None:
    normalized = normalize_usage_metadata(
        {
            "input_tokens": float("inf"),
            "output_tokens": MAX_TOKEN_COUNT + 1,
            "total_tokens": Decimal("NaN"),
            "input_token_details": {"cache_read": "9" * 100_000},
        }
    )

    assert normalized["input_tokens"] == 0
    assert normalized["output_tokens"] == MAX_TOKEN_COUNT
    assert normalized["total_tokens"] == MAX_TOKEN_COUNT
    assert normalized["cache_read_tokens"] == 0


def test_normalize_usage_metadata_reconciles_totals_and_detail_subsets() -> None:
    normalized = normalize_usage_metadata(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": MAX_TOKEN_COUNT,
            "input_token_details": {"cache_read": 100, "cache_creation": 300},
            "output_token_details": {"reasoning": 999},
        }
    )

    assert normalized == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cache_read_tokens": 2,
        "cache_creation_tokens": 8,
        "reasoning_tokens": 4,
    }


def test_normalize_usage_metadata_keeps_total_equal_at_component_caps() -> None:
    normalized = normalize_usage_metadata(
        {"input_tokens": MAX_TOKEN_COUNT, "output_tokens": MAX_TOKEN_COUNT}
    )

    assert normalized["total_tokens"] == 2 * MAX_TOKEN_COUNT
    assert normalized["total_tokens"] == (normalized["input_tokens"] + normalized["output_tokens"])


def test_normalize_usage_metadata_never_executes_custom_mapping_hooks() -> None:
    class HostileUsage(dict[str, Any]):
        called = False

        def __bool__(self) -> bool:
            self.called = True
            raise AssertionError("provider mapping truthiness must not execute")

        def get(self, key: str, default: Any = None) -> Any:
            del key, default
            self.called = True
            raise AssertionError("provider mapping get hook must not execute")

    class HostileDetails(dict[str, Any]):
        called = False

        def get(self, key: str, default: Any = None) -> Any:
            del key, default
            self.called = True
            raise AssertionError("provider detail get hook must not execute")

    usage = HostileUsage(input_tokens=10)
    assert normalize_usage_metadata(usage) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert usage.called is False

    details = HostileDetails(cache_read=10)
    normalized = normalize_usage_metadata({"input_tokens": 10, "input_token_details": details})
    assert normalized["cache_read_tokens"] == 0
    assert details.called is False


def test_usage_metadata_never_compares_or_hashes_hostile_mapping_keys() -> None:
    class CollidingKey:
        compared = False
        hashed = False

        def __hash__(self) -> int:
            self.hashed = True
            return hash("input_tokens")

        def __eq__(self, _other: object) -> bool:
            self.compared = True
            raise AssertionError("provider key comparison must not execute")

    key = CollidingKey()
    raw = {key: 999}
    key.hashed = False  # dictionary construction legitimately hashes the key once

    assert normalize_usage_metadata(cast(Any, raw)) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert compute_cost("gpt-4o", cast(Any, raw)) == (None, None)
    assert key.hashed is False
    assert key.compared is False
