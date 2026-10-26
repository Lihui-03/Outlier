"""WNBA data access — parallel to NBA services, built on stats.nba.com (League 10)."""

from __future__ import annotations

# League id used by stats.nba.com for WNBA (see ``nba_api.stats.library.parameters.LeagueID``).
LEAGUE_ID = "10"

__all__ = ["LEAGUE_ID"]
