"""Central configuration for the NBA prop research dashboard.

Keeping stat keys, weights, and paths in one module so the rest of the app
never hard-codes them. Adjust values here and the whole app follows suit.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env from the project root (if it exists) before any os.environ.get calls.
# python-dotenv is a soft dependency — app still runs without it.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "cache.sqlite"
ASSETS_DIR = PROJECT_ROOT / "assets"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

# Friendly label -> column name in the game-log DataFrame.
# Derived stats (PRA, PR, DD, etc.) use the same name once models.stats
# adds them as columns.
STAT_COLUMNS: dict[str, str] = {
    # ── Standard full-game stats ──────────────────────────────────
    "Points":               "PTS",
    "Rebounds":             "REB",
    "Assists":              "AST",
    "3PM":                  "FG3M",
    "Steals":               "STL",
    "Blocks":               "BLK",
    "Turnovers":            "TOV",
    "Off. Rebounds":        "OREB",
    "Def. Rebounds":        "DREB",
    "FT Made":              "FTM",
    "FT Attempts":          "FTA",
    "3P Attempts":          "FG3A",
    "Personal Fouls":       "PF",
    # ── Combo / derived ───────────────────────────────────────────
    "PRA":                  "PRA",
    "PA":                   "PA",
    "RA":                   "RA",
    "PR":                   "PR",
    "Stocks (STL+BLK)":     "Stocks",
    # ── Threshold-based (0/1 per game) ───────────────────────────
    "Double-Double":        "DD",
    "Triple-Double":        "TD",
    # ── Quarter / half stats (loaded lazily) ─────────────────────
    "1Q Points":            "Q1_PTS",
    "1Q Rebounds":          "Q1_REB",
    "1Q Assists":           "Q1_AST",
    "1Q 3PM":               "Q1_FG3M",
    "1H Points":            "H1_PTS",
    "1H Rebounds":          "H1_REB",
    "1H Assists":           "H1_AST",
    "1H 3PM":               "H1_FG3M",
}

# Sum-based derived stats: label -> tuple of base columns that sum to it.
DERIVED_STATS: dict[str, tuple[str, ...]] = {
    "PRA":    ("PTS", "REB", "AST"),
    "PA":     ("PTS", "AST"),
    "RA":     ("REB", "AST"),
    "PR":     ("PTS", "REB"),
    "Stocks": ("STL", "BLK"),
}

# Threshold-based 0/1 stats: name -> how to compute them per row.
# A row earns a 1 when at least `min_categories` of `columns` each
# reach `threshold`. This covers Double-Double and Triple-Double.
THRESHOLD_STATS: dict[str, dict] = {
    "DD": {
        "columns":        ["PTS", "REB", "AST", "STL", "BLK"],
        "threshold":      10,
        "min_categories": 2,
    },
    "TD": {
        "columns":        ["PTS", "REB", "AST", "STL", "BLK"],
        "threshold":      10,
        "min_categories": 3,
    },
}

# Column names lazy-loaded via ``partial_game_service`` (PlayByPlayV3-derived).
QUARTER_STAT_COLS: frozenset[str] = frozenset({
    "Q1_PTS", "Q1_REB", "Q1_AST", "Q1_FG3M",
    "H1_PTS", "H1_REB", "H1_AST", "H1_FG3M",
})

# Numeric columns we normalize when present in the game log. Missing
# columns are left absent rather than padded; downstream code tolerates
# their absence.
BASE_NUMERIC_COLUMNS: tuple[str, ...] = (
    "PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV", "MIN",
    "OREB", "DREB",           # Tier 1 additions
    "FTM", "FTA", "FG3A",     # Tier 1 additions
    "PF",                     # Tier 1 addition
    "FGA",                    # used by usage-proxy chart
)


# ---------------------------------------------------------------------------
# Projection engine defaults
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "w5": 0.50,
    "w10": 0.30,
    "wSeason": 0.20,
}

DEFAULT_ADJUSTMENTS: dict[str, bool] = {
    "matchup": False,
    "home_away": False,
    "minutes": False,
}


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------

def current_nba_season() -> str:
    """Return the active NBA season string (e.g. "2025-26").

    NBA season labels use the start year of the season. The season rolls
    over in October, so months Oct-Dec belong to the "year-year+1" season
    and Jan-Sep belong to "year-1-year".
    """
    from datetime import date

    today = date.today()
    if today.month >= 10:
        start = today.year
    else:
        start = today.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def recent_seasons(n: int = 8) -> list[str]:
    """Return the most recent `n` season labels, newest first."""
    current = current_nba_season()
    start_year = int(current.split("-")[0])
    seasons = []
    for i in range(n):
        y = start_year - i
        seasons.append(f"{y}-{str(y + 1)[-2:]}")
    return seasons


# ---------------------------------------------------------------------------
# Season types
# ---------------------------------------------------------------------------

# UI label -> nba_api value for `season_type_all_star`.
# "All" is a pseudo-value we handle ourselves by combining the three real ones.
SEASON_TYPES: dict[str, str] = {
    "Regular Season": "Regular Season",
    "Playoffs": "Playoffs",
    "Preseason": "Pre Season",
    "All (Pre + Regular + Playoffs)": "ALL",
}

DEFAULT_SEASON_TYPE_LABEL = "Regular Season"


# ---------------------------------------------------------------------------
# Filter system constants
# ---------------------------------------------------------------------------

# Markets (labels) that appear in the Stat selector and the filter system.
# Each is a key from STAT_COLUMNS so resolve_stat_column() still works.
MARKETS: tuple[str, ...] = tuple(STAT_COLUMNS.keys())

# Season selector options presented in the Filters modal. "All" merges
# the three most-recent seasons at fetch time (handled in data_service).
SEASON_SELECTOR_OPTIONS: tuple[str, ...] = ("25/26", "24/25", "23/24", "All")

# Default choices for the Filters modal "Games" row (display label → stored mode).
# Modes mirror ``apply_game_count_filter`` in ``filter_service``: digit strings =
# ``head(N)`` on the newest-first gamelog **after** all other filters.
GAME_COUNT_RADIO_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Last 30", "30"),
    ("Last 20", "20"),
    ("Last 15", "15"),
    ("Last 10", "10"),
    ("Last 5", "5"),
    ("Max", "Max"),
    ("Custom", "Custom"),
)
GAME_COUNT_CODES: tuple[str, ...] = tuple(code for _, code in GAME_COUNT_RADIO_OPTIONS)
GAME_COUNT_LABEL_BY_CODE: dict[str, str] = dict(
    (code, label) for label, code in GAME_COUNT_RADIO_OPTIONS
)
# Legacy name — same as ``GAME_COUNT_CODES`` for imports that expected value-only tuples.
GAME_COUNT_OPTIONS: tuple[str, ...] = GAME_COUNT_CODES


def formatted_game_count_filter_caption(mode: str, *, custom_n: int | None = None) -> str:
    """Chip / chart caption label for ``FilterState.game_count_mode``."""
    if mode == "Max":
        return "Max games"
    if mode == "Custom":
        n = int(custom_n) if custom_n is not None else 15
        return f"Custom last-{n}"
    return GAME_COUNT_LABEL_BY_CODE.get(mode, f"Last {mode}")

# Chips shown in the "Stats" tab of the filter modal. Selecting one toggles
# a blue overlay line on the main Research bar chart. Label -> column.
STAT_OVERLAY_CHIPS: dict[str, str] = {
    "Minutes":  "MIN",
    "Points":   "PTS",
    "Assists":  "AST",
    "Rebounds": "REB",
    "FGA":      "FGA",
    "3PA":      "FG3A",
    "3PM":      "FG3M",
    "FTA":      "FTA",
    "Fouls":    "PF",
    "TOV":      "TOV",
    "Steals":   "STL",
    "Blocks":   "BLK",
    "OREB":     "OREB",
    "DREB":     "DREB",
}

# Base-stat columns that `defense_service.fetch_team_defense()` supports
# cleanly (used for real opponent-rank chips). Everything else falls through
# to a beta chip.
OPP_RANK_SUPPORTED: frozenset[str] = frozenset({
    "PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV",
})

# Map: suggested-filter chips per market (transparent MVP heuristics).
# Each entry is a chip key that ui/filter_modal.py knows how to render.
# Real filters are listed first; beta chips come after and are drawn in
# a muted style by ui/components.py.
SUGGESTED_FILTERS_PER_MARKET: dict[str, tuple[str, ...]] = {
    "PTS": ("minutes_high", "home_away", "h2h", "b2b", "opp_pts_rank", "opp_pace_rank",
            "opp_def_rtg_rank", "def_vs_position", "def_vs_dpt", "def_vs_csu", "def_vs_pull"),
    "REB": ("minutes_high", "home_away", "h2h", "b2b", "opp_reb_rank", "opp_pace_rank",
            "def_vs_position"),
    "AST": ("minutes_high", "home_away", "h2h", "b2b", "opp_ast_rank", "opp_pace_rank",
            "def_vs_position"),
    "FG3M": ("minutes_high", "home_away", "h2h", "opp_3pm_rank", "opp_3pa_allowed",
             "def_vs_position"),
    "STL": ("minutes_high", "home_away", "h2h", "opp_stl_rank", "opp_pace_rank"),
    "BLK": ("minutes_high", "home_away", "h2h", "opp_blk_rank"),
    "TOV": ("minutes_high", "home_away", "h2h", "opp_tov_rank"),
    "PRA": ("minutes_high", "home_away", "h2h", "b2b", "opp_pts_rank", "opp_pace_rank"),
    "PA":  ("minutes_high", "home_away", "h2h", "opp_pts_rank", "opp_pace_rank"),
    "RA":  ("minutes_high", "home_away", "h2h", "opp_reb_rank", "opp_pace_rank"),
    "PR":  ("minutes_high", "home_away", "h2h", "opp_pts_rank", "opp_pace_rank"),
    "Stocks": ("minutes_high", "home_away", "h2h", "opp_stl_rank", "opp_blk_rank"),
    "DD":  ("minutes_high", "home_away", "h2h", "opp_pace_rank"),
    "TD":  ("minutes_high", "home_away", "h2h", "opp_pace_rank"),
    "Q1_PTS":   ("minutes_high", "home_away", "h2h"),
    "Q1_REB":   ("minutes_high", "home_away", "h2h"),
    "Q1_AST":   ("minutes_high", "home_away", "h2h"),
    "Q1_FG3M":  ("minutes_high", "home_away", "h2h"),
    "H1_PTS":   ("minutes_high", "home_away", "h2h"),
    "H1_REB":   ("minutes_high", "home_away", "h2h"),
    "H1_AST":   ("minutes_high", "home_away", "h2h"),
    "H1_FG3M":  ("minutes_high", "home_away", "h2h"),
}

# Opp Rankings tab sections. Each section has a human title and a list of
# chips. Chips are dicts with keys:
#   "key":    unique id used by filter state
#   "label":  button text
#   "status": "real" or "beta"
#   "col":    (real-only) base stat column used to compute the rank
OPP_RANK_SECTIONS: dict[str, list[dict]] = {
    "Team Defense": [
        {"key": "opp_pts_rank",  "label": "Opp PTS allowed",  "status": "real", "col": "PTS"},
        {"key": "opp_reb_rank",  "label": "Opp REB allowed",  "status": "real", "col": "REB"},
        {"key": "opp_ast_rank",  "label": "Opp AST allowed",  "status": "real", "col": "AST"},
        {"key": "opp_3pm_rank",  "label": "Opp 3PM allowed",  "status": "real", "col": "FG3M"},
        {"key": "opp_stl_rank",  "label": "Opp STL allowed",  "status": "real", "col": "STL"},
        {"key": "opp_blk_rank",  "label": "Opp BLK allowed",  "status": "real", "col": "BLK"},
        {"key": "opp_tov_rank",  "label": "Opp TOV forced",   "status": "real", "col": "TOV"},
        {"key": "opp_pace_rank", "label": "Opp Pace",         "status": "real", "col": "PACE"},
        {"key": "opp_def_rtg_rank", "label": "Opp DefRtg",    "status": "real", "col": "DEF_RATING"},
        {"key": "opp_paint_pts", "label": "Opp Paint PTS allowed", "status": "beta"},
        {"key": "opp_3pa_allowed", "label": "Opp 3PA allowed",    "status": "beta"},
        {"key": "opp_fta_allowed", "label": "Opp FTA allowed",    "status": "beta"},
    ],
    "Play Types": [
        {"key": "pt_spot_up",  "label": "Spot Up",         "status": "beta"},
        {"key": "pt_trans",    "label": "Transition",      "status": "beta"},
        {"key": "pt_pnr_bh",   "label": "PnR Ball Handler","status": "beta"},
        {"key": "pt_pnr_rm",   "label": "PnR Roll Man",    "status": "beta"},
        {"key": "pt_iso",      "label": "Isolation",       "status": "beta"},
        {"key": "pt_handoff",  "label": "Handoff",         "status": "beta"},
        {"key": "pt_putback",  "label": "Putbacks",        "status": "beta"},
        {"key": "pt_postup",   "label": "Post Up",         "status": "beta"},
        {"key": "pt_offscr",   "label": "Off Screen",      "status": "beta"},
    ],
    "Shooting Zones": [
        {"key": "zone_ra",       "label": "Restricted Area",     "status": "beta"},
        {"key": "zone_paint_nra","label": "Paint (non-RA)",      "status": "beta"},
        {"key": "zone_mid",      "label": "Mid Range",           "status": "beta"},
        {"key": "zone_atb3",     "label": "Above the Break 3",   "status": "beta"},
        {"key": "zone_lc3",      "label": "Left Corner 3",       "status": "beta"},
        {"key": "zone_rc3",      "label": "Right Corner 3",      "status": "beta"},
    ],
    "Assist Zones": [
        {"key": "daz",   "label": "DAZ",  "status": "beta"},
        {"key": "daz2",  "label": "DAZ2", "status": "beta"},
    ],
}

# Splits tab chips. "real" means the filter actually narrows the sample.
SPLITS_CHIPS: list[dict] = [
    {"key": "h2h",          "label": "H2H (current opp)", "status": "real"},
    {"key": "home",         "label": "Home",              "status": "real"},
    {"key": "away",         "label": "Away",              "status": "real"},
    {"key": "regular",      "label": "Regular Season",    "status": "real"},
    {"key": "playoffs",     "label": "Playoffs",          "status": "real"},
    {"key": "b2b",          "label": "Back-to-Back",      "status": "real"},
    {"key": "win",          "label": "Wins only",         "status": "real"},
    {"key": "loss",         "label": "Losses only",       "status": "real"},
    {"key": "margin_close", "label": "Close games (±5)",  "status": "real"},
    {"key": "margin_blowout","label": "Blowouts (>15)",   "status": "real"},
    {"key": "closing_spread","label": "Closing Spread",   "status": "beta"},
    {"key": "closing_total", "label": "Closing Total",    "status": "beta"},
]

# Defensive-context timeframe for Opp Rankings. Currently MVP computes
# season-long ranks; others are visual selectors for future work.
DEFENSIVE_TIMEFRAMES: tuple[str, ...] = ("Season", "L15", "L10", "L5")

# Colour tokens used by ui/theme.py and ui/graph.py. Centralised so a
# single change updates every component.
COLORS: dict[str, str] = {
    "over":       "#2ecc71",   # bars above the prop line
    "under":      "#e74c3c",   # bars below
    "line":       "#f1c40f",   # prop line
    "overlay":    "#3498db",   # stat overlay line on graph
    "rank_good":  "#2ecc71",   # favourable matchup rank
    "rank_bad":   "#e74c3c",   # tough matchup rank
    "rank_mid":   "#95a5a6",
    "chip_bg":    "#1f2a36",
    "chip_bg_on": "#2e4a66",
    "chip_border":"#3a4a60",
    "beta_bg":    "#2a2330",
    "beta_text":  "#b58bcf",
}


# ---------------------------------------------------------------------------
# Cache TTLs (seconds)
# ---------------------------------------------------------------------------

CACHE_TTL_PLAYERS = int(os.environ.get("CACHE_TTL_PLAYERS", 24 * 60 * 60))       # 1 day
CACHE_TTL_GAMELOG = int(os.environ.get("CACHE_TTL_GAMELOG", 6 * 60 * 60))        # 6 hours
CACHE_TTL_SCHEDULE = int(os.environ.get("CACHE_TTL_SCHEDULE", 60 * 60))          # 1 hour
CACHE_TTL_DEFENSE = int(os.environ.get("CACHE_TTL_DEFENSE", 12 * 60 * 60))       # 12 hours
CACHE_TTL_TEAM_GAMELOG = int(os.environ.get("CACHE_TTL_TEAM_GAMELOG", 6 * 60 * 60))   # 6 hours
# Line score (BoxScoreSummaryV2) is historical once the game is final, so it is
# cached essentially forever. Set a finite but very long TTL as a belt-and-suspenders.
CACHE_TTL_LINESCORE = int(os.environ.get("CACHE_TTL_LINESCORE", 365 * 24 * 60 * 60))  # 1 year

# Research bar-chart hover: optional BoxScorePlayerTrackV3 per game (rebound chances,
# assists passes / potential assists, etc.). Set to 0/false to skip those fetches.
_enrich_rc_env = (os.environ.get("TOOLTIP_ENRICH_REBOUND_CHANCES", "1") or "1").strip().lower()
TOOLTIP_ENRICH_REBOUND_CHANCES = _enrich_rc_env not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Game-market research mode (Full game total / team total / spread / halves)
# ---------------------------------------------------------------------------

# Display order for the markets dropdown on the Game Markets page.
GAME_MARKETS: tuple[str, ...] = (
    "Full Game Total",
    "Team Total",
    "Full Game Spread",
    "1st Half Total",
    "1st Half Spread",
    "2nd Half Total",
    "2nd Half Spread",
)

# Which markets need a team-side picker.
GAME_MARKET_NEEDS_SIDE: dict[str, bool] = {
    "Full Game Total":   False,
    "Team Total":        True,
    "Full Game Spread":  True,
    "1st Half Total":    False,
    "1st Half Spread":   True,
    "2nd Half Total":    False,
    "2nd Half Spread":   True,
}

# Which markets project a TOTAL vs a MARGIN. Drives chart + lean wording.
GAME_MARKET_KIND: dict[str, str] = {
    "Full Game Total":   "total",
    "Team Total":        "total",
    "Full Game Spread":  "margin",
    "1st Half Total":    "total",
    "1st Half Spread":   "margin",
    "2nd Half Total":    "total",
    "2nd Half Spread":   "margin",
}

# Default manual-line placeholders. The user still types the real number.
GAME_MARKET_DEFAULT_LINE: dict[str, float] = {
    "Full Game Total":   229.5,
    "Team Total":        114.5,
    "Full Game Spread":  -3.5,
    "1st Half Total":    112.5,
    "1st Half Spread":   -2.0,
    "2nd Half Total":    116.5,
    "2nd Half Spread":   -2.0,
}

# Approximate NBA home-court advantage, in points. Applied to the spread
# projection only (totals are near-symmetric). Free stats.nba.com does not
# publish this directly; 2.5 is the long-run consensus from public data.
HOME_COURT_ADV: float = 2.5

# Heuristic share of full-game scoring that falls in each half when we
# have no per-game line score data. Slight 2H skew accounts for foul-outs,
# late-game fouling, and up-tempo fourth quarters. Used only when the
# lazy BoxScoreSummaryV2 loader has not run for a given team/season.
HALF_SCORING_SHARE: dict[str, float] = {
    "1H": 0.48,
    "2H": 0.52,
}

# Spread share per half (how much of the full-game margin typically
# materialises in each half). Roughly even but slightly 2H-weighted.
HALF_SPREAD_SHARE: dict[str, float] = {
    "1H": 0.45,
    "2H": 0.55,
}

# Default recent-form window used by the game-market projection engine.
GAME_MARKET_RECENT_N: int = 10


# ---------------------------------------------------------------------------
# nba_api behaviour
# ---------------------------------------------------------------------------

# Per-request timeout. 12s is enough for a responsive stats.nba.com response;
# 30s (the old default) caused 90s+ blocking per failure (3 retries × 30s).
NBA_API_TIMEOUT = int(os.environ.get("NBA_API_TIMEOUT", 12))

# Max *retries* (so total attempts = retries + 1 = 2). Reducing from 2→1
# cuts worst-case block time from ~90s to ~26s per endpoint failure.
NBA_API_MAX_RETRIES = int(os.environ.get("NBA_API_MAX_RETRIES", 1))

NBA_API_RETRY_BACKOFF = float(os.environ.get("NBA_API_RETRY_BACKOFF", 1.5))

# --- In-process failure cache (prevents re-hammering a failing endpoint) ---
# If an endpoint/params combo failed within this window, calls fail fast
# instead of waiting for the full timeout again on Streamlit reruns.
NBA_API_FAILURE_COOLDOWN_S = int(os.environ.get("NBA_API_FAILURE_COOLDOWN_S", 90))

# --- Circuit breaker (global "server unavailable" guard) ---
# If >= THRESHOLD distinct endpoints fail within WINDOW seconds, stop all
# live requests for RESET seconds. Prevents cascade storms.
NBA_API_CB_THRESHOLD  = int(os.environ.get("NBA_API_CB_THRESHOLD",  3))
NBA_API_CB_WINDOW_S   = int(os.environ.get("NBA_API_CB_WINDOW_S",  60))
NBA_API_CB_RESET_S    = int(os.environ.get("NBA_API_CB_RESET_S",  120))

# --- Rate limiting ---
# Minimum gap between consecutive outbound calls to stats.nba.com.
# Prevents burst-sending all teammate log requests at once.
NBA_API_MIN_CALL_INTERVAL_S = float(os.environ.get("NBA_API_MIN_CALL_INTERVAL_S", 0.35))


# ---------------------------------------------------------------------------
# CS2 research mode (separate top-level product area, not an NBA submenu)
# ---------------------------------------------------------------------------
#
# Provider priority (as of the current HLTV-first config):
#   1. HLTV (gigobyte/hltv Node.js bridge) — primary research data source.
#      Scraping-based; relies on the aggressive cache + throttle + circuit
#      breaker architecture documented below.
#   2. Mock — development / offline fallback when HLTV is unavailable.
#   3. GRID Open Access — architectural placeholder for a future switch.
#      Not active by default; the Open Access tier does not currently
#      include the Central Data GraphQL endpoints we would need.
#
# Provider selection (CS2_PROVIDER env var):
#   CS2_PROVIDER=hltv   (default — requires `npm install` in services/cs2/hltv_bridge/)
#   CS2_PROVIDER=mock   (no network, no credentials)
#   CS2_PROVIDER=grid   (experimental — requires GRID_API_KEY with Central Data access)
#
# If CS2_PROVIDER=hltv but the Node bridge is not installed, the app falls
# back to mock data.  GRID is only ever used when explicitly requested.

CS2_PROVIDER: str = os.environ.get("CS2_PROVIDER", "hltv")

# ---------------------------------------------------------------------------
# GRID Open Access (future placeholder — not the active provider)
# ---------------------------------------------------------------------------
# GRID is preserved as an optional future provider.  The current Open Access
# tier does not include Central Data GraphQL access, so CS2_PROVIDER defaults
# to "hltv".  If that changes, set CS2_PROVIDER=grid + GRID_API_KEY to switch.

# API key — obtain from your GRID account (Account Management > Users > API Keys).
# Pass as GRID_API_KEY=... in .env or as an environment variable.
# Do not commit this value to version control.
def grid_api_key() -> str | None:
    """Read GRID_API_KEY from env or Streamlit secrets."""
    k = os.environ.get("GRID_API_KEY", "").strip()
    if k:
        return k
    try:
        import streamlit as st
        section = st.secrets.get("grid") if hasattr(st, "secrets") else None
        if section and section.get("api_key"):
            return str(section["api_key"]).strip()
    except Exception:
        pass
    return None


GRID_BASE_URL: str = os.environ.get("GRID_BASE_URL", "https://api.grid.gg")

# CS2 / CS:GO title ID on the GRID platform.  Configurable in case GRID
# renumbers titles after the CS2 migration.  4 is the current CS:GO/CS2 ID.
GRID_CS2_TITLE_ID: int = int(os.environ.get("GRID_CS2_TITLE_ID", "4"))

# Seconds between consecutive outbound GRID requests (GRID is a proper API,
# not a scrape — 1 s is conservative but polite).
GRID_THROTTLE_SECONDS: float = float(os.environ.get("GRID_THROTTLE_SECONDS", "1"))

# Per-request HTTP timeout for GRID calls.
GRID_TIMEOUT_SECONDS: int = int(os.environ.get("GRID_TIMEOUT_SECONDS", "15"))

# Max retries per GRID call (0 = one attempt only).
GRID_MAX_RETRIES: int = int(os.environ.get("GRID_MAX_RETRIES", "1"))

# ---------------------------------------------------------------------------
# HLTV enrichment (legacy flag — only meaningful when CS2_PROVIDER=grid)
# ---------------------------------------------------------------------------
#
# With CS2_PROVIDER=hltv (the current default), HLTV is already the primary
# source and this flag is ignored.  It is retained so that, if we later
# switch back to GRID, HLTV enrichment can be toggled without code changes.

CS2_ENABLE_HLTV_ENRICHMENT: bool = (
    os.environ.get("CS2_ENABLE_HLTV_ENRICHMENT", "false").lower() == "true"
)

# ---------------------------------------------------------------------------
# Shared CS2 disk cache
# ---------------------------------------------------------------------------

# Disk cache directory for CS2 provider JSON responses (GRID + HLTV).
# Cache keys are namespaced with "grid:" or "hltv:" prefixes.
CS2_CACHE_DIR: str = os.environ.get(
    "CS2_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), "data", "cs2_cache"),
)

# When True, fall back to mock data if HLTV bridge is not installed or all
# calls are in cooldown and the disk cache is empty.
CS2_USE_MOCK_FALLBACK: bool = os.environ.get("CS2_USE_MOCK_FALLBACK", "false").lower() == "true"

# HLTV anti-ban knobs. These mirror the defaults in hltv_provider.py but are
# documented here so users can tune them via environment variables.
CS2_HLTV_THROTTLE_SECONDS: float = float(os.environ.get("CS2_HLTV_THROTTLE_SECONDS", "3"))
CS2_HLTV_MAX_RETRIES: int        = int(os.environ.get("CS2_HLTV_MAX_RETRIES", "1"))
CS2_HLTV_CB_THRESHOLD: int       = int(os.environ.get("CS2_HLTV_CB_THRESHOLD", "3"))
CS2_HLTV_CB_WINDOW_S: float      = float(os.environ.get("CS2_HLTV_CB_WINDOW_S", "120"))
CS2_HLTV_COOLDOWN_SECONDS: float = float(os.environ.get("CS2_HLTV_COOLDOWN_SECONDS", "300"))
CS2_HLTV_NODE_TIMEOUT: int       = int(os.environ.get("CS2_HLTV_NODE_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# PandaScore (optional supplement — schedule / context data only)
# ---------------------------------------------------------------------------

# PandaScore base URL. Public REST API.
PANDASCORE_BASE_URL = os.environ.get("PANDASCORE_BASE_URL",
                                       "https://api.pandascore.co")

# API path segment for the CS2 game. PandaScore currently uses "csgo".
CS2_API_GAME_SLUG = os.environ.get("CS2_API_GAME_SLUG", "csgo")


def pandascore_token() -> str | None:
    """Read the PandaScore API token from environment or Streamlit secrets.

    Resolution order:
      1. ``PANDASCORE_TOKEN`` env var (best for local dev / .env files)
      2. ``st.secrets["pandascore"]["token"]`` (Streamlit-native)
      3. None — PandaScore is disabled; HLTV covers all required data.
    """
    env_tok = os.environ.get("PANDASCORE_TOKEN")
    if env_tok:
        return env_tok.strip()
    try:  # Streamlit is optional when the project is imported from a script.
        import streamlit as st
        section = st.secrets.get("pandascore") if hasattr(st, "secrets") else None
        if section and section.get("token"):
            return str(section["token"]).strip()
    except Exception:
        pass
    return None


# Per-request timeout. PandaScore responses are small JSON; 15s is generous.
PANDASCORE_TIMEOUT = int(os.environ.get("PANDASCORE_TIMEOUT", 15))
PANDASCORE_MAX_RETRIES = int(os.environ.get("PANDASCORE_MAX_RETRIES", 1))
PANDASCORE_RETRY_BACKOFF = float(os.environ.get("PANDASCORE_RETRY_BACKOFF", 1.5))
PANDASCORE_MIN_CALL_INTERVAL_S = float(os.environ.get("PANDASCORE_MIN_CALL_INTERVAL_S", 0.25))
PANDASCORE_FAILURE_COOLDOWN_S = int(os.environ.get("PANDASCORE_FAILURE_COOLDOWN_S", 90))
PANDASCORE_CB_THRESHOLD = int(os.environ.get("PANDASCORE_CB_THRESHOLD", 3))
PANDASCORE_CB_WINDOW_S = int(os.environ.get("PANDASCORE_CB_WINDOW_S", 60))
PANDASCORE_CB_RESET_S = int(os.environ.get("PANDASCORE_CB_RESET_S", 120))

# Legacy cache TTL constants (retained for compatibility with pandascore_client).
CACHE_TTL_CS2_MATCH_LIST = int(os.environ.get("CACHE_TTL_CS2_MATCH_LIST", 10 * 60))
CACHE_TTL_CS2_MATCH_DETAIL = int(os.environ.get("CACHE_TTL_CS2_MATCH_DETAIL", 30 * 24 * 60 * 60))
CACHE_TTL_CS2_TEAM = int(os.environ.get("CACHE_TTL_CS2_TEAM", 24 * 60 * 60))
CACHE_TTL_CS2_PLAYER = int(os.environ.get("CACHE_TTL_CS2_PLAYER", 24 * 60 * 60))
CACHE_TTL_CS2_TEAM_MATCHES = int(os.environ.get("CACHE_TTL_CS2_TEAM_MATCHES", 30 * 60))
CACHE_TTL_CS2_PLAYER_MATCHES = int(os.environ.get("CACHE_TTL_CS2_PLAYER_MATCHES", 30 * 60))

# Default recent-match sample size for CS2 team/player research.
CS2_RECENT_N: int = 10
