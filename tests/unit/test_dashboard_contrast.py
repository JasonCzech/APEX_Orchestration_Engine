"""Regressions for the dashboard contrast check's token alias resolution."""

import importlib.util
import re
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


@pytest.mark.parametrize("token,value", [("--chart-1", "#00a8e0"), ("--chart-3", "#e5920a")])
def test_chart_series_below_three_to_one_fail_even_when_text_passes(
    tmp_path, monkeypatch, token, value
):
    for filename in ("tokens.css", "themes.css", "apex-light.css", "accessibility.css"):
        css = (module.THEME_DIR / filename).read_text()
        if filename == "apex-light.css":
            css = re.sub(rf"{token}: #[0-9a-f]+;", f"{token}: {value};", css)
        (tmp_path / filename).write_text(css)
    monkeypatch.setattr(module, "THEME_DIR", tmp_path)
    with pytest.raises(SystemExit, match=f"apex-light: {token} 2\\."):
        module.main()


def test_calendar_mix_uses_premultiplied_alpha():
    tokens = {
        "--a": "rgba(255, 0, 0, 0.5)",
        "--b": "#0000ff",
        "--mix": "color-mix(in srgb, var(--a) 50%, var(--b))",
    }
    assert module.token_color(tokens, "--mix") == pytest.approx([85, 0, 170, 0.75])


def test_calendar_mix_rejects_cycles():
    with pytest.raises(ValueError, match="Circular"):
        module.token_color(
            {"--a": "color-mix(in srgb, var(--a) 50%, var(--b))", "--b": "#ffffff"}, "--a"
        )


def test_transparent_calendar_mix_has_no_visible_color():
    assert module.token_color(
        {
            "--a": "rgba(255, 0, 0, 0)",
            "--b": "rgba(0, 0, 255, 0)",
            "--mix": "color-mix(in srgb, var(--a) 50%, var(--b))",
        },
        "--mix",
    ) == [0, 0, 0, 0]
