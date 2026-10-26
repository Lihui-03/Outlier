"""Teammate roster + Game_ID intersection for WITH/WITHOUT filtering.

We use the simplest approach that works reliably on free endpoints:

    * `list_teammates` uses CommonTeamRoster to enumerate the current
      roster for (team_id, season).
    * `teammate_game_ids` re-uses `gamelog_service.fetch_player_game_log`
      to fetch each teammate's own game log and returns the set of
      GAME_IDs they appeared in. An appearance in both the player's
      game log and a teammate's game log means they both played.

This avoids needing to call BoxScore endpoints for every game. It misses
only the rare edge case where a teammate dressed but recorded zero
everything (still appears in a PlayerGameLog row so we're fine) and the
opposite case of games where neither was on the roster yet.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import CACHE_TTL_PLAYERS
from database.cache_db import get_cached, set_cached

from . import gamelog_service
from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "teammates"


def list_teammates(
    team_id: int | None,
    season: str,
    exclude_player_id: int | None = None,
    *,
    league_id: str = "00",
) -> pd.DataFrame:
    """Return a DataFrame of roster entries: PLAYER_ID, PLAYER, NUM, POSITION."""
    if not team_id:
        return pd.DataFrame(columns=["PLAYER_ID", "PLAYER", "NUM", "POSITION"])

    cache_key = f"roster:{league_id}:{team_id}:{season}"
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_PLAYERS)
    if cached is not None:
        df = pd.DataFrame(cached)
    else:
        try:
            from nba_api.stats.endpoints import commonteamroster

            ep = call_endpoint(
                commonteamroster.CommonTeamRoster,
                team_id=team_id,
                season=season,
                league_id_nullable=league_id,
            )
            frames = ep.get_data_frames()
        except NBAApiError as exc:
            logger.warning("CommonTeamRoster failed (%s, %s): %s", team_id, season, exc)
            return pd.DataFrame(columns=["PLAYER_ID", "PLAYER", "NUM", "POSITION"])

        df = frames[0] if frames else pd.DataFrame()
        if df.empty:
            return df

        keep = [c for c in ("PLAYER_ID", "PLAYER", "NUM", "POSITION",
                            "HEIGHT", "WEIGHT", "AGE") if c in df.columns]
        df = df[keep].copy()
        try:
            set_cached(_NAMESPACE, cache_key, df.to_dict(orient="records"))
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not cache roster: %s", exc)

    if exclude_player_id is not None and not df.empty and "PLAYER_ID" in df.columns:
        df = df[df["PLAYER_ID"].astype("Int64") != int(exclude_player_id)].copy()

    return df.reset_index(drop=True)


def teammate_game_ids(
    player_id: int,
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> set[str]:
    """Return the zero-padded set of GAME_IDs that the given player played
    in for the season. Empty set if the log fetch failed."""
    try:
        df = gamelog_service.fetch_player_game_log(
            player_id, season,
            season_type=season_type,
            league_id=league_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("teammate game-log fetch failed: %s", exc)
        return set()
    if df is None or df.empty:
        return set()

    for col in ("Game_ID", "GAME_ID"):
        if col in df.columns:
            return set(df[col].dropna().astype(str).str.zfill(10).tolist())
    return set()


def bulk_teammate_game_ids(teammate_ids: list[int], season: str,
                           season_type: str = "Regular Season") -> dict[int, set[str]]:
    """Fetch and return GAME_ID sets for a batch of teammates."""
    out: dict[int, set[str]] = {}
    for pid in teammate_ids:
        out[int(pid)] = teammate_game_ids(int(pid), season, season_type=season_type)
    return out


# ---------------------------------------------------------------------------
# Teammate summary (per-player aggregates used by the Filter-by-Teammate modal)
# ---------------------------------------------------------------------------


def fetch_teammate_summary(
    player_id: int,
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> dict[str, Any]:
    """Return a small summary of the teammate's own season performance.

    Reuses `gamelog_service.fetch_player_game_log` (already SQLite-cached)
    so repeat calls are instant. Applies `add_derived_stats` before
    aggregating so derived / threshold markets (PRA, DD, etc.) have the
    right columns.

    Returned dict always has the same shape so the UI can render safely
    even on fetch failures.
    """
    from models.stats import add_derived_stats

    result: dict[str, Any] = {
        "player_id":  int(player_id),
        "games":      0,
        "avg_min":    float("nan"),
        "stat_avgs":  {},
        "game_ids":   set(),
    }

    try:
        df = gamelog_service.fetch_player_game_log(
            player_id, season,
            season_type=season_type,
            league_id=league_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("teammate summary fetch failed for %s: %s", player_id, exc)
        return result
    if df is None or df.empty:
        return result

    if league_id == "10":
        from league_config.wnba import WNBA_DERIVED_STATS

        df = add_derived_stats(
            df,
            derived_stats=WNBA_DERIVED_STATS,
            threshold_stats={},
        )
    else:
        df = add_derived_stats(df)

    result["games"] = int(len(df))
    if "MIN" in df.columns:
        minutes = pd.to_numeric(df["MIN"], errors="coerce")
        # Only count games actually played for the minutes average so DNP
        # rows don't drag the number down.
        played = minutes[minutes.fillna(0) > 0]
        if not played.empty:
            result["avg_min"] = float(played.mean())

    # Cache a handful of popular stat averages plus whatever else we can.
    stat_avgs: dict[str, float] = {}
    for col in df.columns:
        series = pd.to_numeric(df[col], errors="coerce")
        if series.notna().any():
            try:
                stat_avgs[col] = float(series.mean())
            except (TypeError, ValueError):
                continue
    result["stat_avgs"] = stat_avgs

    for gid_col in ("Game_ID", "GAME_ID"):
        if gid_col in df.columns:
            result["game_ids"] = set(
                df[gid_col].dropna().astype(str).str.zfill(10).tolist()
            )
            break

    return result


def build_teammate_table(
    team_id: int | None,
    season: str,
    season_type: str,
    exclude_player_id: int | None,
    *,
    league_id: str = "00",
) -> pd.DataFrame:
    """Return a DataFrame ready to drive the Filter-by-Teammate modal.

    Columns:
        PLAYER_ID, PLAYER, POSITION, NUM,
        GAMES, AVG_MIN,
        STAT_AVGS  (dict of {col: avg} for all available stats),
        GAME_IDS   (set of zero-padded game ids used by the filter pipeline).

    `market_col` is intentionally NOT a parameter. By storing all stat
    averages in a single STAT_AVGS column, the Streamlit @st.cache_data
    wrapper in app.py does not have to rebuild the whole table every time
    the user switches markets (e.g. Points → Assists). The modal reads
    STAT_AVGS[market_col] at display time instead.

    Sorted by AVG_MIN descending so high-usage teammates scan first.
    All per-teammate calls go through the SQLite gamelog cache; no
    duplicate network work on reruns.
    """
    roster = list_teammates(
        team_id, season,
        exclude_player_id=exclude_player_id,
        league_id=league_id,
    )
    if roster is None or roster.empty or "PLAYER_ID" not in roster.columns:
        return pd.DataFrame(columns=[
            "PLAYER_ID", "PLAYER", "POSITION", "NUM",
            "GAMES", "AVG_MIN", "STAT_AVGS", "GAME_IDS",
        ])

    rows: list[dict[str, Any]] = []
    for _, r in roster.iterrows():
        try:
            pid = int(r["PLAYER_ID"])
        except (TypeError, ValueError):
            continue
        summary = fetch_teammate_summary(
            pid, season, season_type=season_type, league_id=league_id,
        )
        avg_min = summary["avg_min"]
        rows.append({
            "PLAYER_ID":  pid,
            "PLAYER":     str(r.get("PLAYER") or ""),
            "POSITION":   str(r.get("POSITION") or ""),
            "NUM":        str(r.get("NUM") or ""),
            "GAMES":      int(summary["games"]),
            "AVG_MIN":    float(avg_min) if avg_min == avg_min else float("nan"),
            "STAT_AVGS":  summary["stat_avgs"],   # dict: {col_name: float}
            "GAME_IDS":   summary["game_ids"],
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    return df.sort_values("AVG_MIN", ascending=False, na_position="last").reset_index(drop=True)
