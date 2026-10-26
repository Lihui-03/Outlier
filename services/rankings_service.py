"""Team-level opponent ranking helpers.

Builds on `defense_service` by adding:
    * Advanced stats (PACE, DEF_RATING) via LeagueDashTeamStats Advanced.
    * Rank-per-team dictionaries that the filter modal and graphs use.
    * A best-effort wrapper for LeagueDashTeamShotLocations (shooting zone
      defence) — wrapped in try/except so a 403 from stats.nba.com
      degrades to "beta" rather than crashing.

All results are cached through the same SQLite namespace ("rankings").
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import CACHE_TTL_DEFENSE, OPP_RANK_SECTIONS
from database.cache_db import get_cached, set_cached

from . import defense_service
from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "rankings"


# Map chip key -> column name (either in defense_service's opponent table
# or in the advanced-stats table below). Keys that aren't in this map are
# treated as beta and not passed to the filter pipeline.
_CHIP_COLUMN_MAP: dict[str, tuple[str, str]] = {
    # chip key           -> (source, column)
    "opp_pts_rank":      ("opponent",  "OPP_PTS"),
    "opp_reb_rank":      ("opponent",  "OPP_REB"),
    "opp_ast_rank":      ("opponent",  "OPP_AST"),
    "opp_3pm_rank":      ("opponent",  "OPP_FG3M"),
    "opp_stl_rank":      ("opponent",  "OPP_STL"),
    "opp_blk_rank":      ("opponent",  "OPP_BLK"),
    "opp_tov_rank":      ("opponent",  "OPP_TOV"),
    "opp_pace_rank":     ("advanced",  "PACE"),
    "opp_def_rtg_rank":  ("advanced",  "DEF_RATING"),
}

# When ranking "lower is better for the defence" stats, we still want
# rank 1 = easiest for the offensive player. So for DEF_RATING and "opp
# XX allowed" stats, higher raw value = easier matchup = rank 1.
# PACE: higher pace = more possessions = offensive friendly = rank 1.
_RANK_ASCENDING: dict[str, bool] = {
    "OPP_PTS":    False,  # higher allowed = easier
    "OPP_REB":    False,
    "OPP_AST":    False,
    "OPP_FG3M":   False,
    "OPP_STL":    True,   # higher = they steal more = tougher, rank asc
    "OPP_BLK":    True,   # higher = more blocks = tougher, rank asc
    "OPP_TOV":    True,   # higher = more TOV forced = tougher, rank asc
    "PACE":       False,  # higher pace = more chances = easier
    "DEF_RATING": False,  # higher allowed-per-100 = easier
}


def fetch_team_advanced(
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> pd.DataFrame:
    """Team-level advanced stats (PACE, DEF_RATING, OFF_RATING, ...).

    Returns an empty DataFrame on failure. Cached for CACHE_TTL_DEFENSE.
    """
    cache_key = f"adv:{league_id}:{season}:{season_type}"
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_DEFENSE)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from nba_api.stats.endpoints import leaguedashteamstats

        ep = call_endpoint(
            leaguedashteamstats.LeagueDashTeamStats,
            season=season,
            season_type_all_star=season_type,
            measure_type_detailed_defense="Advanced",
            per_mode_detailed="PerGame",
            league_id_nullable=league_id,
        )
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        logger.warning("LeagueDashTeamStats(Advanced) failed: %s", exc)
        return pd.DataFrame()

    df = frames[0] if frames else pd.DataFrame()
    if df.empty:
        return df

    keep = [c for c in ("TEAM_ID", "TEAM_NAME", "GP",
                        "PACE", "DEF_RATING", "OFF_RATING", "NET_RATING") if c in df.columns]
    df = df[keep].copy()
    try:
        set_cached(_NAMESPACE, cache_key, df.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not cache team advanced: %s", exc)
    return df


def fetch_team_shot_locations(season: str,
                              season_type: str = "Regular Season") -> pd.DataFrame:
    """Per-team opponent shot-location breakdown.

    Wrapped in a try/except — when stats.nba.com returns 403 for the
    endpoint (which happens irregularly from free IPs) we return an
    empty frame and the UI falls back to "beta" chips.
    """
    cache_key = f"shotloc:{season}:{season_type}"
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_DEFENSE)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from nba_api.stats.endpoints import leaguedashteamshotlocations

        ep = call_endpoint(
            leaguedashteamshotlocations.LeagueDashTeamShotLocations,
            season=season,
            season_type_all_star=season_type,
            measure_type_simple="Opponent",
            per_mode_detailed="PerGame",
        )
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        logger.info("LeagueDashTeamShotLocations unavailable (%s) — shooting-zone "
                    "filters remain beta.", exc)
        return pd.DataFrame()
    except Exception as exc:  # pragma: no cover - any other issue (e.g. parsing)
        logger.info("LeagueDashTeamShotLocations parse error (%s) — zones stay beta.",
                    exc)
        return pd.DataFrame()

    df = frames[0] if frames else pd.DataFrame()
    if df.empty:
        return df

    try:
        set_cached(_NAMESPACE, cache_key, df.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not cache shot-location table: %s", exc)
    return df


# ---------------------------------------------------------------------------
# Rank computation
# ---------------------------------------------------------------------------


def rank_teams(df: pd.DataFrame, col: str, ascending: bool) -> dict[int, int]:
    """Return {team_id: 1-indexed rank} for a column.

    Uses dense ranking so ties share a rank. When `col` or `TEAM_ID` is
    missing the function returns an empty dict.
    """
    if df is None or df.empty or col not in df.columns or "TEAM_ID" not in df.columns:
        return {}
    numeric = pd.to_numeric(df[col], errors="coerce")
    ranks = numeric.rank(ascending=ascending, method="dense", na_option="bottom")
    return {int(tid): int(r) for tid, r in zip(df["TEAM_ID"], ranks) if pd.notna(r)}


def build_chip_rank_maps(
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> dict[str, dict[int, int]]:
    """Compute {chip_key: {team_id: rank}} for every chip we support."""
    opp_df = defense_service.fetch_team_defense(
        season, season_type=season_type, league_id=league_id,
    )
    adv_df = fetch_team_advanced(season, season_type, league_id=league_id)

    out: dict[str, dict[int, int]] = {}
    for chip_key, (source, col) in _CHIP_COLUMN_MAP.items():
        df = opp_df if source == "opponent" else adv_df
        ascending = _RANK_ASCENDING.get(col, False)
        out[chip_key] = rank_teams(df, col, ascending)
    return out


def describe_supported_chips() -> list[str]:
    """Return chip keys that have a real rank map (helpful for debugging)."""
    return list(_CHIP_COLUMN_MAP.keys())


def is_chip_real(chip_key: str) -> bool:
    """True iff the given chip is backed by real data (not beta). Used by
    the modal renderer."""
    for section_chips in OPP_RANK_SECTIONS.values():
        for chip in section_chips:
            if chip["key"] == chip_key:
                return chip.get("status") == "real"
    return False
