"""Player game-log fetching.

We fetch via PlayerGameLog (regular season) and normalize the result
into a consistent schema with parsed dates, home/away flag, opponent
abbreviation, and minutes as float. Derived stats (PRA/PA/RA) are added
by models.stats so this layer stays focused on I/O.

Resilience additions (timeout-storm fix)
-----------------------------------------
* Stale cache fallback: if a live fetch fails and SQLite has ANY previously
  saved data for the same key (even expired), we return that stale data
  with a warning. Empty + crashed > data. See `_fetch_one_season_type`.

* Pre Season is skipped by default when `season_type="ALL"`. Pre Season
  data is almost never relevant for prop research and adds 1–3 extra API
  calls on every cold start. Pass `include_preseason=True` to opt in.

* The nba_client layer now enforces a per-endpoint failure cooldown and a
  global circuit breaker, so repeated failures stop hammering stats.nba.com
  within the same Streamlit session. Nothing extra is needed here.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import BASE_NUMERIC_COLUMNS, CACHE_TTL_GAMELOG
from database.cache_db import get_cached, get_cached_stale, set_cached
from utils.dates import compute_rest_days, parse_game_date

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "gamelog"


def _cache_key(league_id: str, player_id: int, season: str, season_type: str) -> str:
    return f"{league_id}:{player_id}:{season}:{season_type}"


def _opp_abbr_to_team_id(abbr: str, league_id: str = "00") -> int | None:
    """Map a team tricode to a stats.nba.com team id (NBA or WNBA).

    Uses a small in-module cache because nba_api's lookup walks a list
    every call.
    """
    if not abbr:
        return None
    cache = _opp_abbr_to_team_id._cache  # type: ignore[attr-defined]
    key = (league_id, abbr)
    if key in cache:
        return cache[key]
    try:
        from nba_api.stats.static import teams
        if league_id == "10":
            match = teams.find_wnba_team_by_abbreviation(abbr)
        else:
            match = teams.find_team_by_abbreviation(abbr)
        team_id = int(match["id"]) if match else None
    except Exception:
        team_id = None
    cache[key] = team_id
    return team_id


_opp_abbr_to_team_id._cache = {}  # type: ignore[attr-defined]


def team_id_from_abbr(abbr: str | None, league_id: str = "00") -> int | None:
    """Map tricode (e.g. ``OKC``) to stats.nba.com ``TEAM_ID`` (NBA or WNBA)."""
    if not abbr:
        return None
    return _opp_abbr_to_team_id(str(abbr).strip().upper(), league_id)


def _derive_matchup_columns_inplace(df: pd.DataFrame, league_id: str = "00") -> None:
    """Populate ``OPPONENT``, ``IS_HOME``, ``OPP_TEAM_ID`` from ``MATCHUP``.

    Must match live normalization behaviour so SQLite JSON replay cannot leave
    blank opponents when ``MATCHUP`` is present (the usual cache shape).
    """
    if "MATCHUP" not in df.columns:
        return
    parsed = df["MATCHUP"].apply(parse_player_matchup_string)
    df["OPPONENT"] = parsed.apply(lambda x: x[0])
    df["IS_HOME"] = parsed.apply(lambda x: x[1])
    df["OPP_TEAM_ID"] = df["OPPONENT"].apply(lambda a: _opp_abbr_to_team_id(a, league_id))


def parse_player_matchup_string(matchup: Any) -> tuple[str, bool]:
    """Return (opponent_abbr, is_home) from an nba_api ``MATCHUP`` cell.

    Examples: ``DET vs. ORL`` → (``ORL``, True); ``DET @ ORL`` → (``ORL``, False).

    Exported for chart / UI repair when ``OPPONENT`` or ``IS_HOME`` is missing
    after cache round-trips.
    """
    if not isinstance(matchup, str):
        return ("", False)
    s = matchup.strip()
    if not s:
        return ("", False)
    if " vs. " in s:
        parts = s.split(" vs. ")
        return (parts[1].strip(), True)
    if " vs " in s:
        parts = s.split(" vs ", 1)
        return (parts[1].strip(), True)
    if " @ " in s:
        parts = s.split(" @ ")
        return (parts[1].strip(), False)
    return ("", False)


def _minutes_to_float(value: Any) -> float:
    """MIN can be '34:12' (mm:ss), '34.2', or already numeric."""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return float("nan")
    if ":" in s:
        try:
            m, sec = s.split(":", 1)
            return int(m) + int(sec) / 60.0
        except ValueError:
            return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _normalize(df: pd.DataFrame, season_type: str | None = None,
               league_id: str = "00") -> pd.DataFrame:
    """Apply consistent column shape: dates, opponent, home flag, numeric MIN.

    Only touches columns that exist. Downstream code tolerates missing
    columns, so we keep the frame narrow rather than padding with NA (which
    serializes badly into the JSON cache).

    If `season_type` is supplied, tag every row with that label so combined
    frames (Pre + Regular + Playoffs) stay distinguishable in the UI.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    for col in BASE_NUMERIC_COLUMNS:
        if col not in df.columns:
            continue
        if col == "MIN":
            df[col] = df[col].apply(_minutes_to_float)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "GAME_DATE" in df.columns:
        df["GAME_DATE"] = parse_game_date(df["GAME_DATE"])

    _derive_matchup_columns_inplace(df, league_id)

    if "WL" in df.columns:
        df["IS_WIN"] = df["WL"].fillna("").str.upper().eq("W")

    if "GAME_DATE" in df.columns:
        df["REST_DAYS"] = compute_rest_days(df["GAME_DATE"])

    if season_type is not None:
        df["SEASON_TYPE"] = season_type

    # Sort newest-first (nba_api default) for consistency in UI tables and
    # rolling calculations that operate on the most-recent N games.
    if "GAME_DATE" in df.columns:
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)

    return df


def _restore_from_cache_record(records: list, season_type: str,
                               league_id: str = "00") -> pd.DataFrame:
    """Reconstruct a normalized DataFrame from a raw SQLite records list."""
    df = pd.DataFrame(records)
    if df.empty:
        return df
    if "GAME_DATE" in df.columns:
        df["GAME_DATE"] = parse_game_date(df["GAME_DATE"])

    _derive_matchup_columns_inplace(df, league_id)

    if "REST_DAYS" in df.columns:
        df["REST_DAYS"] = pd.to_numeric(df["REST_DAYS"], errors="coerce")
    if "OPP_TEAM_ID" in df.columns:
        df["OPP_TEAM_ID"] = pd.to_numeric(df["OPP_TEAM_ID"],
                                           errors="coerce").astype("Int64")
    elif "OPPONENT" in df.columns:
        # Older payloads without OPP_TEAM_ID and without MATCHUP replay above.
        df["OPP_TEAM_ID"] = df["OPPONENT"].apply(
            lambda a: _opp_abbr_to_team_id(a, league_id))
    if "SEASON_TYPE" not in df.columns:
        df["SEASON_TYPE"] = season_type
    return df


# The three real nba_api season_type values that make up "All".
_ALL_SEASON_TYPES: tuple[str, ...] = ("Regular Season", "Playoffs")
_ALL_SEASON_TYPES_WITH_PRE: tuple[str, ...] = ("Pre Season", "Regular Season", "Playoffs")


def _fetch_one_season_type(
    player_id: int,
    season: str,
    season_type: str,
    *,
    league_id: str,
    use_cache: bool,
) -> pd.DataFrame:
    """Fetch and normalize a single season_type; cached per-combination.

    Resilience: if the live fetch fails and there is ANY previously cached
    data for this key (even beyond the normal TTL), that stale data is
    returned with a WARNING log. A "stale data" indicator is set in the
    SEASON_TYPE_STALE column so the UI can display a soft warning.
    """
    key = _cache_key(league_id, player_id, season, season_type)

    if use_cache:
        cached = get_cached(_NAMESPACE, key, ttl_seconds=CACHE_TTL_GAMELOG)
        if cached is not None:
            return _restore_from_cache_record(cached, season_type, league_id)

    from nba_api.stats.endpoints import playergamelog

    try:
        ep = call_endpoint(
            playergamelog.PlayerGameLog,
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
            league_id_nullable=league_id,
        )
        raw_frames = ep.get_data_frames()
    except NBAApiError as exc:
        # Live fetch failed. Try stale cache as fallback before giving up.
        stale = get_cached_stale(_NAMESPACE, key)
        if stale is not None:
            logger.warning(
                "playergamelog LIVE FETCH FAILED (%s / %s / %s / lg=%s): %s — "
                "returning STALE cached data",
                player_id, season, season_type, league_id, exc,
            )
            df = _restore_from_cache_record(stale, season_type, league_id)
            if not df.empty:
                df["_stale"] = True
            return df
        # No stale data available either.
        logger.error(
            "playergamelog failed (%s / %s / %s / lg=%s) with no cache fallback: %s",
            player_id, season, season_type, league_id, exc,
        )
        return pd.DataFrame()

    raw = raw_frames[0] if raw_frames else pd.DataFrame()
    df = _normalize(raw, season_type=season_type, league_id=league_id)

    try:
        safe = df.astype(object).where(df.notna(), None)
        set_cached(_NAMESPACE, key, safe.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not cache game log: %s", exc)

    return df


def fetch_player_game_log(
    player_id: int,
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
    use_cache: bool = True,
    include_preseason: bool = False,
) -> pd.DataFrame:
    """Return a normalized game-log DataFrame for the player & season.

    Pass ``season_type="ALL"`` to fetch Regular Season and Playoffs for
    the given season and return them combined in a single frame (sorted
    newest-first). Each row is tagged with a ``SEASON_TYPE`` column.

    Pre Season is excluded by default when ``season_type="ALL"`` because
    it adds 1-3 extra API calls on every cold start with almost no value
    for prop research. Pass ``include_preseason=True`` to include it.

    Returns empty DataFrame on upstream failure or no games played.

    ``league_id`` is ``\"00\"`` for NBA and ``\"10\"`` for WNBA (stats.nba.com).
    """
    if season_type == "ALL":
        types = _ALL_SEASON_TYPES_WITH_PRE if include_preseason else _ALL_SEASON_TYPES
        frames = [
            _fetch_one_season_type(
                player_id, season, st,
                league_id=league_id, use_cache=use_cache,
            )
            for st in types
        ]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        # Re-sort and recompute rest days across the combined timeline.
        if "GAME_DATE" in combined.columns:
            combined = combined.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
            combined["REST_DAYS"] = compute_rest_days(combined["GAME_DATE"])
        return combined

    return _fetch_one_season_type(
        player_id, season, season_type,
        league_id=league_id, use_cache=use_cache,
    )


def invalidate_player_cache(
    player_id: int,
    season: str | None = None,
    *,
    league_id: str = "00",
) -> int:
    """Clear cached game logs for a player. Returns rows deleted."""
    from database.cache_db import clear_namespace

    prefix = (
        f"{league_id}:{player_id}:"
        if season is None else
        f"{league_id}:{player_id}:{season}:"
    )
    return clear_namespace(_NAMESPACE, prefix)
