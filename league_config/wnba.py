"""WNBA-specific configuration.

stats.nba.com uses LeagueID ``10`` and **calendar-year** season strings
(e.g. ``\"2025\"`` for the 2025 WNBA campaign), unlike the NBA's
``\"2025-26\"`` format.

This package lives under ``league_config/`` because a top-level ``config.py``
module shadows a ``config/`` directory for imports.
"""

from __future__ import annotations

from datetime import date

# Mirror the root ``config.STAT_COLUMNS`` but **exclude** quarter/half markets (no
# PBP pipeline for WNBA yet) and threshold stats we have not validated.
WNBA_STAT_COLUMNS: dict[str, str] = {
    "Points": "PTS",
    "Rebounds": "REB",
    "Assists": "AST",
    "3PM": "FG3M",
    "Steals": "STL",
    "Blocks": "BLK",
    "Turnovers": "TOV",
    "Off. Rebounds": "OREB",
    "Def. Rebounds": "DREB",
    "FT Made": "FTM",
    "FT Attempts": "FTA",
    "3P Attempts": "FG3A",
    "Personal Fouls": "PF",
    "PRA": "PRA",
    "PA": "PA",
    "RA": "RA",
    "PR": "PR",
    "Stocks (STL+BLK)": "Stocks",
}

# Same shape as config.DERIVED_STATS but only for keys present above.
WNBA_DERIVED_STATS: dict[str, tuple[str, ...]] = {
    "PRA": ("PTS", "REB", "AST"),
    "PA": ("PTS", "AST"),
    "RA": ("REB", "AST"),
    "PR": ("PTS", "REB"),
    "Stocks": ("STL", "BLK"),
}

WNBA_THRESHOLD_STATS: frozenset[str] = frozenset()  # DD/TD not enabled yet

# Filters modal: season radio labels → wnba_api season string.
# Keep short year labels; map to API via `wnba_season_id_from_selector`.
WNBA_SEASON_SELECTOR_OPTIONS: tuple[str, ...] = ("2025", "2024", "2023", "All")


def current_wnba_season_year() -> int:
    """Rough 'current WNBA season' calendar year (May–Sept peak)."""
    today = date.today()
    # WNBA runs ~May–September; before May, attribute to prior calendar label.
    if today.month < 5:
        return today.year - 1
    return today.year


def wnba_season_strings_recent(n: int = 8) -> list[str]:
    """Return ``[\"2025\", \"2024\", ...]`` newest first."""
    y = current_wnba_season_year()
    return [str(y - i) for i in range(max(1, n))]


def default_wnba_filter_season_label() -> str:
    """Default Filters modal season chip for WNBA (matches `WNBA_SEASON_SELECTOR_OPTIONS` [0])."""
    y = current_wnba_season_year()
    return str(y)


def wnba_season_id_from_selector(selection: str) -> str | None:
    """Map UI label to single-year season id, or ``None`` for 'All'."""
    if selection == "All":
        return None
    return selection if selection.isdigit() else None
