"""Map-centric research for the CS2 page.

Aggregates team and player performance by map using data from the HLTV
provider.  Data is derived from:
  * CS2TeamStats.map_stats — per-map team win rates
  * CS2PlayerStats.recent_maps — per-map player kill/rating history
"""

from __future__ import annotations

import logging
from collections import defaultdict

from services.cs2.constants import CS2_ACTIVE_MAPS, normalize_map_name
from services.cs2.models import CS2PlayerStats, CS2TeamStats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Map pool helpers
# ---------------------------------------------------------------------------


def map_win_rates(stats: CS2TeamStats) -> dict[str, float | None]:
    """Return {map_name: win_rate} for all maps in a team's stats."""
    return {ms.map_name: ms.win_rate for ms in (stats.map_stats or [])}


def strongest_map(stats: CS2TeamStats) -> str | None:
    """Return the map name with the highest win rate."""
    if not stats.map_stats:
        return None
    best = max(
        (ms for ms in stats.map_stats if ms.win_rate is not None),
        key=lambda x: x.win_rate,  # type: ignore[arg-type]
        default=None,
    )
    return best.map_name if best else None


def weakest_map(stats: CS2TeamStats) -> str | None:
    """Return the map name with the lowest win rate (minimum played > 0)."""
    if not stats.map_stats:
        return None
    eligible = [ms for ms in stats.map_stats
                if ms.win_rate is not None and (ms.wins + ms.losses) >= 3]
    if not eligible:
        return None
    return min(eligible, key=lambda x: x.win_rate).map_name  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Player map breakdown
# ---------------------------------------------------------------------------


def player_kills_by_map(player_stats: CS2PlayerStats) -> dict[str, list[int]]:
    """Return {map_name: [kills_per_appearance]} from recent map history."""
    result: dict[str, list[int]] = defaultdict(list)
    for entry in player_stats.recent_maps or []:
        if entry.kills is not None and entry.map_name:
            result[entry.map_name].append(entry.kills)
    return dict(result)


def player_avg_kills_by_map(player_stats: CS2PlayerStats) -> dict[str, float]:
    """Return {map_name: avg_kills} for maps with at least 1 appearance."""
    raw = player_kills_by_map(player_stats)
    return {m: round(sum(v) / len(v), 1) for m, v in raw.items() if v}


def player_avg_rating_by_map(player_stats: CS2PlayerStats) -> dict[str, float]:
    """Return {map_name: avg_rating} from per-map history."""
    by_map: dict[str, list[float]] = defaultdict(list)
    for entry in player_stats.recent_maps or []:
        if entry.rating is not None and entry.map_name:
            by_map[entry.map_name].append(entry.rating)
    return {m: round(sum(v) / len(v), 2) for m, v in by_map.items() if v}


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def map_matchup_summary(stats_a: CS2TeamStats, stats_b: CS2TeamStats,
                        maps: list[str] | None = None) -> list[dict]:
    """Return a per-map comparison between two teams.

    Returns a list of dicts with keys:
        map, team_a_wr, team_b_wr, edge_team, edge_pct
    """
    maps = maps or list(CS2_ACTIVE_MAPS)
    wr_a = map_win_rates(stats_a)
    wr_b = map_win_rates(stats_b)

    rows: list[dict] = []
    for m in maps:
        norm = normalize_map_name(m)
        a = wr_a.get(norm)
        b = wr_b.get(norm)
        edge_team = None
        edge_pct = None
        if a is not None and b is not None:
            edge = a - b
            if edge > 0.05:
                edge_team = stats_a.team_name or "Team A"
                edge_pct = round(edge * 100, 1)
            elif edge < -0.05:
                edge_team = stats_b.team_name or "Team B"
                edge_pct = round(-edge * 100, 1)
            else:
                edge_team = "Even"
                edge_pct = 0.0
        rows.append({
            "Map":        norm,
            "Team A %":   f"{a * 100:.1f}%" if a is not None else "—",
            "Team B %":   f"{b * 100:.1f}%" if b is not None else "—",
            "Edge":       edge_team or "—",
            "Edge %":     f"{edge_pct:.1f}%" if edge_pct is not None else "—",
        })
    return rows
