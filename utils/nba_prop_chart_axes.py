"""Y-axis scaling for the NBA Research prop bar chart (``ui/graph.py``).

Player-facing counting stats are non-negative. The chart must keep **y_min = 0**
so bars sit on a real baseline — label annotations belong in **paper** margins,
not negative data coordinates.
"""

from __future__ import annotations

import math
from typing import Any

from config import STAT_COLUMNS

# All stat columns in this app’s Research view represent non-negative game totals
# or 0/1 threshold rows for the plotted series.
_NON_NEGATIVE_STAT_COLS: frozenset[str] = frozenset(STAT_COLUMNS.values())

# Stats that are naturally small integers per game (friendlier tick density).
_LOW_INTEGER_STAT_COLS: frozenset[str] = frozenset({
    "STL", "BLK", "TOV", "OREB", "DREB", "PF", "FG3M", "FG3A", "FTM", "FTA",
    "Q1_FG3M", "H1_FG3M",
})


def stat_column_is_non_negative(stat_col: str) -> bool:
    return stat_col in _NON_NEGATIVE_STAT_COLS


def _finite_values(ys: list[float]) -> list[float]:
    out: list[float] = []
    for y in ys:
        try:
            f = float(y)
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        out.append(f)
    return out


def _nice_upper_bound(raw_top: float, *, prop_line: float) -> float:
    """Round the computed top up to a clean axis limit (whole or half steps)."""
    if raw_top <= 0:
        return 1.0
    # Preserve half-point lines when the scale is tight.
    uses_half = abs(prop_line * 2 - round(prop_line * 2)) > 1e-6
    if raw_top <= 3.0:
        step = 0.5 if uses_half or raw_top < 2.0 else 1.0
        return math.ceil(raw_top / step) * step
    if raw_top <= 15:
        if uses_half and raw_top <= 10:
            return math.ceil(raw_top * 2) / 2
        return float(int(math.ceil(raw_top)))
    if raw_top <= 60:
        step = 5.0
        return math.ceil(raw_top / step) * step
    step = 10.0
    return math.ceil(raw_top / step) * step


def _headroom_for_anchor(anchor: float) -> float:
    """Additive padding above max(bar, line) — tighter for huge anchors."""
    if anchor <= 0:
        return 0.75
    if anchor <= 6:
        return max(0.6, anchor * 0.14)
    if anchor <= 25:
        return max(1.2, anchor * 0.07)
    if anchor <= 55:
        return max(2.5, anchor * 0.05)
    return max(4.0, anchor * 0.035)


def _choose_dtick(y_max: float, stat_col: str, prop_line: float) -> float:
    """Pick tick spacing that stays legible for low-count and high-count stats."""
    if y_max <= 0:
        return 1.0
    uses_half_ticks = (
        abs(prop_line * 2 - round(prop_line * 2)) > 1e-6 and y_max <= 12
    )
    if uses_half_ticks and y_max <= 8:
        return 0.5
    low_int = stat_col in _LOW_INTEGER_STAT_COLS
    if y_max <= 8 and low_int:
        return 1.0
    if y_max <= 12:
        return 1.0 if y_max <= 10 else 2.0
    if y_max <= 30:
        return 2.0 if (low_int and y_max <= 20) else 5.0
    if y_max <= 60:
        return 5.0
    return 10.0


def nba_prop_chart_yaxis_config(
    *,
    stat_col: str,
    ys: list[float],
    prop_line: float,
) -> dict[str, Any]:
    """Build Plotly ``yaxis`` kwargs: non-negative range, sensible max, ticks.

    The returned dict can be merged: ``fig.update_layout(yaxis={**base, **cfg})``.
    """
    # All Research stats use non-negative counting / 0–1 threshold columns.
    y_min = 0.0

    vals = _finite_values(ys)
    max_bar = max(vals) if vals else 0.0
    pl = float(prop_line)
    anchor = max(max_bar, pl, 0.0)

    top_raw = anchor + _headroom_for_anchor(anchor)
    y_max = _nice_upper_bound(top_raw, prop_line=pl)
    if y_max <= y_min:
        y_max = y_min + 1.0

    dtick = _choose_dtick(y_max, stat_col, pl)
    return {
        "range": [y_min, float(y_max)],
        "rangemode": "nonnegative",
        "dtick": dtick,
        "tick0": 0,
        "zeroline": True,
        "zerolinewidth": 1,
        "zerolinecolor": "rgba(148, 163, 184, 0.35)",
        "showgrid": True,
    }
