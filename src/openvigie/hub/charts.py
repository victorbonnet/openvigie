"""Server-rendered SVG charts: no JS, CSP-friendly (classes only, no inline styles).

Colors come from the CSS custom properties ``--series-1..8`` (validated categorical palette,
light and dark steps in app.css). A series keeps its slot whatever its rank: slots are assigned
from a stable order of entity ids, and anything past eight folds into "Other". Every mark has a
native ``<title>`` tooltip; every chart is paired with a table in the template.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from markupsafe import Markup, escape

MAX_SERIES = 8
OTHER = "Other"


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    slot: int  # 1..8, CSS class s<slot>; 0 = "Other" (muted)
    values: dict[str, Decimal]


def assign_slots(keys: Sequence[str]) -> dict[str, int]:
    """Stable slot per entity id (sorted ids), so filtering never repaints the survivors."""
    return {key: index + 1 for index, key in enumerate(sorted(keys)[:MAX_SERIES])}


def money(value: Decimal) -> str:
    if value == 0:
        return "$0"
    if value < Decimal("0.01"):
        return "<$0.01"
    return f"${value:,.2f}"


def compact(value: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{value:.0f}"


def _top_rounded(x: float, y: float, w: float, h: float, r: float) -> str:
    """Bar path with rounded top corners and a square base anchored to the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h:.1f} Z")  # fmt: skip


def _right_rounded(x: float, y: float, w: float, h: float, r: float) -> str:
    """Horizontal bar anchored at the left axis with a rounded right end."""
    r = min(r, h / 2, w)
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
            f"H{x:.1f} Z")  # fmt: skip


def _ticks(maximum: float, count: int = 4) -> list[float]:
    """Round gridline values (1, 2, 2.5 or 5 times a power of ten) covering ``maximum``."""
    if maximum <= 0:
        return [0.0]
    raw = maximum / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return [step * i for i in range(count + 1)]


def stacked_bars(
    days: Sequence[str],
    series: Sequence[Series],
    *,
    value_label: Callable[[Decimal], str] = money,
    height: int = 220,
) -> Markup:
    left, bottom, top = 56, 24, 8
    slot_w = max(8.0, min(40.0, 1080 / max(len(days), 1)))  # viewBox ~ page width: 1:1 text
    width = left + slot_w * len(days) + 8
    plot_h = height - bottom - top
    totals = [sum((s.values.get(d, Decimal(0)) for s in series), Decimal(0)) for d in days]
    maximum = float(max(totals, default=Decimal(0)))
    ticks = _ticks(maximum)
    scale_max = max(ticks[-1], maximum) or 1.0
    parts = [
        f'<svg class="chart" viewBox="0 0 {width:.0f} {height}" role="img" '
        f'aria-label="Daily cost by agent">'
    ]
    for tick in ticks:
        y = top + plot_h - plot_h * tick / scale_max
        parts.append(f'<line class="grid" x1="{left}" x2="{width - 4:.0f}" y1="{y:.1f}" '
                     f'y2="{y:.1f}"/>')  # fmt: skip
        parts.append(f'<text class="axis" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">'
                     f"{escape(value_label(Decimal(str(tick))))}</text>")  # fmt: skip
    label_every = max(1, len(days) // 8)
    bar_w = slot_w * 0.66
    for i, day in enumerate(days):
        x = left + slot_w * i + (slot_w - bar_w) / 2
        base = float(top + plot_h)
        stack = [(s, s.values.get(day, Decimal(0))) for s in series]
        stack = [(s, v) for s, v in stack if v > 0]
        for j, (s, value) in enumerate(stack):
            h = plot_h * float(value) / scale_max
            gap = 2.0 if j < len(stack) - 1 and h > 3 else 0.0
            y = base - h
            title = f"{s.label} · {day}: {value_label(value)}"
            css = f"s{s.slot}" if s.slot else "s-other"
            if j == len(stack) - 1:
                shape = f'<path class="{css}" d="{_top_rounded(x, y, bar_w, h, 4)}">'
                parts.append(shape + f"<title>{escape(title)}</title></path>")
            else:
                parts.append(f'<rect class="{css}" x="{x:.1f}" y="{y + gap:.1f}" '
                             f'width="{bar_w:.1f}" height="{max(h - gap, 0.5):.1f}">'
                             f"<title>{escape(title)}</title></rect>")  # fmt: skip
            base = y
        if i % label_every == 0:
            parts.append(f'<text class="axis" x="{x + bar_w / 2:.1f}" y="{height - 6}" '
                         f'text-anchor="middle">{escape(day[5:])}</text>')  # fmt: skip
    parts.append(f'<line class="baseline" x1="{left}" x2="{width - 4:.0f}" '
                 f'y1="{top + plot_h}" y2="{top + plot_h}"/>')  # fmt: skip
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - every dynamic value above is escaped


def hbars(
    items: Sequence[tuple[str, float]],
    *,
    value_label: Callable[[float], str] = compact,
    row_h: int = 26,
) -> Markup:
    """Single-series horizontal bars (one color: the title names the measure)."""
    label_w, value_w, width = 190.0, 70.0, 640.0
    plot_w = width - label_w - value_w
    height = row_h * max(len(items), 1) + 4
    maximum = max((v for _, v in items), default=0.0) or 1.0
    parts = [f'<svg class="chart hbars" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Tokens by model">']  # fmt: skip
    for i, (label, value) in enumerate(items):
        y = i * row_h + 4
        w = plot_w * value / maximum
        short = label if len(label) <= 28 else label[:27] + "…"
        parts.append(f'<text class="label" x="{label_w - 8}" y="{y + row_h / 2 + 2:.1f}" '
                     f'text-anchor="end">{escape(short)}</text>')  # fmt: skip
        if w > 0:
            path = _right_rounded(label_w, y + 5, max(w, 1), row_h - 10, 4)
            parts.append(f'<path class="s1" d="{path}"><title>{escape(label)}: '
                         f"{escape(value_label(value))}</title></path>")  # fmt: skip
        text_y = y + row_h / 2 + 2
        parts.append(f'<text class="value" x="{label_w + w + 6:.1f}" y="{text_y:.1f}">'
                     f"{escape(value_label(value))}</text>")  # fmt: skip
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - every dynamic value above is escaped
