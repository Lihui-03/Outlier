"""Player-level research for the CS2 page.

With HLTV as the primary source we now get real stats:
  * kills, deaths, K/D ratio
  * ADR (average damage per round) — overviewStatistics.damagePerRound
  * headshot % — overviewStatistics.headshots
  * HLTV rating 2.0 — overviewStatistics.rating2
  * opening kills / deaths / ratio — individualStatistics

Per-map history comes from getPlayerStats().matches[] — each entry gives
kills, deaths, and per-map rating for the player's recent maps.

KAST% is not available via the gigobyte/hltv schema.
"""

from __future__ import annotations

import logging
from typing import Any

from services.cs2 import cs2_data_provider as provider
from services.cs2.models import CS2Player, CS2PlayerStats, CS2PlayerMapEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search / profile
# ---------------------------------------------------------------------------


def find_player(name: str) -> CS2Player | None:
    """Look up a player profile by IGN.

    Caution: makes up to 2 HLTV requests (cached 24 h).
    Show a spinner / "searching…" indicator before calling.
    """
    if not name or len(name.strip()) < 2:
        return None
    try:
        return provider.search_player(name.strip())
    except Exception as exc:
        logger.warning("Player search failed for %r: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Stats (lazy — only call when user explicitly requests)
# ---------------------------------------------------------------------------


def load_player_stats(player_id: int) -> CS2PlayerStats | None:
    """Load full player stats (3 HLTV requests, cached 24 h).

    Call this *only* when the user clicks "Load stats" — not on page load.
    """
    try:
        return provider.player_stats(player_id)
    except Exception as exc:
        logger.warning("Player stats failed for id=%s: %s", player_id, exc)
        return None


# ---------------------------------------------------------------------------
# Derived metrics (from CS2PlayerStats)
# ---------------------------------------------------------------------------


def kills_per_map(stats: CS2PlayerStats) -> float | None:
    """Average kills per map from per-map history."""
    maps = stats.recent_maps or []
    kills = [m.kills for m in maps if m.kills is not None]
    return sum(kills) / len(kills) if kills else None


def kd_from_maps(stats: CS2PlayerStats) -> float | None:
    """K/D ratio computed from per-map history (all maps in sample)."""
    maps = stats.recent_maps or []
    ks = [m.kills for m in maps if m.kills is not None]
    ds = [m.deaths for m in maps if m.deaths is not None]
    if len(ks) == 0 or len(ds) == 0:
        return None
    k_total = sum(ks)
    d_total = sum(ds)
    return round(k_total / d_total, 2) if d_total > 0 else None


def kills_on_map(stats: CS2PlayerStats, map_name: str) -> list[CS2PlayerMapEntry]:
    """Filter per-map history to a specific map name."""
    from services.cs2.constants import normalize_map_name
    target = normalize_map_name(map_name)
    return [m for m in (stats.recent_maps or []) if m.map_name == target]


def avg_kills_on_map(stats: CS2PlayerStats, map_name: str) -> float | None:
    entries = kills_on_map(stats, map_name)
    vals = [m.kills for m in entries if m.kills is not None]
    return sum(vals) / len(vals) if vals else None


def avg_rating(stats: CS2PlayerStats) -> float | None:
    """Average HLTV rating from per-map history."""
    maps = stats.recent_maps or []
    ratings = [m.rating for m in maps if m.rating is not None]
    return round(sum(ratings) / len(ratings), 2) if ratings else stats.rating


def recent_form(stats: CS2PlayerStats, n: int = 5) -> list[CS2PlayerMapEntry]:
    """Return the N most recent per-map entries."""
    return (stats.recent_maps or [])[:n]


def sample_for_map(sample: dict, map_name: str | None) -> list[dict]:
    """Return per-map entries filtered to one map.  Accepts the legacy dict
    format produced by ``build_player_sample_dict`` so the projection engine
    keeps working without changes."""
    from services.cs2.constants import normalize_map_name as _norm
    maps = sample.get("maps") or []
    if not map_name:
        return list(maps)
    target = _norm(map_name)
    return [x for x in maps if _norm(str(x.get("map", ""))) == target]


def build_player_sample_dict(stats: CS2PlayerStats | None) -> dict:
    """Build the legacy flat-dict format expected by the projection engine.

    The projection engine (``cs2_projection_service``) uses:
        has_stats, kills_avg, headshots_avg, hs_pct_avg, sample_size, maps[]
    This function bridges from the new ``CS2PlayerStats`` model to that format.
    """
    if stats is None or not stats.recent_maps:
        return {
            "player": {},
            "has_stats": False,
            "kills_avg": None,
            "deaths_avg": None,
            "headshots_avg": None,
            "hs_pct_avg": None,
            "kd_avg": None,
            "sample_size": 0,
            "maps": [],
        }

    maps_list = [
        {
            "kills":     m.kills,
            "deaths":    m.deaths,
            "headshots": None,              # HLTV per-map doesn't include HS count
            "map":       m.map_name or "",
            "rating":    m.rating,
            "date_unix": m.date_unix_ms,
        }
        for m in stats.recent_maps
    ]

    # Overall kills / deaths averages from per-map history.
    ks = [m.kills for m in stats.recent_maps if m.kills is not None]
    ds = [m.deaths for m in stats.recent_maps if m.deaths is not None]
    kills_avg = sum(ks) / len(ks) if ks else None
    deaths_avg = sum(ds) / len(ds) if ds else None
    kd_avg = (kills_avg / deaths_avg) if (kills_avg and deaths_avg and deaths_avg > 0) else None

    # HS% comes from the overview stats (not per-map); project headshots as HS% × kills.
    hs_pct_frac = (stats.hs_pct / 100.0) if stats.hs_pct is not None else None
    headshots_avg = (kills_avg * hs_pct_frac) if (kills_avg and hs_pct_frac) else None

    return {
        "player":        {"id": stats.player_id, "ign": stats.ign},
        "has_stats":     True,
        "kills_avg":     kills_avg,
        "deaths_avg":    deaths_avg,
        "headshots_avg": headshots_avg,
        "hs_pct_avg":    hs_pct_frac,
        "kd_avg":        kd_avg,
        "sample_size":   len(stats.recent_maps),
        "maps":          maps_list,
        "_stale":        stats._stale,
        "_source":       stats._source,
    }


def build_player_summary(stats: CS2PlayerStats | None) -> dict:
    """Return a flat summary dict for the UI cards.  All values may be None."""
    if stats is None:
        return {
            "ign": None,
            "kills_per_round": None,
            "kd_ratio": None,
            "hs_pct": None,
            "adr": None,
            "rating": None,
            "opening_kill_ratio": None,
            "maps_played": None,
            "has_stats": False,
        }

    kd = stats.kd_ratio or kd_from_maps(stats)
    rating_val = avg_rating(stats)

    return {
        "ign":               stats.ign,
        "kills_per_round":   stats.kills_per_round,
        "kd_ratio":          kd,
        "hs_pct":            stats.hs_pct,
        "adr":               stats.adr,
        "rating":            rating_val,
        "opening_kill_ratio": stats.opening_kill_ratio,
        "maps_played":       stats.maps_played,
        "total_kills":       stats.total_kills,
        "total_deaths":      stats.total_deaths,
        "opening_kills":     stats.opening_kills,
        "opening_deaths":    stats.opening_deaths,
        "has_stats":         True,
        "_stale":            stats._stale,
        "_source":           stats._source,
    }
