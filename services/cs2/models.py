"""Internal CS2 data models.

All external providers (GRID, HLTV, mock) normalize their responses into
these dataclasses before passing data to the service layer or UI.  This
isolates the services and UI from provider-specific JSON shapes.

Every field is optional (None) so partial data from cache or providers that
do not supply every field is still valid.  Callers check for None rather than
assuming presence.

The ``_source`` field on each model records the originating provider:
    "grid"   — GRID Open Access (official)
    "hltv"   — HLTV via gigobyte/hltv Node.js bridge (enrichment/fallback)
    "mock"   — static demo data (development / offline)
    "mixed"  — fields from more than one provider merged together
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


@dataclass
class CS2Tournament:
    """A tournament / event hosting CS2 series."""
    id: str | int | None = None
    name: str | None = None
    start_date: str | None = None        # ISO-8601 date string
    end_date: str | None = None
    prize_pool: str | None = None        # raw string from GRID (e.g. "$1,000,000")
    city: str | None = None
    country: str | None = None
    _stale: bool = False
    _source: str = "unknown"


@dataclass
class CS2Team:
    id: int | None = None
    name: str | None = None
    abbreviation: str | None = None       # short tag / acronym
    logo_url: str | None = None
    country: str | None = None
    ranking: int | None = None            # HLTV world ranking if available
    players: list["CS2Player"] = field(default_factory=list)
    _stale: bool = False                  # True when served from stale cache
    _source: str = "unknown"              # "grid" | "hltv" | "mock"


@dataclass
class CS2Player:
    id: int | None = None
    ign: str | None = None               # in-game name
    name: str | None = None              # real name (may be None)
    nationality: str | None = None
    age: int | None = None
    role: str | None = None              # "rifler" | "AWPer" | "IGL" | …
    team: CS2Team | None = None
    image_url: str | None = None
    _stale: bool = False
    _source: str = "unknown"


# ---------------------------------------------------------------------------
# Match / result
# ---------------------------------------------------------------------------


@dataclass
class CS2MapResult:
    """One map played inside a series."""
    map_name: str | None = None          # "Mirage", "Inferno", …
    score_t1: int | None = None
    score_t2: int | None = None
    winner_name: str | None = None
    stats_id: int | None = None          # HLTV mapStatsId for deep stats


@dataclass
class CS2MatchResult:
    """A finished or upcoming CS2 series."""
    id: int | None = None
    date_unix_ms: int | None = None      # epoch milliseconds
    team1: CS2Team | None = None
    team2: CS2Team | None = None
    score_t1: int | None = None          # series maps won
    score_t2: int | None = None
    format: str | None = None            # "bo1" / "bo3" / "bo5"
    event_name: str | None = None
    event_id: int | None = None
    stars: int = 0                       # HLTV star rating (0–5)
    is_live: bool = False
    maps: list[CS2MapResult] = field(default_factory=list)
    _stale: bool = False
    _source: str = "unknown"

    @property
    def winner(self) -> CS2Team | None:
        """Return the winning team based on map scores (None if tied/TBD)."""
        if self.score_t1 is None or self.score_t2 is None:
            return None
        if self.score_t1 > self.score_t2:
            return self.team1
        if self.score_t2 > self.score_t1:
            return self.team2
        return None


# ---------------------------------------------------------------------------
# Player statistics
# ---------------------------------------------------------------------------


@dataclass
class CS2PlayerMapEntry:
    """Stats for one map in a player's match history."""
    date_unix_ms: int | None = None
    map_name: str | None = None
    team1_name: str | None = None
    team2_name: str | None = None
    kills: int | None = None
    deaths: int | None = None
    rating: float | None = None
    map_stats_id: int | None = None      # HLTV mapStatsId


@dataclass
class CS2PlayerStats:
    """Aggregate and per-map performance from HLTV getPlayerStats."""
    player_id: int
    ign: str | None = None
    # Overview stats
    total_kills: int | None = None
    total_deaths: int | None = None
    maps_played: int | None = None
    rounds_played: int | None = None
    kd_ratio: float | None = None
    kills_per_round: float | None = None
    deaths_per_round: float | None = None
    hs_pct: float | None = None          # headshot percentage 0-100
    adr: float | None = None             # average damage per round
    rating: float | None = None          # HLTV rating 2.0 (or 1.0)
    # Opening duels
    opening_kills: int | None = None
    opening_deaths: int | None = None
    opening_kill_ratio: float | None = None
    # Per-map history
    recent_maps: list[CS2PlayerMapEntry] = field(default_factory=list)
    _stale: bool = False
    _source: str = "unknown"


# ---------------------------------------------------------------------------
# Team statistics
# ---------------------------------------------------------------------------


@dataclass
class CS2TeamMapStat:
    """Per-map aggregate for a team."""
    map_name: str
    wins: int = 0
    losses: int = 0
    total_rounds_won: float | None = None
    total_rounds_lost: float | None = None
    win_rate: float | None = None        # 0.0–1.0

    def compute_win_rate(self) -> None:
        total = self.wins + self.losses
        self.win_rate = self.wins / total if total > 0 else None


@dataclass
class CS2TeamStats:
    """Aggregate stats for a team from HLTV getTeamStats."""
    team_id: int
    team_name: str | None = None
    maps_played: int | None = None
    wins: int | None = None
    losses: int | None = None
    win_rate: float | None = None
    kills_per_round: float | None = None
    deaths_per_round: float | None = None
    rounds_per_map_won: float | None = None
    rounds_per_map_lost: float | None = None
    map_stats: list[CS2TeamMapStat] = field(default_factory=list)
    _stale: bool = False
    _source: str = "unknown"


# ---------------------------------------------------------------------------
# Source status (used by UI status panel)
# ---------------------------------------------------------------------------


@dataclass
class CS2SourceStatus:
    """Real-time status of the CS2 data provider stack."""
    # Active primary provider: "hltv" (default) | "mock" | "grid" (experimental)
    provider: str = "hltv"
    mode: str = "unknown"               # "live" | "cached" | "stale" | "cooldown" | "offline"

    # GRID fields
    grid_configured: bool = False        # True when GRID_API_KEY is set
    grid_last_success_ts: float | None = None
    grid_permission_denied: bool = False  # True once Central Data GraphQL returned PERMISSION_DENIED

    # HLTV fields (fallback / enrichment)
    hltv_enabled: bool = False           # True when HLTV enrichment is on
    hltv_bridge_installed: bool = False
    hltv_cb_open: bool = False
    hltv_cb_open_until: float | None = None
    hltv_last_success_ts: float | None = None

    # Shared
    last_success_ts: float | None = None # most recent success across all providers
    throttle_s: float = 1.0
    cache_dir: str = ""

    @property
    def mode_label(self) -> str:
        labels = {
            "live":     "Live",
            "cached":   "Cached",
            "stale":    "Stale cache",
            "cooldown": "Cooldown — requests paused",
            "offline":  "Offline — no data",
            "unknown":  "Unknown",
        }
        return labels.get(self.mode, self.mode)

    @property
    def mode_emoji(self) -> str:
        emojis = {
            "live":     "🟢",
            "cached":   "🟡",
            "stale":    "🟠",
            "cooldown": "🔴",
            "offline":  "⚫",
            "unknown":  "⚪",
        }
        return emojis.get(self.mode, "⚪")

    @property
    def provider_label(self) -> str:
        labels = {
            "grid": "GRID Open Access",
            "hltv": "HLTV (gigobyte/hltv)",
            "mock": "Demo data (mock)",
        }
        return labels.get(self.provider, self.provider)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def as_dict(obj: Any) -> dict:
    """Recursively convert dataclass instances to plain dicts (for JSON serialization)."""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: as_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [as_dict(i) for i in obj]
    return obj
