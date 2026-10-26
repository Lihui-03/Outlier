"""Player lookup. Uses nba_api's bundled static player list (fast, offline)
and falls back to CommonAllPlayers when needed."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import pandas as pd

from database.cache_db import get_cached, set_cached
from config import CACHE_TTL_PLAYERS

logger = logging.getLogger(__name__)

_NAMESPACE = "players"


@lru_cache(maxsize=4)
def all_players(league: str = "nba") -> pd.DataFrame:
    """Return a DataFrame of players. ``league`` is ``\"nba\"`` (default) or ``\"wnba\"``."""
    from nba_api.stats.static import players  # local import: slight startup cost

    cache_tag = "all_static" if league == "nba" else "all_static_wnba"
    cached = get_cached(_NAMESPACE, cache_tag, ttl_seconds=CACHE_TTL_PLAYERS)
    if cached is not None:
        return pd.DataFrame(cached)

    if league == "wnba":
        raw = players.get_wnba_players()
    else:
        raw = players.get_players()  # list of dicts
    df = pd.DataFrame(raw)
    # Ensure expected columns exist even if nba_api changes its shape.
    for col in ("id", "full_name", "first_name", "last_name", "is_active"):
        if col not in df.columns:
            df[col] = None

    set_cached(_NAMESPACE, cache_tag, df.to_dict(orient="records"))
    return df


def search_players(
    query: str,
    active_only: bool = True,
    limit: int = 25,
    *,
    league: str = "nba",
) -> pd.DataFrame:
    """Case-insensitive substring match on full_name."""
    if not query or not query.strip():
        return pd.DataFrame(columns=["id", "full_name", "is_active"])

    df = all_players(league)
    if df.empty:
        return df

    q = query.strip().lower()
    mask = df["full_name"].fillna("").str.lower().str.contains(q, regex=False)
    if active_only:
        mask &= df["is_active"].fillna(False).astype(bool)

    results = df.loc[mask, ["id", "full_name", "is_active"]]
    # Active first, then alphabetical.
    results = results.sort_values(
        by=["is_active", "full_name"],
        ascending=[False, True],
    )
    return results.head(limit).reset_index(drop=True)


def get_player_by_id(player_id: int, *, league: str = "nba") -> dict[str, Any] | None:
    df = all_players(league)
    row = df.loc[df["id"] == player_id]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def get_player_team_for_season(
    player_id: int,
    season: str,
    *,
    league_id: str = "00",
) -> dict[str, Any] | None:
    """Return team fields for a player. ``season`` reserved for future use;
    CommonPlayerInfo reflects the current roster snapshot from stats.nba.com.
    """
    from nba_api.stats.endpoints import commonplayerinfo
    from .nba_client import call_endpoint, NBAApiError

    _ = season
    cache_key = f"info:{league_id}:{player_id}"
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_PLAYERS)
    info_dict: dict[str, Any] | None = cached

    if info_dict is None:
        try:
            ep = call_endpoint(
                commonplayerinfo.CommonPlayerInfo,
                player_id=player_id,
                league_id_nullable=league_id,
            )
            frames = ep.get_data_frames()
            if not frames or frames[0].empty:
                return None
            info_dict = frames[0].iloc[0].to_dict()
            set_cached(_NAMESPACE, cache_key, info_dict)
        except NBAApiError as exc:
            logger.warning("CommonPlayerInfo failed for %s: %s", player_id, exc)
            return None

    return {
        "team_id": info_dict.get("TEAM_ID"),
        "team_abbreviation": info_dict.get("TEAM_ABBREVIATION"),
        "team_name": info_dict.get("TEAM_NAME"),
        "team_city": info_dict.get("TEAM_CITY"),
        "position": info_dict.get("POSITION"),
    }
