"""Per-game player tracking box score (rebound chances, passes, etc.).

``PlayerGameLog`` does not include rebound chances or tracking passes. The source is
``boxscoreplayertrackv3`` on stats.nba.com: each game's ``PlayerStats`` frame lists
``personId`` plus ``reboundChancesTotal``, ``passes``, ``assists``, etc.

``BoxScorePlayerTrackV3`` does not expose potential assists. These are fetched
separately via ``leaguedashptstats`` with ``PtMeasureType=Passing`` filtered to the
exact game date, which returns ``POTENTIAL_AST`` per player for that day.

Results are cached per game (historical finals do not change), matching
``linescore_service``'s long TTL pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from config import CACHE_TTL_LINESCORE
from database.cache_db import get_cached, set_cached
from utils.dates import coerce_game_datetime

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "playertrack_v3"
_NAMESPACE_PASSING = "passing_stats"


def _zfill_game_id(game_id: Any) -> str:
    s = "".join(ch for ch in str(game_id) if ch.isdigit())
    if not s:
        t = str(game_id).strip()
        return t.zfill(10) if t else ""
    tail = s[-10:] if len(s) >= 10 else s
    return tail.zfill(10)


def _cache_key_player_stats(game_id: str) -> str:
    return f"player_stats:{game_id}"


def fetch_player_track_player_stats(game_id: Any) -> pd.DataFrame | None:
    """Return the ``PlayerStats`` DataFrame for ``game_id``, or ``None`` if unavailable.

    Cached with the same long TTL as line scores — finalized games never change.
    """
    gid = _zfill_game_id(game_id)
    if len(gid) != 10:
        return None

    key = _cache_key_player_stats(gid)
    cached = get_cached(_NAMESPACE, key, ttl_seconds=CACHE_TTL_LINESCORE)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from nba_api.stats.endpoints import boxscoreplayertrackv3

        ep = call_endpoint(boxscoreplayertrackv3.BoxScorePlayerTrackV3, game_id=gid)
    except NBAApiError as exc:
        logger.info("BoxScorePlayerTrackV3(%s) skipped: %s", gid, exc)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("BoxScorePlayerTrackV3(%s) failed: %s", gid, exc)
        return None

    try:
        dfs = ep.get_data_frames()
    except Exception as exc:
        logger.warning("BoxScorePlayerTrackV3(%s) get_data_frames: %s", gid, exc)
        return None

    if not dfs:
        return None
    pstats = dfs[0]
    if not isinstance(pstats, pd.DataFrame) or pstats.empty:
        return None
    if "personId" not in pstats.columns:
        logger.debug("PlayerTrack missing personId for %s", gid)
        return None
    if not any(c in pstats.columns for c in ("reboundChancesTotal", "passes", "assists")):
        logger.debug("PlayerTrack has no usable stat columns for %s", gid)
        return None

    try:
        set_cached(_NAMESPACE, key, pstats.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("player track cache set failed for %s: %s", gid, exc)

    return pstats


def _numeric_cell(row: pd.Series, col: str) -> int | None:
    if col not in row.index:
        return None
    try:
        v = float(row[col])
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    return int(round(v))


def player_track_stats_for_player(
    game_id: Any,
    player_id: int,
) -> dict[str, int | None]:
    """Tracking stats for one player in one game (from cached PlayerTrack).

    Keys may include: ``rebound_chances_total``, ``passes``, ``assists``, ``potential_assists``.
    Omitted keys mean the column was missing or the row was not found.
    """
    out: dict[str, int | None] = {}
    if player_id <= 0:
        return out
    df = fetch_player_track_player_stats(game_id)
    if df is None or df.empty:
        return out
    pids = pd.to_numeric(df["personId"], errors="coerce")
    m = pids == float(int(player_id))
    if not m.any():
        return out
    row = df.loc[m].iloc[0]

    if "reboundChancesTotal" in df.columns:
        v = _numeric_cell(row, "reboundChancesTotal")
        if v is not None:
            out["rebound_chances_total"] = v
    if "passes" in df.columns:
        v = _numeric_cell(row, "passes")
        if v is not None:
            out["passes"] = v
    if "assists" in df.columns:
        v = _numeric_cell(row, "assists")
        if v is not None:
            out["assists"] = v
    for pa_col in ("potentialAssists", "potential_assists"):
        if pa_col in df.columns:
            v = _numeric_cell(row, pa_col)
            if v is not None:
                out["potential_assists"] = v
            break

    return out


def rebound_chances_total_for_player(game_id: Any, player_id: int) -> int | None:
    """Total rebound chances for ``player_id`` in ``game_id``, or ``None`` if missing."""
    s = player_track_stats_for_player(game_id, player_id)
    v = s.get("rebound_chances_total")
    return int(v) if v is not None else None


# ---------------------------------------------------------------------------
# Potential assists via leaguedashptstats (Passing) filtered to a game date
# ---------------------------------------------------------------------------

def normalize_game_date_iso(game_date: Any) -> str | None:
    """Normalize a ``GAME_DATE`` cell to ``YYYY-MM-DD``, or ``None`` if unparseable."""
    ts = coerce_game_datetime(game_date)
    if ts is None or pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _game_date_to_api_format(game_date: Any) -> str | None:
    """Convert a game date to ``MM/DD/YYYY`` for nba_api date params."""
    iso = normalize_game_date_iso(game_date)
    if iso is None:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        return None


def _season_from_game_date(game_date: Any) -> str:
    """Infer NBA season string (e.g., ``'2024-25'``) from a game date."""
    iso = normalize_game_date_iso(game_date)
    if iso is None:
        from nba_api.stats.library.parameters import Season
        return Season.default
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        year = dt.year
        if dt.month >= 10:
            return f"{year}-{str(year + 1)[2:]}"
        return f"{year - 1}-{str(year)[2:]}"
    except ValueError:
        from nba_api.stats.library.parameters import Season
        return Season.default


# NBA 10-digit game IDs: 00-T-YY-NNNNN where T = season type digit
#   1 = Pre Season, 2 = Regular Season, 3 = All-Star, 4 = Playoffs, 5 = Play-In
_GAME_ID_TYPE_DIGIT: dict[str, str] = {
    "1": "Pre Season",
    "2": "Regular Season",
    "3": "All Star",
    "4": "Playoffs",
    "5": "PlayIn",
}


def _season_type_from_game_id(game_id: str) -> str:
    """Return the nba_api season-type string from the type digit (index 2) of the game-id."""
    digits = "".join(ch for ch in str(game_id) if ch.isdigit())
    if len(digits) == 10:
        return _GAME_ID_TYPE_DIGIT.get(digits[2], "Regular Season")
    return "Regular Season"


def _fetch_passing_df(
    season: str,
    season_type: str,
    api_date: str,
    league_id: str,
) -> "pd.DataFrame | None":
    """Single ``leaguedashptstats`` Passing call; returns the DataFrame or ``None``."""
    try:
        from nba_api.stats.endpoints import leaguedashptstats
        from nba_api.stats.library.parameters import PerModeSimple, PlayerOrTeam, PtMeasureType

        ep = call_endpoint(
            leaguedashptstats.LeagueDashPtStats,
            last_n_games=0,
            month=0,
            opponent_team_id=0,
            per_mode_simple=PerModeSimple.per_game,
            player_or_team=PlayerOrTeam.player,
            pt_measure_type=PtMeasureType.passing,
            season=season,
            season_type_all_star=season_type,
            date_from_nullable=api_date,
            date_to_nullable=api_date,
            league_id_nullable=league_id,
        )
        dfs = ep.get_data_frames()
        if dfs and isinstance(dfs[0], pd.DataFrame) and not dfs[0].empty:
            return dfs[0]
        return None
    except NBAApiError as exc:
        logger.info("LeagueDashPtStats passing(%s %s) skipped: %s", season_type, api_date, exc)
        return None
    except Exception as exc:
        logger.warning("LeagueDashPtStats passing(%s %s) failed: %s", season_type, api_date, exc)
        return None


def _passing_day_cache_key(
    league_id: str,
    season: str,
    season_type: str,
    iso_date: str,
) -> str:
    return f"passing_day:{league_id}:{season}:{season_type}:{iso_date}"


def _season_types_to_try(game_id: str) -> list[str]:
    primary_type = _season_type_from_game_id(game_id) if game_id else "Regular Season"
    fallback_types = [t for t in ("Regular Season", "Playoffs", "PlayIn") if t != primary_type]
    return [primary_type] + fallback_types


def _get_passing_df_for_date(
    iso_date: str,
    game_id: str,
    league_id: str,
) -> pd.DataFrame | None:
    """Return the Passing ``leaguedashptstats`` frame for one calendar day (cached)."""
    season = _season_from_game_date(iso_date)
    api_date = _game_date_to_api_format(iso_date)
    if api_date is None:
        return None

    for stype in _season_types_to_try(game_id):
        day_key = _passing_day_cache_key(league_id, season, stype, iso_date)
        cached = get_cached(_NAMESPACE_PASSING, day_key, ttl_seconds=CACHE_TTL_LINESCORE)
        if cached is not None:
            try:
                df = pd.DataFrame(cached)
                if not df.empty:
                    return df
            except Exception:
                pass

        df = _fetch_passing_df(season, stype, api_date, league_id)
        if df is not None and not df.empty:
            try:
                set_cached(_NAMESPACE_PASSING, day_key, df.to_dict(orient="records"))
            except Exception as exc:  # pragma: no cover
                logger.warning("passing day cache set failed for %s: %s", iso_date, exc)
            return df

    return None


def potential_assists_is_cached(
    player_id: int,
    game_date: Any,
    *,
    game_id: str = "",
    league_id: str = "00",
) -> bool:
    """Return ``True`` if potential assists for this player/date are already cached."""
    if player_id <= 0:
        return False
    iso = normalize_game_date_iso(game_date)
    if iso is None:
        return False

    player_key = f"potential_ast:{player_id}:{iso}"
    if get_cached(_NAMESPACE_PASSING, player_key, ttl_seconds=CACHE_TTL_LINESCORE) is not None:
        return True

    season = _season_from_game_date(iso)
    for stype in _season_types_to_try(game_id):
        day_key = _passing_day_cache_key(league_id, season, stype, iso)
        if get_cached(_NAMESPACE_PASSING, day_key, ttl_seconds=CACHE_TTL_LINESCORE) is not None:
            return True
    return False


def potential_assists_for_player_on_date(
    player_id: int,
    game_date: Any,
    game_id: str = "",
    league_id: str = "00",
) -> int | None:
    """Return ``POTENTIAL_AST`` for ``player_id`` on the given game date.

    Uses ``leaguedashptstats`` with ``PtMeasureType=Passing`` filtered to the single
    calendar day (one API call per date, shared across players). Tries the season type
    inferred from ``game_id`` first (e.g. Playoffs), then fallbacks.
    """
    if player_id <= 0:
        return None

    iso = normalize_game_date_iso(game_date)
    if iso is None:
        return None

    player_key = f"potential_ast:{player_id}:{iso}"
    cached = get_cached(_NAMESPACE_PASSING, player_key, ttl_seconds=CACHE_TTL_LINESCORE)
    if cached is not None:
        try:
            v = int(cached)
            return None if v == -1 else v
        except (TypeError, ValueError):
            return None

    df = _get_passing_df_for_date(iso, game_id, league_id)
    if df is None or df.empty:
        set_cached(_NAMESPACE_PASSING, player_key, "-1")
        return None

    if "PLAYER_ID" not in df.columns or "POTENTIAL_AST" not in df.columns:
        logger.debug(
            "LeagueDashPtStats Passing missing expected columns for %s / %s",
            player_id,
            iso,
        )
        return None

    row_mask = pd.to_numeric(df["PLAYER_ID"], errors="coerce") == float(player_id)
    if not row_mask.any():
        set_cached(_NAMESPACE_PASSING, player_key, "-1")
        return None

    row = df.loc[row_mask].iloc[0]
    v = _numeric_cell(row, "POTENTIAL_AST")
    set_cached(_NAMESPACE_PASSING, player_key, str(v) if v is not None else "-1")
    return v
