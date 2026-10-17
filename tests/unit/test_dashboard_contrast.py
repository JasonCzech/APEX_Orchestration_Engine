"""Regressions for the dashboard contrast check's token alias resolution."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "dashboard_contrast", Path(__file__).parents[2] / "scripts/check_dashboard_contrast.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_color_aliases_resolve_to_the_final_color():
    assert module.token_color({"--a": "var(--b)", "--b": "#ffffff"}, "--a") == [255, 255, 255, 1]


def test_color_alias_cycle_is_not_silently_accepted():
    with pytest.raises(ValueError, match="Circular"):
        module.token_color({"--a": "var(--b)", "--b": "var(--a)"}, "--a")


def test_all_shipped_theme_pairs_meet_text_contrast():
    module.main()
