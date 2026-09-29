"""Shared palette, typography and color helpers for every window."""

from typing import Iterable, Optional

from PyQt6.QtGui import QColor, QFont

# Surfaces
PANEL = QColor(20, 23, 29, 242)
DIALOG = QColor(22, 25, 31, 246)
SHEEN = QColor(255, 255, 255, 16)
BORDER = QColor(255, 255, 255, 30)
CARD = "rgba(255,255,255,0.045)"
CARD_BORDER = "rgba(255,255,255,0.07)"

# Text
TEXT = "#eef0f4"
TEXT_DIM = "#a7acb6"
TEXT_MUTED = "#80868f"
TEXT_FAINT = "#5b616b"

# Accents. Chinese market convention: red rises, green falls.
GOLD = "#e8c47c"
GOLD_DEEP = "#a8792c"
UP = "#ff7b72"
DOWN = "#45cf88"
FLAT = "#b4bac5"
WARN = "#eab676"
LIVE = "#78d0a5"
ACCENT = "#5b9dff"

UI_FAMILIES = (
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Microsoft YaHei",
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "sans-serif",
)
MONO_FAMILIES = (
    "SF Mono", "Menlo", "JetBrains Mono", "Consolas", "DejaVu Sans Mono", "monospace",
)


def _font(families, pixel_size, weight):
    # type: (Iterable[str], float, QFont.Weight) -> QFont
    # Pixel sizes render identically on macOS (72 dpi) and Windows/Linux (96 dpi),
    # which point sizes do not.
    font = QFont()
    font.setFamilies(list(families))
    font.setPixelSize(max(1, round(pixel_size)))
    font.setWeight(weight)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def ui_font(pixel_size, weight=QFont.Weight.Normal):
    # type: (float, QFont.Weight) -> QFont
    return _font(UI_FAMILIES, pixel_size, weight)


def mono_font(pixel_size, weight=QFont.Weight.Normal):
    # type: (float, QFont.Weight) -> QFont
    return _font(MONO_FAMILIES, pixel_size, weight)


def blend(start, end, ratio):
    # type: (QColor, QColor, float) -> QColor
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(start.red() + (end.red() - start.red()) * ratio),
        round(start.green() + (end.green() - start.green()) * ratio),
        round(start.blue() + (end.blue() - start.blue()) * ratio),
        round(start.alpha() + (end.alpha() - start.alpha()) * ratio),
    )


def with_alpha(color, alpha):
    # type: (object, int) -> QColor
    result = QColor(color)
    result.setAlpha(max(0, min(255, int(alpha))))
    return result


def css_rgba(color):
    # type: (QColor) -> str
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


def signed_color(value):
    # type: (Optional[float]) -> str
    if value is None:
        return TEXT_MUTED
    return UP if value > 0 else DOWN if value < 0 else FLAT


def movement_theme(interval_pct, threshold):
    # type: (Optional[float], float) -> dict
    """Colors for the floating panel; intensity grows with the interval move."""
    neutral = {
        "price": QColor(TEXT),
        "interval": QColor(192, 196, 204),
        "sparkline": QColor(GOLD),
        "background": QColor(255, 255, 255, 0),
        "border": QColor(BORDER),
    }
    if interval_pct is None:
        return neutral

    threshold = max(float(threshold), 0.01)
    magnitude = abs(interval_pct)
    if magnitude < threshold:
        softness = magnitude / threshold
        neutral["sparkline"] = QColor(255, 255, 255, 128 + round(softness * 24))
        neutral["background"] = QColor(255, 255, 255, round(softness * 10))
        neutral["border"] = QColor(255, 255, 255, 30 + round(softness * 8))
        return neutral

    ratio = min((magnitude - threshold) / (threshold * 2), 1.0)
    if interval_pct > 0:
        accent = blend(QColor(255, 150, 95), QColor(255, 72, 72), ratio)
    else:
        accent = blend(QColor(102, 225, 155), QColor(54, 210, 110), ratio)
    return {
        "price": accent,
        "interval": accent,
        "sparkline": with_alpha(accent, 160 + round(ratio * 60)),
        "background": with_alpha(accent, 28 + round(ratio * 56)),
        "border": with_alpha(accent, 48 + round(ratio * 56)),
    }
