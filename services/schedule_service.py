"""Schedule / upcoming opponent lookups.

Finding the "next opponent" with free endpoints is surprisingly fiddly
because LeagueGameFinder only returns games that have already occurred.
To get a team's upcoming game we use ScoreboardV2 for today's date,
and otherwise fall back to 'last known opponent' from the game log.
This is clearly commented because it's a v1 approximation.

For **WNBA** (LeagueID 10) we use **ScoreboardV3** + ``line_score`` because
the daily slate is not returned on the legacy v2 path the same way.

Resilience additions (timeout-storm fix)
-----------------------------------------
* Negative caching: if ScoreboardV2 confirms there is no game for this
  team today, that "no-game" result is cached for CACHE_TTL_SCHEDULE
  seconds. This prevents re-querying the endpoint on every Streamlit
  widget interaction (which previously triggered the full retry stack
  on a cold render).

* Stale fallback: if the live ScoreboardV2 fetch fails but SQLite has a
  previous schedule result, the stale result is returned with a warning
  and displayed in the UI as approximate.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

from config import CACHE_TTL_SCHEDULE
from database.cache_db import get_cached, get_cached_stale, set_cached

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "schedule"

# Sentinel stored in the cache when we confirm "no game today".
# Distinguished from None (cache miss) so we can skip the live fetch.
_NO_GAME_SENTINEL = "__no_game__"


def get_next_opponent(
    team_id: int | None,
    season: str,
    *,
    league_id: str = "00",
) -> dict[str, Any] | None:
    """Best-effort: return info about the team's next scheduled game.

    Returns None when we can't determine it from free data. The dashboard
    should fall back to "most recent opponent from the game log" and make
    this clear in the UI.

    Caching strategy:
    - If today's game is found or confirmed absent, cache that result for
      CACHE_TTL_SCHEDULE (default 1 hour) so widgets reruns don't re-query.
    - If ScoreboardV2 fails entirely, return any stale cached result as a
      best-effort fallback so the projection section still has an opponent.
    """
    _ = season
    if not team_id:
        return None

    today_str = date.today().strftime("%Y-%m-%d")
    cache_key = f"next:{league_id}:{team_id}:{today_str}"

    # Check fresh cache first.
    cached = get_cached(_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_SCHEDULE)
    if cached is not None:
        if cached == _NO_GAME_SENTINEL:
            return None   # confirmed "no game today" within TTL
        return cached

    if league_id == "10":
        # WNBA: ScoreboardV3 + line_score (team rows per game).
        try:
            from nba_api.stats.endpoints import scoreboardv3

            ep = call_endpoint(
                scoreboardv3.ScoreboardV3,
                game_date=today_str,
                league_id=league_id,
            )
            ls = ep.line_score.get_data_frame()
        except NBAApiError as exc:
            logger.warning("ScoreboardV3 (WNBA) failed: %s", exc)
            ls = pd.DataFrame()
        except Exception as exc:
            logger.warning("ScoreboardV3 (WNBA) unexpected failure: %s", exc)
            ls = pd.DataFrame()

        if ls is not None and not ls.empty and "teamId" in ls.columns:
            hits = ls[ls["teamId"] == int(team_id)]
            if not hits.empty:
                gid = hits.iloc[0].get("gameId")
                sides = ls[ls["gameId"] == gid]
                others = sides[sides["teamId"] != int(team_id)]
                if len(others) == 1:
                    opp_id = int(others.iloc[0]["teamId"])
                    result: dict[str, Any] = {
                        "opponent_team_id": opp_id,
                        "is_home": None,
                        "game_date": today_str,
                        "source": "scoreboard_today",
                    }
                    set_cached(_NAMESPACE, cache_key, result)
                    return result

        set_cached(_NAMESPACE, cache_key, _NO_GAME_SENTINEL)
        return None

    # Try ScoreboardV2 for today (NBA default).
    try:
        from nba_api.stats.endpoints import scoreboardv2

        ep = call_endpoint(scoreboardv2.ScoreboardV2, game_date=today_str)
        frames = ep.get_data_frames()
        games = frames[0] if frames else pd.DataFrame()

        if not games.empty:
            home_col = "HOME_TEAM_ID" if "HOME_TEAM_ID" in games.columns else None
            vis_col  = "VISITOR_TEAM_ID" if "VISITOR_TEAM_ID" in games.columns else None
            if home_col and vis_col:
                row = games[
                    (games[home_col] == team_id) | (games[vis_col] == team_id)
                ]
                if not row.empty:
                    g = row.iloc[0]
                    is_home = int(g.get(home_col, 0)) == team_id
                    opp_id = int(
                        g.get(vis_col) if is_home else g.get(home_col)
                    )
                    result = {
                        "opponent_team_id": opp_id,
                        "is_home": is_home,
                        "game_date": today_str,
                        "source": "scoreboard_today",
                    }
                    set_cached(_NAMESPACE, cache_key, result)
                    return result

        # Scoreboard returned successfully but team has no game today.
        # Cache the "no game" sentinel to prevent repeated ScoreboardV2
        # calls on every widget rerun within the same hour.
        set_cached(_NAMESPACE, cache_key, _NO_GAME_SENTINEL)
        return None

    except NBAApiError as exc:
        logger.warning("ScoreboardV2 failed: %s", exc)
    except Exception as exc:
        logger.warning("ScoreboardV2 unexpected failure: %s", exc)

    # Live fetch failed — look for any stale cached result for today or
    # the last season-day we have stored so the projection still has an opponent.
    stale = get_cached_stale(_NAMESPACE, cache_key)
    if stale is not None and stale != _NO_GAME_SENTINEL:
        logger.warning(
            "schedule: returning STALE opponent info for team %s (live fetch failed).",
            team_id,
        )
        return stale

    # Nothing cached at all — fall back to most-recent opponent in game log (done in app.py).
    return None


def resolve_opponent_team(
    team_id: int | None,
    *,
    league_id: str = "00",
) -> dict[str, Any] | None:
    """Resolve a team id to its tricode/name using nba_api's static team list."""
    if not team_id:
        return None
    try:
        from nba_api.stats.static import teams
        if league_id == "10":
            info = teams.find_wnba_team_name_by_id(team_id)
        else:
            info = teams.find_team_name_by_id(team_id)
        if info:
            return {
                "team_id": team_id,
                "team_abbreviation": info.get("abbreviation"),
                "team_name": info.get("full_name"),
            }
    except Exception as exc:
        logger.warning("team lookup failed: %s", exc)
    return None


def resolve_opponent_by_abbr(
    abbr: str,
    *,
    league_id: str = "00",
) -> dict[str, Any] | None:
    if not abbr:
        return None
    try:
        from nba_api.stats.static import teams
        if league_id == "10":
            matches = teams.find_wnba_team_by_abbreviation(abbr)
        else:
            matches = teams.find_team_by_abbreviation(abbr)
        if matches:
            return {
                "team_id": matches.get("id"),
                "team_abbreviation": matches.get("abbreviation"),
                "team_name": matches.get("full_name"),
            }
    except Exception as exc:
        logger.warning("team lookup by abbr failed: %s", exc)
    return None
