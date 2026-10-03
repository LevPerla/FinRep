import os
from pathlib import Path
import re

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import _level_palette


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_dark_financial_table_palettes_meet_normal_text_aa_contrast():
    for palette in ("green", "blue", "red"):
        backgrounds, foreground = _level_palette(palette, "dark")

        for background in backgrounds.values():
            assert _contrast_ratio(foreground, background) >= 4.5


def test_light_metric_cards_have_distinct_readable_surface():
    css = (Path(__file__).resolve().parents[1] / "assets" / "dashboard.css").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"\.finrep-theme-light \.finrep-cockpit-card\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})",
        css,
    )

    assert match is not None
    background = match.group(1)
    assert background.lower() != "#ffffff"
    assert _contrast_ratio("#172033", background) >= 4.5
