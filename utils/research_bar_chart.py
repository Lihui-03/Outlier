"""Sparse x-axis ticks for Research bar charts (many-game / Max filters)."""

from __future__ import annotations

import numpy as np
import pandas as pd

# Number of plotted games at or below which we stack opponent + month + day annotations.
AXIS_DENSE_MAX_GAMES = 30


def sparse_axis_tick_positions(
    timestamps: list[pd.Timestamp],
    *,
    max_ticks: int = 12,
) -> list[int]:
    """Sparse bar indices: start/end, month starts, even coverage.

    Thin by removing the right anchor of shortest interior gaps until
    ``<= max_ticks`` (never removes bar index ``0`` or ``n - 1``).
    """
    n = len(timestamps)
    if n == 0:
        return []
    if n == 1:
        return [0]

    cand: set[int] = {0, n - 1}
    prev_ts = timestamps[0]
    for i in range(1, n):
        tsi = timestamps[i]
        prev_key = (prev_ts.year, prev_ts.month)
        cur_key = (tsi.year, tsi.month)
        if cur_key != prev_key:
            cand.add(i)
        prev_ts = tsi

    need = max(6, min(int(max_ticks), n))
    lin = np.linspace(0, max(n - 1, 1), num=need, dtype=float)
    for x in lin:
        cand.add(int(round(float(x))))

    ordered = sorted(cand)

    while len(ordered) > max_ticks:
        gaps: list[tuple[int, int, int]] = []
        for k in range(len(ordered) - 1):
            gaps.append((ordered[k + 1] - ordered[k], ordered[k], ordered[k + 1]))
        gaps.sort(key=lambda z: z[0])

        shrunk = False
        for gap_w, ix_a, ix_b in gaps:
            if gap_w <= 0:
                continue
            if ix_b != n - 1:
                ordered.remove(ix_b)
                shrunk = True
                break
            if ix_a != 0:
                ordered.remove(ix_a)
                shrunk = True
                break
        if not shrunk:
            break

    return ordered


def format_sparse_chart_day(ts: pd.Timestamp) -> str:
    """Compact day below month (screenshot-style, no zero-padding)."""
    ts = pd.Timestamp(ts)
    if pd.isna(ts):
        raise ValueError("timestamp must not be NaT")
    return str(int(ts.day))
