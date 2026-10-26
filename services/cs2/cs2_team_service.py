"""Team-level research for the CS2 page.

HLTV provides:
  * Team profile (name, country, roster) — getTeam (1 request)
  * Recent match results — getResults with teamIds filter (1 request)
  * Team stats (win rate, per-map stats) — getTeamStats (4-5 requests, lazy)
"""

from __future__ import annotations

import logging

from services.cs2 import cs2_data_provider as provider
from services.cs2.models import CS2MatchResult, CS2Team, CS2TeamStats

logger = logging.getLogger(__name__)


def find_team(name: str) -> CS2Team | None:
    """Search for a team by name (2 HLTV requests, cached 24 h)."""
    if not name or len(name.strip()) < 2:
        return None
    try:
        return provider.search_team(name.strip())
    except Exception as exc:
        logger.warning("Team search failed for %r: %s", name, exc)
        return None


def load_team_stats(team_id: int) -> CS2TeamStats | None:
    """Full team stats (4-5 HLTV requests, cached 24 h). Lazy-load only."""
    try:
        return provider.team_stats(team_id)
    except Exception as exc:
        logger.warning("Team stats failed for id=%s: %s", team_id, exc)
        return None


def load_team_results(team_id: int, limit: int = 15) -> list[CS2MatchResult]:
    """Recent results for a team (uses getResults filter, 1 request)."""
    try:
        return provider.team_results(team_id, limit=limit)
    except Exception as exc:
        logger.warning("Team results failed for id=%s: %s", team_id, exc)
        return []


def form_string(results: list[CS2MatchResult], team_id: int | None,
                team_name: str | None, n: int = 5) -> str:
    """Return a compact win/loss string like 'W W L W L' from recent results."""
    chars: list[str] = []
    for r in results[:n]:
        if r.score_t1 is None or r.score_t2 is None:
            chars.append("?")
            continue
        t1_is_our_team = (
            (team_id and r.team1 and r.team1.id == team_id)
            or (team_name and r.team1 and
                team_name.lower() in (r.team1.name or "").lower())
        )
        if t1_is_our_team:
            chars.append("W" if r.score_t1 > r.score_t2 else "L")
        else:
            chars.append("W" if r.score_t2 > r.score_t1 else "L")
    return " ".join(chars) if chars else "—"


def win_rate(results: list[CS2MatchResult], team_id: int | None,
             team_name: str | None) -> float | None:
    """Compute win rate from a list of results for a given team."""
    wins = 0
    total = 0
    for r in results:
        if r.score_t1 is None or r.score_t2 is None:
            continue
        total += 1
        t1_is_ours = (
            (team_id and r.team1 and r.team1.id == team_id)
            or (team_name and r.team1 and
                team_name.lower() in (r.team1.name or "").lower())
        )
        if t1_is_ours:
            if r.score_t1 > r.score_t2:
                wins += 1
        else:
            if r.score_t2 > r.score_t1:
                wins += 1
    return round(wins / total, 2) if total > 0 else None


def map_pool_table(stats: CS2TeamStats) -> list[dict]:
    """Return a sorted list of map-pool dicts for display."""
    rows = []
    for ms in sorted(stats.map_stats or [], key=lambda x: -(x.win_rate or 0)):
        wr = ms.win_rate
        total = ms.wins + ms.losses
        rows.append({
            "Map": ms.map_name or "—",
            "W":   ms.wins,
            "L":   ms.losses,
            "Total": total,
            "Win%": f"{wr * 100:.1f}%" if wr is not None else "—",
        })
    return rows
