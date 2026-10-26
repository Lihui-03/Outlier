"""Rolling / summary stats for a player's game log.

Game logs passed in are expected to be newest-first (services.gamelog_service
sorts this way). All functions return NaN-safe floats.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd

from .stats import ensure_stat_column


def _mean_of_recent(values: pd.Series, n: int) -> float:
    """Mean of the most recent `n` games (newest-first input)."""
    if values is None or values.empty:
        return float("nan")
    head = values.head(n).dropna()
    if head.empty:
        return float("nan")
    return float(head.mean())


def _median(values: pd.Series) -> float:
    if values is None or values.empty:
        return float("nan")
    cleaned = values.dropna()
    if cleaned.empty:
        return float("nan")
    return float(cleaned.median())


def _std(values: pd.Series) -> float:
    if values is None or values.empty:
        return float("nan")
    cleaned = values.dropna()
    if len(cleaned) < 2:
        return float("nan")
    return float(cleaned.std(ddof=1))


@dataclass
class RollingSummary:
    games_played: int
    last5: float
    last10: float
    last20: float
    season_mean: float
    season_median: float
    season_std: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_rolling_summary(df: pd.DataFrame, stat_col: str) -> RollingSummary:
    values = ensure_stat_column(df, stat_col)
    return RollingSummary(
        games_played=int(values.dropna().shape[0]),
        last5=_mean_of_recent(values, 5),
        last10=_mean_of_recent(values, 10),
        last20=_mean_of_recent(values, 20),
        season_mean=float(values.mean()) if not values.empty and not values.isna().all() else float("nan"),
        season_median=_median(values),
        season_std=_std(values),
    )


def rolling_series(df: pd.DataFrame, stat_col: str, window: int = 5) -> pd.DataFrame:
    """Return a per-game trend DataFrame with columns:
        GAME_DATE, stat, rolling_mean.

    Intended for plotting. We re-sort to oldest-first for a natural x-axis.
    """
    if df is None or df.empty or stat_col not in df.columns:
        return pd.DataFrame(columns=["GAME_DATE", stat_col, "rolling_mean"])

    out = df[["GAME_DATE", stat_col]].copy()
    out[stat_col] = pd.to_numeric(out[stat_col], errors="coerce")
    if "GAME_DATE" in out.columns:
        out = out.sort_values("GAME_DATE", ascending=True)
    out["rolling_mean"] = out[stat_col].rolling(window=window, min_periods=1).mean()
    return out.reset_index(drop=True)
