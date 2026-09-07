"""Check shared dashboard text/control tokens against WCAG contrast formulas.

Covers the six shipped palettes and alpha-composited shared surfaces. This is
not a substitute for testing rendered focus, overlays, charts, and typography.
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
        ratios: list[float] = []

        def check(
            label: str,
            ink: list[float],
            background: list[float],
            ratios: list[float] = ratios,
            name: str = name,
        ) -> None:
            ratio = contrast(composite(ink, background), background)
            ratios.append(ratio)
            if ratio < 4.5:
                failures.append(f"{name}: {label} {ratio:.2f}:1")

        for key in ["--text-primary", "--text-secondary", "--text-muted", "--accent"]:
            for background in backgrounds:
                check(key, color(tokens[key]), background)
        for tone in ["success", "warning", "danger", "info"]:
            for background in backgrounds:
                fill = composite(color(tokens[f"--{tone}-bg"]), background)
                check(tone, color(tokens[f"--{tone}-text"]), fill)
        for fill in ["--accent", "--accent-hover"]:
            check("primary button", color(tokens["--accent-contrast"]), color(tokens[fill]))
        check("danger button", color(tokens["--danger-contrast"]), color(tokens["--danger"]))
        print(f"{name}: minimum {min(ratios):.2f}:1 across {len(ratios)} shared token pairs")
    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
