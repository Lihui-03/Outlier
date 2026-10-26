"""Filter-aware metric helpers.

All functions take already-filtered and optionally already-excluded
DataFrames. They never apply filters themselves — that's the job of
`services/filter_service.py`. This keeps a clean separation between
"what sample do we want?" and "what numbers do we compute from that
sample?".

The `mode` argument throughout controls Average vs Median so the
entire app follows the toggle in the Stats tab with one switch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").dropna()


def _agg(values: pd.Series, mode: str) -> float:
    if values.empty:
        return float("nan")
    return float(values.median()) if mode == "median" else float(values.mean())


# ---------------------------------------------------------------------------
# Hit rate
# ---------------------------------------------------------------------------


@dataclass
class HitRateRow:
    hits: int
    games: int
    rate: float   # NaN when games == 0

    @property
    def ratio_text(self) -> str:
        if self.games == 0:
            return "0/0"
        return f"{self.hits}/{self.games}"


def compute_hit_rate(df: pd.DataFrame, col: str, line: float) -> HitRateRow:
    """Return hit rate over `line` using the same half-line / integer
    convention as models.hit_rate. For threshold-based 0/1 stats (DD/TD),
    a line of 0.5 naturally gives the "has-DD?" rate.
    """
    values = _numeric(df, col)
    if values.empty:
        return HitRateRow(hits=0, games=0, rate=float("nan"))
    is_half = abs(line - round(line)) > 1e-9
    hits = int((values > line).sum()) if is_half else int((values >= line).sum())
    games = int(values.shape[0])
    rate = hits / games if games else float("nan")
    return HitRateRow(hits=hits, games=games, rate=rate)


# ---------------------------------------------------------------------------
# Averages / diffs
# ---------------------------------------------------------------------------


def compute_graph_avg(df: pd.DataFrame, col: str, mode: str = "avg") -> float:
    return _agg(_numeric(df, col), mode)


def compute_season_avg(season_df: pd.DataFrame, col: str, mode: str = "avg") -> float:
    """Full-sample average regardless of the current filter.

    `season_df` should be the **unfiltered** game log for the current
    season + season_type.
    """
    return _agg(_numeric(season_df, col), mode)


def compute_filtered_vs_excluded_diff(filtered: pd.DataFrame, excluded: pd.DataFrame,
                                      col: str, mode: str = "avg") -> dict[str, float | None]:
    """Return a small dict describing the filtered vs excluded diff."""
    f_val = _agg(_numeric(filtered, col), mode)
    e_val = _agg(_numeric(excluded, col), mode) if (excluded is not None and not excluded.empty) else None
    if e_val is None or (isinstance(e_val, float) and math.isnan(e_val)):
        e_val = None
        diff = None
    else:
        diff = None if math.isnan(f_val) else (f_val - e_val)
    return {
        "filtered": None if (isinstance(f_val, float) and math.isnan(f_val)) else f_val,
        "excluded": e_val,
        "diff": diff,
    }


def compute_stat_card_diffs(filtered: pd.DataFrame, excluded: pd.DataFrame,
                            cols: list[str], mode: str = "avg") -> list[dict[str, Any]]:
    """Return a list of card payloads for the Research stat-card grid."""
    rows = []
    for c in cols:
        d = compute_filtered_vs_excluded_diff(filtered, excluded, c, mode=mode)
        rows.append({
            "col":      c,
            "filtered": d["filtered"],
            "excluded": d["excluded"],
            "diff":     d["diff"],
        })
    return rows


def compute_usage_proxy(df: pd.DataFrame) -> pd.Series:
    """USG%-style single-player approximation per game row.

    True usage rate requires team possessions; we approximate with
    (FGA + 0.44*FTA + TOV) normalised by MIN*5 / 48. This is documented
    as an estimate in the UI.
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    for col in ("FGA", "FTA", "TOV", "MIN"):
        if col not in df.columns:
            return pd.Series(dtype="float64")
    fga = pd.to_numeric(df["FGA"], errors="coerce")
    fta = pd.to_numeric(df["FTA"], errors="coerce")
    tov = pd.to_numeric(df["TOV"], errors="coerce")
    mins = pd.to_numeric(df["MIN"], errors="coerce").clip(lower=1e-6)
    # Per-48 approximation: (FGA + 0.44*FTA + TOV) * (48/MIN)
    raw = (fga + 0.44 * fta + tov)
    per48 = raw * (48.0 / mins)
    # Express as a pseudo-% of team plays, assuming 100 possessions per 48:
    # (per48 / 100) gives a 0–1 ratio; multiply by 100 for %.
    return per48  # raw per-48 count; UI labels it "Usage proxy"
