"""Team-level defensive allowed-stats.

This is the v1 / MVP approximation for matchup context. Real positional
defense (e.g. "opponent allows X points to PGs") requires either a paid
feed or scraping, both of which are out of scope for this free build.

We use LeagueDashTeamStats with MeasureType='Opponent' to get per-game
stats that each team has allowed on average. We then express a given
team's allowed stat relative to the league average, producing a simple
matchup multiplier in the projection engine.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import CACHE_TTL_DEFENSE
from database.cache_db import get_cached, set_cached

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "defense"

# Maps our stat keys (base columns, not derived) to the OPP_* column names
# that LeagueDashTeamStats returns for MeasureType='Opponent'.
_OPP_COLUMN_MAP = {
    "PTS": "OPP_PTS",
    "REB": "OPP_REB",
    "AST": "OPP_AST",
    "FG3M": "OPP_FG3M",
    "STL": "OPP_STL",
    "BLK": "OPP_BLK",
    "TOV": "OPP_TOV",
}


def fetch_team_defense(
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> pd.DataFrame:
    """Return a DataFrame of per-team opponent averages for the season.

    Empty DataFrame on failure.
    """
    cache_key = f"opp:{league_id}:{season}:{season_type}"
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_DEFENSE)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from nba_api.stats.endpoints import leaguedashteamstats

        ep = call_endpoint(
            leaguedashteamstats.LeagueDashTeamStats,
            season=season,
            season_type_all_star=season_type,
            measure_type_detailed_defense="Opponent",
            per_mode_detailed="PerGame",
            league_id_nullable=league_id,
        )
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        logger.warning("LeagueDashTeamStats(Opponent) failed: %s", exc)
        return pd.DataFrame()

    df = frames[0] if frames else pd.DataFrame()
    if df.empty:
        return df

    # Keep only columns we care about to shrink cache size.
    keep = ["TEAM_ID", "TEAM_NAME", "GP"] + [c for c in _OPP_COLUMN_MAP.values() if c in df.columns]
    df = df[[c for c in keep if c in df.columns]].copy()

    try:
        set_cached(_NAMESPACE, cache_key, df.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not cache team defense: %s", exc)

    return df


def opponent_allowed(
    team_id: int,
    stat_base_col: str,
    season: str,
    *,
    league_id: str = "00",
) -> float | None:
    """Return the team's per-game allowed value for a base stat column.

    `stat_base_col` is one of 'PTS', 'REB', 'AST', 'FG3M', 'STL', 'BLK', 'TOV'.
    """
    opp_col = _OPP_COLUMN_MAP.get(stat_base_col)
    if not opp_col:
        return None

    df = fetch_team_defense(season, league_id=league_id)
    if df.empty or opp_col not in df.columns:
        return None

    row = df[df["TEAM_ID"] == team_id]
    if row.empty:
        return None
    val = row.iloc[0][opp_col]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def league_allowed(
    stat_base_col: str,
    season: str,
    *,
    league_id: str = "00",
) -> float | None:
    """Return league average allowed (used as matchup denominator)."""
    opp_col = _OPP_COLUMN_MAP.get(stat_base_col)
    if not opp_col:
        return None
    df = fetch_team_defense(season, league_id=league_id)
    if df.empty or opp_col not in df.columns:
        return None
    mean = pd.to_numeric(df[opp_col], errors="coerce").mean()
    if pd.isna(mean):
        return None
    return float(mean)


def matchup_factor(
    team_id: int | None,
    stat_base_col: str,
    season: str,
    *,
    league_id: str = "00",
) -> dict[str, Any] | None:
    """Return a dict with matchup adjustment info, or None if not computable.

    Structure:
        {
            'opp_allowed': 28.5,
            'league_allowed': 27.2,
            'factor': 1.048,    # opp / league
        }
    """
    if not team_id:
        return None
    opp = opponent_allowed(team_id, stat_base_col, season, league_id=league_id)
    lg = league_allowed(stat_base_col, season, league_id=league_id)
    if opp is None or lg is None or lg == 0:
        return None
    return {
        "opp_allowed": opp,
        "league_allowed": lg,
        "factor": opp / lg,
    }
