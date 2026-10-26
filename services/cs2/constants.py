"""CS2 product-specific constants.

Kept out of the global ``config.py`` because these are CS2-product concepts
(markets, map pool) rather than runtime/infra knobs. Runtime constants (URLs,
TTLs, rate limits) still live in ``config.py`` per the single-source-of-truth
convention.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Active competitive map pool (Valve / ESL rotation as of late 2025).
# If the pool changes, update this tuple; every aggregation respects ordering.
# ---------------------------------------------------------------------------
CS2_ACTIVE_MAPS: tuple[str, ...] = (
    "Mirage",
    "Inferno",
    "Nuke",
    "Ancient",
    "Anubis",
    "Dust2",
    "Train",
)

# Historical / deprecated maps. Surface them only when they appear in match
# data so old pro matches still show on team pages.
CS2_LEGACY_MAPS: tuple[str, ...] = (
    "Overpass",
    "Vertigo",
    "Cache",
    "Cobblestone",
    "Tuscan",
)

ALL_KNOWN_MAPS: tuple[str, ...] = CS2_ACTIVE_MAPS + CS2_LEGACY_MAPS


def normalize_map_name(raw: str | None) -> str:
    """Normalise PandaScore / HLTV map names. Most feeds use Pascal-case
    strings ("de_mirage" / "Mirage"); we return the friendly form."""
    if not raw:
        return ""
    s = str(raw).strip()
    s = s.replace("de_", "").replace("cs_", "").strip()
    s = s.replace("_", " ")
    # Common aliases.
    aliases = {
        "Mirage": "Mirage", "Inferno": "Inferno", "Nuke": "Nuke",
        "Ancient": "Ancient", "Anubis": "Anubis", "Dust2": "Dust2",
        "Dust 2": "Dust2", "Train": "Train", "Overpass": "Overpass",
        "Vertigo": "Vertigo", "Cache": "Cache", "Cobblestone": "Cobblestone",
        "Tuscan": "Tuscan",
    }
    title = s.title()
    return aliases.get(title, title)


# ---------------------------------------------------------------------------
# CS2 research markets. Each entry is a UI-facing market name and a kind used
# by the page to decide what controls to render. Kinds:
#   "team_line"   → needs a side selector + projected win probability (lean)
#   "match_total" → needs a manual "maps" line for a series total
#   "map_total"   → needs a manual rounds line for a single map total
#   "map_spread"  → needs a manual round handicap for a single map
#   "player_prop" → needs a player + manual line + optional map filter
# Markets flagged ``needs_hltv=True`` are exposed as beta placeholders because
# PandaScore's free tier does not supply the underlying metric.
# ---------------------------------------------------------------------------
CS2_MARKETS: tuple[dict, ...] = (
    {"key": "match_winner",      "label": "Match Winner",
     "kind": "team_line",   "needs_hltv": False},
    {"key": "total_maps",        "label": "Total Maps (series)",
     "kind": "match_total", "needs_hltv": False},
    {"key": "total_rounds_map",  "label": "Map Total Rounds",
     "kind": "map_total",   "needs_hltv": False},
    {"key": "map_handicap",      "label": "Map Round Handicap",
     "kind": "map_spread",  "needs_hltv": False},
    {"key": "player_kills",      "label": "Player Kills (map)",
     "kind": "player_prop", "needs_hltv": False},
    {"key": "player_headshots",  "label": "Player Headshots",
     "kind": "player_prop", "needs_hltv": False},
    {"key": "player_adr",        "label": "Player ADR",
     "kind": "player_prop", "needs_hltv": True},
    {"key": "player_kast",       "label": "Player KAST%",
     "kind": "player_prop", "needs_hltv": True},
    {"key": "player_first_kills", "label": "First / Opening Kills",
     "kind": "player_prop", "needs_hltv": True},
    {"key": "pistol_winner",     "label": "Pistol Round Winner",
     "kind": "team_line",   "needs_hltv": True},
)


def market_lookup(key: str) -> dict | None:
    """Return the market meta dict for a key, or None."""
    for m in CS2_MARKETS:
        if m["key"] == key:
            return m
    return None


# Best-of options for series research.
BEST_OF_OPTIONS: tuple[int, ...] = (1, 3, 5)

# Default manual-line placeholders so the user sees a sensible starting
# number; they still type the real sportsbook line themselves.
DEFAULT_MANUAL_LINES: dict[str, float] = {
    "total_maps":       2.5,
    "total_rounds_map": 26.5,
    "map_handicap":     -3.5,
    "player_kills":     18.5,
    "player_headshots": 8.5,
    "player_adr":       78.5,
    "player_kast":      72.5,
    "player_first_kills": 2.5,
}


# ---------------------------------------------------------------------------
# SQLite cache namespace. Single namespace keeps stale-fallback logic simple;
# cache_key encodes the endpoint path + params hash.
# ---------------------------------------------------------------------------
CACHE_NAMESPACE = "cs2_pandascore"
