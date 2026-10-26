"""CS2 provider capability registry.

Single source of truth for what each data source can deliver.

Architecture
------------
Capabilities are split into three tiers, picked at runtime based on the
*resolved* provider (see ``cs2_data_provider._active_provider``):

1. **HLTV capabilities** — what the gigobyte/hltv Node bridge delivers.
   This is the primary/default tier today.
2. **GRID capabilities** — what GRID Open Access *would* deliver if the
   tier provided Central Data GraphQL access.  Only consulted when
   ``CS2_PROVIDER=grid`` is explicitly set.
3. **HLTV enrichment (legacy)** — only meaningful when the primary is
   GRID.  Kept so we can flip back to a GRID+HLTV split later without
   code churn.

Usage::

    from services.cs2.cs2_capabilities import supported, notice, source_for

    if supported("adr"):
        _render_adr(sample)
    else:
        cs2_capability_notice("adr")

    st.caption(f"Source: {source_for('match_results')}")
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# HLTV capabilities (gigobyte/hltv bridge, primary today)
# ---------------------------------------------------------------------------

_HLTV: dict[str, bool] = {
    # ── Schedule / fixtures ────────────────────────────────────────────────
    "match_schedule":          True,   # getMatches()
    "match_results":           True,   # getResults()
    "match_detail":            True,   # getMatch(id)

    # ── Series / map results ───────────────────────────────────────────────
    "map_names_in_series":     True,
    "map_winner_in_series":    True,

    # ── Team research ──────────────────────────────────────────────────────
    "team_profile":            True,   # getTeam / getTeamByName
    "team_roster":             True,
    "team_ranking":            True,   # getTeamRanking
    "team_form":               True,   # derived from recent results
    "map_pool_win_rate":       True,   # getTeamStats per-map win rates
    "h2h_results":             True,   # derived locally

    # ── Player research ────────────────────────────────────────────────────
    "player_profile":          True,   # getPlayerByName / getPlayer
    "map_level_player_stats":  True,   # getPlayerStats per-map history
    "player_kills_projection": True,
    "player_deaths":           True,
    "player_headshots_projection": True,
    "player_damage":           True,

    # ── Advanced ───────────────────────────────────────────────────────────
    "adr":                     True,   # overviewStatistics.damagePerRound
    "opening_duels":           True,   # individualStatistics.opening*
    "kast":                    False,  # not exposed by gigobyte/hltv schema
    "side_splits":             False,  # needs per-round data
    "round_totals":            False,
    "map_total_rounds_projection": False,
    "round_handicap":          False,
    "pistol_rounds":           False,

    # ── Derived / rule-based projections ───────────────────────────────────
    "match_winner_lean":       True,
    "total_maps_lean":         True,

    # ── GRID-only features ─────────────────────────────────────────────────
    "tournament_list":         False,
}

# ---------------------------------------------------------------------------
# GRID capabilities (only used when CS2_PROVIDER=grid)
# ---------------------------------------------------------------------------
# Conservative defaults — what GRID Central Data *would* expose if the
# account tier included GraphQL access.  Leave these as-is to preserve
# the future-switch option.
# ---------------------------------------------------------------------------

_GRID: dict[str, bool] = {
    "match_schedule":          True,
    "match_results":           True,
    "match_detail":            True,
    "tournament_list":         True,
    "map_names_in_series":     True,
    "map_winner_in_series":    True,
    "team_profile":            True,
    "team_roster":             True,
    "h2h_results":             True,
    "player_profile":          True,
    "map_level_player_stats":  True,
    "player_kills_projection": True,
    "player_deaths":           True,
    "player_headshots_projection": True,
    "player_damage":           True,
    "adr":                     False,
    "kast":                    False,
    "opening_duels":           False,
    "side_splits":             False,
    "round_totals":            False,
    "map_total_rounds_projection": False,
    "round_handicap":          False,
    "pistol_rounds":           False,
    "match_winner_lean":       True,
    "total_maps_lean":         True,
    "team_form":               True,
    "map_pool_win_rate":       False,
    "team_ranking":            False,
}

# ---------------------------------------------------------------------------
# HLTV enrichment (legacy — active only when CS2_PROVIDER=grid
# AND CS2_ENABLE_HLTV_ENRICHMENT=true AND the bridge is installed)
# ---------------------------------------------------------------------------

_HLTV_ENRICHMENT: dict[str, bool] = {
    "adr":                     True,
    "opening_duels":           True,
    "team_ranking":            True,
    "map_pool_win_rate":       True,
    "player_kills_projection": True,
    "player_headshots_projection": True,
    "map_level_player_stats":  True,
    "kast":                    False,
    "side_splits":             False,
    "round_totals":            False,
    "map_total_rounds_projection": False,
    "round_handicap":          False,
    "pistol_rounds":           False,
}

# ---------------------------------------------------------------------------
# Human-readable notices for unsupported capabilities
# ---------------------------------------------------------------------------

_NOTICES: dict[str, str] = {
    "adr": (
        "Average Damage per Round (ADR) is loaded from HLTV's overview "
        "statistics.  If you see this notice, HLTV is unreachable — "
        "retry once the circuit breaker resets."
    ),
    "kast": (
        "KAST% (Kill/Assist/Survive/Trade) is not available from the "
        "HLTV gigobyte/hltv schema."
    ),
    "side_splits": (
        "T-side / CT-side win splits require per-round data not exposed "
        "by the current data sources."
    ),
    "round_totals": (
        "Per-map total-rounds data is not exposed by the current provider."
    ),
    "map_total_rounds_projection": (
        "Map Total Rounds projection requires per-game round data, "
        "not available from the current provider."
    ),
    "round_handicap": (
        "Round Handicap requires per-map round counts, "
        "unavailable from the current provider."
    ),
    "pistol_rounds": (
        "Pistol-round win rates require dedicated per-round data "
        "not returned by the current provider."
    ),
    "opening_duels": (
        "Opening-duel statistics are loaded from HLTV's individual "
        "statistics.  Unavailable while HLTV is in cooldown."
    ),
    "team_ranking": (
        "HLTV world ranking — unavailable while HLTV is in cooldown."
    ),
    "map_pool_win_rate": (
        "Per-map win rate aggregates come from HLTV's team-stats page. "
        "Unavailable while HLTV is in cooldown."
    ),
    "tournament_list": (
        "Tournament listings are only available via GRID Open Access "
        "(experimental).  Set CS2_PROVIDER=grid to enable."
    ),
}

_DEFAULT_NOTICE = (
    "This feature is not available from the current data source."
)

# ---------------------------------------------------------------------------
# Runtime provider selection (lazy — avoids circular import)
# ---------------------------------------------------------------------------


def _resolved_provider() -> str:
    """Return the currently-active provider: 'hltv' | 'grid' | 'mock'."""
    try:
        from services.cs2.cs2_data_provider import _active_provider  # noqa: PLC0415
        return _active_provider()
    except Exception:
        # Safe fallback when imports are mid-initialisation.
        import os  # noqa: PLC0415
        return os.environ.get("CS2_PROVIDER", "hltv").lower()


def _hltv_enrichment_active() -> bool:
    """True if HLTV enrichment is enabled AND the bridge is installed.

    Only meaningful when the primary provider is GRID.  When HLTV is
    already primary, enrichment is irrelevant.
    """
    try:
        from config import CS2_ENABLE_HLTV_ENRICHMENT  # noqa: PLC0415
        if not CS2_ENABLE_HLTV_ENRICHMENT:
            return False
        from services.cs2.hltv_provider import bridge_installed  # noqa: PLC0415
        return bridge_installed()
    except Exception:
        return False


def _caps_for_active_provider() -> dict[str, bool]:
    """Return the capability dict for the currently-resolved provider."""
    prov = _resolved_provider()
    if prov == "grid":
        return _GRID
    # mock acts like HLTV for feature gating: the UI will still be
    # coherent, and normalisers return demo data.
    return _HLTV


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def supported(key: str) -> bool:
    """Return True if the named capability is available on the active stack."""
    if _caps_for_active_provider().get(key, False):
        return True
    # GRID + HLTV-enrichment legacy path.
    if _resolved_provider() == "grid" and _hltv_enrichment_active():
        if _HLTV_ENRICHMENT.get(key, False):
            return True
    return False


def notice(key: str) -> str:
    """Return a human-readable explanation for an unsupported capability."""
    return _NOTICES.get(key, _DEFAULT_NOTICE)


def source_for(key: str) -> str:
    """Return the provider label for a capability (e.g. 'HLTV', 'GRID')."""
    prov = _resolved_provider()
    if prov == "hltv" and _HLTV.get(key, False):
        return "HLTV"
    if prov == "grid":
        if _GRID.get(key, False):
            return "GRID"
        if _hltv_enrichment_active() and _HLTV_ENRICHMENT.get(key, False):
            return "HLTV enrichment"
    if prov == "mock":
        return "demo"
    return "unavailable"


def all_supported() -> list[str]:
    keys = set(_HLTV) | set(_GRID) | set(_HLTV_ENRICHMENT)
    return [k for k in keys if supported(k)]


def all_unsupported() -> list[str]:
    keys = set(_HLTV) | set(_GRID) | set(_HLTV_ENRICHMENT)
    return [k for k in keys if not supported(k)]
