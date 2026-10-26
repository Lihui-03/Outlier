"""Hit-rate calculations over a threshold."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .stats import ensure_stat_column


@dataclass
class HitRateResult:
    threshold: float
    games: int
    hits: int
    rate: float  # 0..1, NaN if no games

    def as_dict(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "games": self.games,
                "hits": self.hits, "rate": self.rate}


def hit_rate(df: pd.DataFrame, stat_col: str, threshold: float,
             last_n: int | None = None) -> HitRateResult:
    """Fraction of games where stat_col > threshold.

    `last_n=None` uses the whole log. Matches sportsbook convention that a
    push (== threshold) is not a hit on a half-line, but we also treat
    equality as a hit when threshold is an integer to avoid surprising
    the user; use strictly greater-than for .5 lines.
    """
    values = ensure_stat_column(df, stat_col)
    if last_n is not None:
        values = values.head(last_n)
    values = values.dropna()

    if values.empty:
        return HitRateResult(threshold=threshold, games=0, hits=0, rate=float("nan"))

    # If threshold is a half-number (e.g. 24.5) use strict >. If it's an
    # integer, treat == as a hit (common "push counts" user preference).
    is_half = abs(threshold - round(threshold)) > 1e-9
    if is_half:
        hits = int((values > threshold).sum())
    else:
        hits = int((values >= threshold).sum())

    games = int(values.shape[0])
    rate = hits / games if games else float("nan")
    return HitRateResult(threshold=threshold, games=games, hits=hits, rate=rate)


def hit_rates_over_windows(df: pd.DataFrame, stat_col: str,
                           threshold: float,
                           windows: tuple[int, ...] = (5, 10, 20)) -> pd.DataFrame:
    """Return a small summary DataFrame of hit rates over common windows."""
    rows = []
    for w in windows:
        r = hit_rate(df, stat_col, threshold, last_n=w)
        rows.append({"window": f"L{w}", **r.as_dict()})
    # Include season-long.
    season = hit_rate(df, stat_col, threshold, last_n=None)
    rows.append({"window": "Season", **season.as_dict()})
    return pd.DataFrame(rows)
