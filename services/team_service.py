"""Team game-log fetching.

The Game Markets mode needs team-level per-game scoring (points scored +
points allowed + opponent + home/away + rest). We pull from `TeamGameLog`
and normalise into a consistent schema that mirrors `gamelog_service`:
same date/opponent/is_home columns, plus `PTS_AGAINST` derived from
`PLUS_MINUS`.

Resilience:
* All network goes through ``nba_client.call_endpoint`` (inherits retry,
  circuit breaker, rate limiter).
* SQLite-cached under the ``team_gamelog`` namespace.
* Falls back to stale cache on live-fetch failure (``_stale=True``).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import CACHE_TTL_TEAM_GAMELOG
from database.cache_db import get_cached, get_cached_stale, set_cached
from utils.dates import compute_rest_days, parse_game_date

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "team_gamelog"

# In-module cache of the 30-team static list so repeated lookups stay free.
_TEAMS_CACHE: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Team listing (static, offline)
# ---------------------------------------------------------------------------


def list_teams() -> list[dict[str, Any]]:
    """Return the 30 NBA teams from nba_api's bundled static list.

    No network. Each dict has at least ``id``, ``abbreviation``, ``full_name``,
    ``nickname``, ``city``. Sorted alphabetically by full name for UI display.
    """
    global _TEAMS_CACHE
    if _TEAMS_CACHE is not None:
        return _TEAMS_CACHE
    try:
        from nba_api.stats.static import teams as static_teams
        rows = static_teams.get_teams() or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("static team list failed: %s", exc)
        rows = []
    _TEAMS_CACHE = sorted(rows, key=lambda r: str(r.get("full_name", "")))
    return _TEAMS_CACHE


def resolve_team(team_id_or_abbr: int | str | None) -> dict[str, Any] | None:
    """Look up a team by id or tricode. Returns None when not found."""
    if team_id_or_abbr is None:
        return None
    rows = list_teams()
    if isinstance(team_id_or_abbr, int):
        for r in rows:
            if int(r.get("id", 0)) == team_id_or_abbr:
                return r
        return None
    key = str(team_id_or_abbr).upper().strip()
    for r in rows:
        if str(r.get("abbreviation", "")).upper() == key:
            return r
    return None


# ---------------------------------------------------------------------------
# Team game log
# ---------------------------------------------------------------------------


def _parse_matchup(matchup: Any) -> tuple[str, bool]:
    """Return (opponent_abbr, is_home) from 'ATL vs. NYK' or 'ATL @ NYK'."""
    if not isinstance(matchup, str):
        return ("", False)
    if " vs. " in matchup:
        return (matchup.split(" vs. ")[1].strip(), True)
    if " @ " in matchup:
        return (matchup.split(" @ ")[1].strip(), False)
    return ("", False)


def _normalize(df: pd.DataFrame, season_type: str | None = None) -> pd.DataFrame:
    """Shape a raw TeamGameLog frame for downstream consumers."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # Canonical column aliases: TeamGameLog uses 'Team_ID', 'Game_ID', etc.
    rename = {}
    for src, dst in (("Team_ID", "TEAM_ID"), ("Game_ID", "GAME_ID")):
        if src in df.columns and dst not in df.columns:
            rename[src] = dst
    if rename:
        df = df.rename(columns=rename)

    for col in ("PTS", "PLUS_MINUS", "FGM", "FGA", "FG3M", "FG3A",
                "FTM", "FTA", "REB", "AST", "STL", "BLK", "TOV", "PF"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "GAME_DATE" in df.columns:
        df["GAME_DATE"] = parse_game_date(df["GAME_DATE"])

    if "MATCHUP" in df.columns:
        parsed = df["MATCHUP"].apply(_parse_matchup)
        df["OPPONENT"] = parsed.apply(lambda x: x[0])
        df["IS_HOME"] = parsed.apply(lambda x: x[1])

    if "WL" in df.columns:
        df["IS_WIN"] = df["WL"].fillna("").astype(str).str.upper().eq("W")

    # PTS_AGAINST = team_pts - team_margin. Requires both columns to be numeric.
    if "PTS" in df.columns and "PLUS_MINUS" in df.columns:
        df["PTS_AGAINST"] = df["PTS"] - df["PLUS_MINUS"]
        df["GAME_TOTAL"] = df["PTS"] + df["PTS_AGAINST"]
        df["MARGIN"] = df["PLUS_MINUS"]

    if "GAME_DATE" in df.columns:
        df["REST_DAYS"] = compute_rest_days(df["GAME_DATE"])

    if season_type is not None:
        df["SEASON_TYPE"] = season_type

    if "GAME_DATE" in df.columns:
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)

    return df


def _restore(records: list, season_type: str) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if df.empty:
        return df
    if "GAME_DATE" in df.columns:
        df["GAME_DATE"] = parse_game_date(df["GAME_DATE"])
    for col in ("PTS", "PLUS_MINUS", "PTS_AGAINST", "GAME_TOTAL",
                "MARGIN", "REST_DAYS"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "SEASON_TYPE" not in df.columns:
        df["SEASON_TYPE"] = season_type
    return df


def _cache_key(team_id: int, season: str, season_type: str) -> str:
    return f"{team_id}:{season}:{season_type}"


def fetch_team_game_log(team_id: int, season: str,
                        season_type: str = "Regular Season",
                        use_cache: bool = True) -> pd.DataFrame:
    """Return a normalized team game log.

    Returns an empty DataFrame on failure with no stale cache available.
    When stale cache is served, a ``_stale`` column is set to True on every
    row so the UI can show a warning banner.
    """
    key = _cache_key(team_id, season, season_type)

    if use_cache:
        cached = get_cached(_NAMESPACE, key, ttl_seconds=CACHE_TTL_TEAM_GAMELOG)
        if cached is not None:
            return _restore(cached, season_type)

    try:
        from nba_api.stats.endpoints import teamgamelog

        ep = call_endpoint(
            teamgamelog.TeamGameLog,
            team_id=team_id,
            season=season,
            season_type_all_star=season_type,
        )
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        stale = get_cached_stale(_NAMESPACE, key)
        if stale is not None:
            logger.warning(
                "TeamGameLog LIVE FAILED (%s / %s / %s): %s — returning STALE cache",
                team_id, season, season_type, exc,
            )
            df = _restore(stale, season_type)
            if not df.empty:
                df["_stale"] = True
            return df
        logger.error(
            "TeamGameLog failed (%s / %s / %s), no cache fallback: %s",
            team_id, season, season_type, exc,
        )
        return pd.DataFrame()

    raw = frames[0] if frames else pd.DataFrame()
    df = _normalize(raw, season_type=season_type)

    try:
        safe = df.astype(object).where(df.notna(), None)
        set_cached(_NAMESPACE, key, safe.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not cache team game log: %s", exc)

    return df


def invalidate_team_cache(team_id: int, season: str | None = None) -> int:
    from database.cache_db import clear_namespace
    prefix = f"{team_id}:" if season is None else f"{team_id}:{season}:"
    return clear_namespace(_NAMESPACE, prefix)
