"""Transparent weighted projection engine.

Goal: explainable, not a black box. The user should always be able to
answer "why did it pick that number?" by reading four lines of math.

The formula:

    base  = w5 * mean(last 5)
          + w10 * mean(last 10)
          + wSeason * mean(season)

    final = base * matchup_factor * home_away_factor * minutes_factor

Weights are normalized to sum to 1 (we don't force the user to keep them
exactly at 1 in the UI). Any factor is 1.0 (no effect) when its toggle
is off or when we don't have enough data to compute it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from config import DEFAULT_WEIGHTS, DERIVED_STATS, THRESHOLD_STATS

from .stats import ensure_stat_column


# ---------------------------------------------------------------------------
# Small helpers kept tiny and pure so each factor can use them.
# ---------------------------------------------------------------------------


def _nan() -> float:
    return float("nan")


def _is_nan(v: Any) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


def _mean_last_n(values: pd.Series, n: int) -> float:
    """Mean of the most recent `n` values (input is newest-first)."""
    if values is None or values.empty:
        return _nan()
    head = values.head(n).dropna()
    return float(head.mean()) if not head.empty else _nan()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Factor record: every factor returns the same shape so the UI can render
# them uniformly.
# ---------------------------------------------------------------------------


@dataclass
class Factor:
    name: str
    factor: float            # the multiplier applied to base
    note: str                # one-line plain-English explanation
    details: dict[str, Any] = field(default_factory=dict)  # optional structured data


def _neutral_factor(name: str, note: str) -> Factor:
    return Factor(name=name, factor=1.0, note=note)


# ---------------------------------------------------------------------------
# Individual factor calculators. Each one is small and self-contained.
# ---------------------------------------------------------------------------


def home_away_factor(df: pd.DataFrame, stat_col: str,
                     is_home_next: bool | None) -> Factor:
    if is_home_next is None or df is None or df.empty \
            or "IS_HOME" not in df.columns or stat_col not in df.columns:
        return _neutral_factor("home_away", "Home/away not applied (no location info).")

    values = pd.to_numeric(df[stat_col], errors="coerce")
    home = values[df["IS_HOME"] == True].dropna()
    away = values[df["IS_HOME"] == False].dropna()
    overall = values.dropna()

    if home.empty or away.empty or overall.empty:
        return _neutral_factor("home_away", "Not enough home/away games yet.")

    home_mean = float(home.mean())
    away_mean = float(away.mean())
    overall_mean = float(overall.mean())
    if overall_mean == 0:
        return _neutral_factor("home_away", "Overall mean is 0; skipping home/away.")

    chosen_mean = home_mean if is_home_next else away_mean
    raw = chosen_mean / overall_mean
    factor = _clamp(raw, 0.80, 1.20)
    side = "home" if is_home_next else "away"
    note = (f"Home {home_mean:.1f} / Away {away_mean:.1f} / Overall {overall_mean:.1f}; "
            f"next game is {side} → factor {factor:.3f}.")
    return Factor("home_away", factor, note,
                  details={"home_mean": home_mean, "away_mean": away_mean,
                           "overall_mean": overall_mean, "side": side})


def minutes_factor(df: pd.DataFrame, projected_minutes: float | None) -> Factor:
    if df is None or df.empty or "MIN" not in df.columns:
        return _neutral_factor("minutes", "Minutes adjustment not applied (no MIN column).")

    minutes = pd.to_numeric(df["MIN"], errors="coerce").dropna()
    if minutes.empty:
        return _neutral_factor("minutes", "Minutes adjustment not applied (no minutes played).")

    season_mean = float(minutes.mean())
    if season_mean == 0:
        return _neutral_factor("minutes", "Season mean minutes is 0.")

    if projected_minutes is None:
        projected_minutes = _mean_last_n(minutes, 5)
    if _is_nan(projected_minutes):
        return _neutral_factor("minutes", "Could not determine projected minutes.")

    raw = float(projected_minutes) / season_mean
    factor = _clamp(raw, 0.70, 1.30)
    note = (f"Projected {float(projected_minutes):.1f} min vs season avg {season_mean:.1f} min "
            f"→ factor {factor:.3f}.")
    return Factor("minutes", factor, note,
                  details={"projected_min": float(projected_minutes),
                           "season_mean_min": season_mean})


def matchup_factor_from_info(matchup_info: dict[str, Any] | None) -> Factor:
    """Build the matchup factor from a pre-fetched info dict.

    `matchup_info` comes from services.defense_service.matchup_factor()
    and looks like {'opp_allowed': X, 'league_allowed': Y, 'factor': X/Y}.
    We accept None and degrade to neutral.
    """
    if not matchup_info:
        return _neutral_factor("matchup", "Matchup adjustment unavailable (no opponent defense data).")

    try:
        raw = float(matchup_info["factor"])
    except (KeyError, TypeError, ValueError):
        return _neutral_factor("matchup", "Matchup factor malformed.")

    factor = _clamp(raw, 0.85, 1.15)
    opp = matchup_info.get("opp_allowed")
    lg = matchup_info.get("league_allowed")
    if opp is not None and lg is not None:
        note = (f"Opponent allows {float(opp):.1f} vs league avg {float(lg):.1f} "
                f"→ factor {factor:.3f}.")
    else:
        note = f"Matchup factor = {factor:.3f}."
    return Factor("matchup", factor, note,
                  details={"opp_allowed": opp, "league_allowed": lg})


# ---------------------------------------------------------------------------
# Core entrypoint
# ---------------------------------------------------------------------------


@dataclass
class ProjectionResult:
    stat_col: str
    last5: float
    last10: float
    season_mean: float
    weights: dict[str, float]
    base: float
    factors: list[Factor]
    final: float
    explanation: list[str]


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Scale weights to sum to 1. Fall back to defaults if all zero."""
    w5 = max(0.0, float(weights.get("w5", DEFAULT_WEIGHTS["w5"])))
    w10 = max(0.0, float(weights.get("w10", DEFAULT_WEIGHTS["w10"])))
    ws = max(0.0, float(weights.get("wSeason", DEFAULT_WEIGHTS["wSeason"])))
    total = w5 + w10 + ws
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {"w5": w5 / total, "w10": w10 / total, "wSeason": ws / total}


def _can_use_stat(df: pd.DataFrame, stat_col: str) -> bool:
    if df is None or df.empty:
        return False
    # Already computed and present as a column (covers everything after
    # add_derived_stats() has run, including DD/TD/Q1 etc.).
    if stat_col in df.columns:
        return True
    # Sum-based derived: all components present?
    if stat_col in DERIVED_STATS:
        return all(c in df.columns for c in DERIVED_STATS[stat_col])
    # Threshold-based derived: at least one component present?
    if stat_col in THRESHOLD_STATS:
        return any(c in df.columns for c in THRESHOLD_STATS[stat_col]["columns"])
    return False


def _compute_base(values: pd.Series, weights: dict[str, float]) -> tuple[float, float, float, float]:
    """Return (base, last5, last10, season_mean).

    If one of the means is NaN (e.g. fewer than 10 games played), we drop
    that term and renormalize the remaining weights so we still return a
    meaningful number instead of NaN.
    """
    last5 = _mean_last_n(values, 5)
    last10 = _mean_last_n(values, 10)
    season_mean = float(values.mean()) if not values.dropna().empty else _nan()

    parts = [(last5, weights["w5"]), (last10, weights["w10"]), (season_mean, weights["wSeason"])]
    available = [(v, w) for v, w in parts if not _is_nan(v)]
    if not available:
        return _nan(), last5, last10, season_mean

    total_weight = sum(w for _, w in available) or 1.0
    base = sum(v * (w / total_weight) for v, w in available)
    return base, last5, last10, season_mean


def project(
    df: pd.DataFrame,
    stat_col: str,
    *,
    weights: dict[str, float] | None = None,
    is_home_next: bool | None = None,
    matchup_info: dict[str, Any] | None = None,
    projected_minutes: float | None = None,
    apply_matchup: bool = False,
    apply_home_away: bool = False,
    apply_minutes: bool = False,
) -> ProjectionResult:
    """Produce a projection with every component exposed for inspection.

    Safe to call on empty/missing data: returns NaN `final` and a short
    explanation instead of raising.
    """
    weights = _normalize_weights(weights or DEFAULT_WEIGHTS)

    if not _can_use_stat(df, stat_col):
        return ProjectionResult(
            stat_col=stat_col,
            last5=_nan(), last10=_nan(), season_mean=_nan(),
            weights=weights,
            base=_nan(),
            factors=[],
            final=_nan(),
            explanation=["No game-log data available for this player/season."],
        )

    values = ensure_stat_column(df, stat_col)
    base, last5, last10, season_mean = _compute_base(values, weights)

    factors: list[Factor] = []
    if apply_matchup:
        factors.append(matchup_factor_from_info(matchup_info))
    if apply_home_away:
        factors.append(home_away_factor(df, stat_col, is_home_next))
    if apply_minutes:
        factors.append(minutes_factor(df, projected_minutes))

    final = base
    if not _is_nan(final):
        for f in factors:
            final *= f.factor

    explanation = _build_explanation(stat_col, last5, last10, season_mean,
                                     weights, base, factors, final)

    return ProjectionResult(
        stat_col=stat_col,
        last5=last5, last10=last10, season_mean=season_mean,
        weights=weights,
        base=base,
        factors=factors,
        final=final,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Explanation builder & edge helper
# ---------------------------------------------------------------------------


def _fmt(v: float) -> str:
    return "n/a" if _is_nan(v) else f"{float(v):.2f}"


def _build_explanation(stat_col: str, last5: float, last10: float,
                       season_mean: float, weights: dict[str, float],
                       base: float, factors: list[Factor],
                       final: float) -> list[str]:
    lines = [
        f"Stat: {stat_col}.",
        (f"Base = {weights['w5']:.2f} × L5 ({_fmt(last5)}) "
         f"+ {weights['w10']:.2f} × L10 ({_fmt(last10)}) "
         f"+ {weights['wSeason']:.2f} × Season ({_fmt(season_mean)})  "
         f"=  {_fmt(base)}."),
    ]
    if not factors:
        lines.append("No adjustments; final = base.")
    else:
        for f in factors:
            lines.append(f"{f.name}: ×{f.factor:.3f} — {f.note}")
        lines.append(f"Final = base × " + " × ".join(
            f"{f.factor:.3f}" for f in factors
        ) + f"  =  {_fmt(final)}.")
    return lines


def compute_edge(projection_final: float, manual_line: float | None) -> float:
    """Edge = projection - line. NaN-safe."""
    if manual_line is None or _is_nan(projection_final):
        return _nan()
    try:
        return float(projection_final) - float(manual_line)
    except (TypeError, ValueError):
        return _nan()
