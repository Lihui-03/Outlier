"""High-level PandaScore endpoint helpers.

Every function returns plain JSON (list/dict) the way PandaScore sends it.
Parsing into friendlier shapes happens in the service layer above this one
(``cs2_match_service`` / ``cs2_team_service`` / ``cs2_player_service``).

The game slug is parameterised via ``CS2_API_GAME_SLUG`` (defaults to
``"csgo"``, which is how PandaScore currently exposes CS2 endpoints).
Update that one env var / config value if PandaScore renames.
"""

from __future__ import annotations

import logging
from typing import Any

from config import (
    CACHE_TTL_CS2_MATCH_DETAIL,
    CACHE_TTL_CS2_MATCH_LIST,
    CACHE_TTL_CS2_PLAYER,
    CACHE_TTL_CS2_PLAYER_MATCHES,
    CACHE_TTL_CS2_TEAM,
    CACHE_TTL_CS2_TEAM_MATCHES,
    CS2_API_GAME_SLUG,
)
from services.cs2.pandascore_client import PandaScoreError, request

logger = logging.getLogger(__name__)


def _game_path(tail: str) -> str:
    """Prefix ``tail`` with the CS2 game slug, e.g. ``"/csgo"``."""
    return f"/{CS2_API_GAME_SLUG}{tail}"


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------


def list_upcoming_matches(*, per_page: int = 25,
                           tournament_id: int | None = None) -> list[dict]:
    """Upcoming scheduled matches, ordered by begin time (earliest first)."""
    params: dict[str, Any] = {
        "per_page": per_page,
        "sort":     "begin_at",
    }
    if tournament_id:
        params["filter[tournament_id]"] = tournament_id
    data = request(_game_path("/matches/upcoming"),
                   params=params,
                   ttl_seconds=CACHE_TTL_CS2_MATCH_LIST)
    return data if isinstance(data, list) else []


def list_past_matches(*, per_page: int = 50,
                       tournament_id: int | None = None,
                       team_id: int | None = None) -> list[dict]:
    """Recent completed matches, most recent first."""
    params: dict[str, Any] = {
        "per_page": per_page,
        "sort":     "-end_at",
    }
    if tournament_id:
        params["filter[tournament_id]"] = tournament_id
    if team_id:
        # PandaScore uses a nested filter for participant team IDs.
        params["filter[opponent_id]"] = team_id
    data = request(_game_path("/matches/past"),
                   params=params,
                   ttl_seconds=CACHE_TTL_CS2_MATCH_LIST)
    return data if isinstance(data, list) else []


def get_match(match_id: int) -> dict:
    """Full match detail including nested games (maps), opponents,
    tournament, and per-player stats when the match is finished."""
    data = request(_game_path(f"/matches/{int(match_id)}"),
                   ttl_seconds=CACHE_TTL_CS2_MATCH_DETAIL)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def search_teams(query: str, *, per_page: int = 15) -> list[dict]:
    """Free-text team search. PandaScore's ``search[name]`` is prefix-ish
    but good enough for an input box."""
    q = (query or "").strip()
    if not q:
        return []
    params: dict[str, Any] = {
        "search[name]": q,
        "per_page":     per_page,
    }
    data = request(_game_path("/teams"),
                   params=params, ttl_seconds=CACHE_TTL_CS2_TEAM)
    return data if isinstance(data, list) else []


def get_team(team_id: int) -> dict:
    data = request(_game_path(f"/teams/{int(team_id)}"),
                   ttl_seconds=CACHE_TTL_CS2_TEAM)
    return data if isinstance(data, dict) else {}


def list_team_matches(team_id: int, *, per_page: int = 30) -> list[dict]:
    """Recent completed matches for a team (most recent first)."""
    return list_past_matches(per_page=per_page, team_id=int(team_id))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def search_players(query: str, *, per_page: int = 15) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    params: dict[str, Any] = {
        "search[name]": q,
        "per_page":     per_page,
    }
    data = request(_game_path("/players"),
                   params=params, ttl_seconds=CACHE_TTL_CS2_PLAYER)
    return data if isinstance(data, list) else []


def get_player(player_id: int) -> dict:
    data = request(_game_path(f"/players/{int(player_id)}"),
                   ttl_seconds=CACHE_TTL_CS2_PLAYER)
    return data if isinstance(data, dict) else {}


def list_player_team_matches(team_id: int | None, *,
                              per_page: int = 30) -> list[dict]:
    """Recent team matches used as a stand-in for a player's match sample.

    PandaScore does not expose a direct ``/players/{id}/matches`` endpoint
    in a usable shape on the free tier. We fetch the player's current team
    matches and the caller filters roster stats out of each match detail.
    """
    if not team_id:
        return []
    return list_past_matches(per_page=per_page, team_id=int(team_id))


# ---------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------


def list_running_tournaments(per_page: int = 20) -> list[dict]:
    data = request(_game_path("/tournaments/running"),
                   params={"per_page": per_page},
                   ttl_seconds=CACHE_TTL_CS2_MATCH_LIST)
    return data if isinstance(data, list) else []


def list_upcoming_tournaments(per_page: int = 20) -> list[dict]:
    data = request(_game_path("/tournaments/upcoming"),
                   params={"per_page": per_page},
                   ttl_seconds=CACHE_TTL_CS2_MATCH_LIST)
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Public error alias for callers that want to handle missing-token specifically.
# ---------------------------------------------------------------------------

__all__ = [
    "PandaScoreError",
    "list_upcoming_matches",
    "list_past_matches",
    "get_match",
    "search_teams",
    "get_team",
    "list_team_matches",
    "search_players",
    "get_player",
    "list_player_team_matches",
    "list_running_tournaments",
    "list_upcoming_tournaments",
]
