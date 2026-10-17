"""Check shared dashboard text/control and chart tokens against contrast formulas.

Covers the six shipped palettes and alpha-composited shared surfaces. This is
not a substitute for testing rendered focus, overlays, chart encodings, and typography.
"""

from __future__ import annotations

import re
from pathlib import Path

THEME_DIR = Path(__file__).resolve().parents[1] / "apps/dashboard/src/theme"


def color(value: str) -> list[float]:
    if value.startswith("#"):
        return [float(int(value[i : i + 2], 16)) for i in (1, 3, 5)] + [1.0]
    channels = [
        float(part.strip()) for part in value[value.index("(") + 1 : value.index(")")].split(",")
    ]
    return channels if len(channels) == 4 else channels + [1.0]


def composite(foreground: list[float], background: list[float]) -> list[float]:
    return [
        foreground[i] * foreground[3] + background[i] * (1 - foreground[3]) for i in range(3)
    ] + [1.0]


def luminance(rgb: list[float]) -> float:
    channels = [channel / 255 for channel in rgb[:3]]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(
        weight * channel for weight, channel in zip([0.2126, 0.7152, 0.0722], linear, strict=True)
    )


def contrast(a: list[float], b: list[float]) -> float:
    low, high = sorted([luminance(a), luminance(b)])
    return (high + 0.05) / (low + 0.05)


def token_color(
    tokens: dict[str, str], key: str, seen: frozenset[str] = frozenset()
) -> list[float]:
    """Resolve aliases and two-token sRGB surface mixes, rejecting cycles."""
    if key in seen:
        raise ValueError(f"Circular color token: {key}")
    value = tokens[key].strip()
    alias = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if alias:
        return token_color(tokens, alias[1], seen | {key})
    mix = re.fullmatch(
        r"color-mix\(in srgb, var\((--[\w-]+)\) ([\d.]+)%, var\((--[\w-]+)\)\)", value
    )
    if mix:
        first = token_color(tokens, mix[1], seen | {key})
        second = token_color(tokens, mix[3], seen | {key})
        weight = float(mix[2]) / 100
        alpha = first[3] * weight + second[3] * (1 - weight)
        if alpha == 0:
            return [0.0, 0.0, 0.0, 0.0]
        return [
            (first[i] * first[3] * weight + second[i] * second[3] * (1 - weight)) / alpha
            for i in range(3)
        ] + [alpha]
    return color(value)


def main() -> None:
    defaults: dict[str, str] = {}
    themes: dict[str, dict[str, str]] = {
        name: {}
        for name in [
            "dark",
            "light",
            "apex-light",
            "solarized-dark",
            "solarized-light",
            "monokai-dimmed",
        ]
    }
    for filename in ["tokens.css", "themes.css", "apex-light.css", "accessibility.css"]:
        for selector, body in re.findall(
            r"(:root(?:\[data-theme='[^']+'\])?)\s*\{([^}]+)\}", (THEME_DIR / filename).read_text()
        ):
            match = re.search(r"data-theme='([^']+)'", selector)
            values = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
            if match:
                themes[match[1]].update(values)
            else:
                defaults.update(values)
    failures: list[str] = []
    for name, overrides in themes.items():
        tokens = {**defaults, **overrides}
        root = color(tokens["--bg-primary"])
        backgrounds = [root] + [
            composite(color(tokens[key]), root)
            for key in ["--bg-secondary", "--bg-card", "--bg-surface", "--bg-elevated"]
        ]
        # Selected rows and human dialogue use a translucent accent overlay.
        backgrounds += [composite(color(tokens["--accent-soft"]), bg) for bg in backgrounds]
        ratios: list[float] = []

        def check(
            label: str,
            ink: list[float],
            background: list[float],
            ratios: list[float] = ratios,
            name: str = name,
            minimum: float = 4.5,
        ) -> None:
            ratio = contrast(composite(ink, background), background)
            ratios.append(ratio)
            if ratio < minimum:
                failures.append(f"{name}: {label} {ratio:.2f}:1")

        for key in ["--text-primary", "--text-secondary", "--text-muted", "--accent-text"]:
            for background in backgrounds:
                check(key, token_color(tokens, key), background)
        for tone in ["success", "warning", "danger", "info"]:
            for background in backgrounds:
                fill = composite(color(tokens[f"--{tone}-bg"]), background)
                check(tone, color(tokens[f"--{tone}-text"]), fill)
        for fill in ["--accent", "--accent-hover"]:
            check("primary button", color(tokens["--accent-contrast"]), color(tokens[fill]))
        check(
            "danger button",
            token_color(tokens, "--danger-fill-text"),
            token_color(tokens, "--danger-fill"),
        )
        check(
            "warning fill",
            token_color(tokens, "--warning-fill-text"),
            token_color(tokens, "--warning"),
        )
        for background in backgrounds:
            check(
                "tag",
                token_color(tokens, "--tag-text"),
                composite(token_color(tokens, "--tag-bg"), background),
            )
        check("code text", token_color(tokens, "--code-text"), token_color(tokens, "--code-bg"))
        for kind in ["added", "deleted"]:
            check(
                f"code diff {kind}",
                token_color(tokens, "--code-text"),
                token_color(tokens, f"--code-diff-{kind}-bg"),
            )
        for background in backgrounds:
            disabled_bg = composite(token_color(tokens, "--control-disabled-bg"), background)
            check("disabled control", token_color(tokens, "--control-disabled-text"), disabled_bg)
            for kind in ["cron", "ad-hoc"]:
                fill = composite(token_color(tokens, f"--run-calendar-{kind}-bg"), background)
                check(f"calendar {kind}", token_color(tokens, f"--run-calendar-{kind}-text"), fill)
        print(f"{name}: minimum {min(ratios):.2f}:1 across {len(ratios)} shared token pairs")
        # Analytics charts sit on glass-panel (--bg-card), composited over the
        # page background. Series strokes and pattern lines need 3:1 here;
        # decorative translucent area fills are not the identifying marks.
        chart_background = composite(token_color(tokens, "--bg-card"), root)
        chart_ratios: list[float] = []
        for index in range(1, 9):
            key = f"--chart-{index}"
            check(
                key,
                token_color(tokens, key),
                chart_background,
                ratios=chart_ratios,
                minimum=3.0,
            )
        print(f"{name}: chart minimum {min(chart_ratios):.2f}:1 across 8 series/panel pairs")
    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
