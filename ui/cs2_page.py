"""CS2 research dashboard — top-level tab.

Primary data source: HLTV via the gigobyte/hltv Node.js bridge.
Fallback: demo / mock data.
Future placeholder: GRID Open Access (only consulted when
``CS2_PROVIDER=grid`` is set explicitly — see ``cs2_data_provider``).

Cache: aggressive disk cache in ``data/cs2_cache/`` (``hltv:`` and
``grid:`` namespaces share the same directory).

Anti-spam rules enforced here
------------------------------
1. st.session_state is the primary guard.  Expensive data is fetched once
   per session and stored; Streamlit rerenders never trigger re-fetches.
2. Overview auto-loads ONE endpoint only: recent results (6-h cache).
   Everything else is behind an explicit user button.
3. Upcoming matches, player stats, team stats, tournaments are all lazy —
   only fetched when the user clicks a button.
4. No batch-scraping, no auto-refresh loops, no pagination by default.
5. All errors degrade gracefully: stale cache → clean notice → empty state.
   Never NaN, never broken widget, never silent empty.
"""

from __future__ import annotations

import logging
import math
import time

import streamlit as st

from services.cs2 import cs2_data_provider as dp
from services.cs2 import cs2_cache_service as _cache_svc
from services.cs2.cs2_match_service import (
    get_recent_results,
    get_upcoming,
    get_match_detail,
    format_match_label,
    format_match_date,
    build_team_form,
)
from services.cs2.cs2_player_service import (
    find_player,
    load_player_stats,
    build_player_summary,
    build_player_sample_dict,
)
from services.cs2.cs2_team_service import (
    find_team,
    load_team_stats,
    load_team_results,
    form_string,
    win_rate,
)
from services.cs2.cs2_map_service import (
    player_avg_kills_by_map,
    player_avg_rating_by_map,
    map_matchup_summary,
)
from services.cs2.cs2_projection_service import (
    project_match_winner,
    project_player_kills,
    project_player_headshots,
)
from services.cs2.constants import CS2_ACTIVE_MAPS
from ui.cs2_components import (
    cs2_source_status_panel,
    cs2_stale_banner,
    cs2_cooldown_banner,
    cs2_mock_banner,
    cs2_grid_no_key_banner,
    cs2_source_badge,
    cs2_match_result_card,
    cs2_player_stat_cards,
    cs2_player_map_history_table,
    cs2_team_roster_card,
    cs2_team_map_pool_table,
    cs2_tournament_table,
    cs2_section_header,
    cs2_no_data,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session-state keys (prevents re-fetching on every Streamlit rerender)
# ---------------------------------------------------------------------------
_SK_RESULTS     = "cs2_results"       # list[CS2MatchResult] — recent results
_SK_UPCOMING    = "cs2_upcoming"      # list[CS2MatchResult] — upcoming
_SK_PLAYER      = "cs2_player"        # CS2Player
_SK_P_STATS     = "cs2_p_stats"       # CS2PlayerStats
_SK_TEAM        = "cs2_team"          # CS2Team
_SK_T_STATS     = "cs2_t_stats"       # CS2TeamStats
_SK_T_RESULTS   = "cs2_t_results"     # list[CS2MatchResult]
_SK_TOURNAMENTS = "cs2_tournaments"   # list[CS2Tournament]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_cs2_page() -> None:
    _init_cache()
    _render_sidebar()

    st.markdown("## CS2 Research Dashboard")
    _render_provider_header_caption()

    tab_overview, tab_matches, tab_players, tab_teams, tab_maps, tab_tournaments = st.tabs(
        ["Overview", "Matches", "Players", "Teams", "Maps", "Tournaments"]
    )

    with tab_overview:
        _render_overview()
    with tab_matches:
        _render_matches()
    with tab_players:
        _render_players()
    with tab_teams:
        _render_teams()
    with tab_maps:
        _render_maps()
    with tab_tournaments:
        _render_tournaments()


def _render_provider_header_caption() -> None:
    """Show a one-line source attribution beneath the page header.

    Uses the *resolved* provider (from ``source_status``) rather than the
    raw env-var so the caption always matches what the app actually uses.
    """
    status = dp.source_status()
    provider = status.provider          # resolved at runtime

    if provider == "hltv":
        if status.hltv_bridge_installed:
            st.caption(
                "Source: **HLTV** (gigobyte/hltv). Cache-first; stats load on demand."
            )
        else:
            st.warning(
                "HLTV bridge not installed. Run `cd services/cs2/hltv_bridge && "
                "npm install` to enable live data. Using demo data in the meantime.",
                icon="⚠️",
            )
    elif provider == "grid":
        if status.grid_configured:
            st.caption(
                "Source: **GRID Open Access** (experimental). "
                "Detail and stats load on demand."
            )
        else:
            st.caption(
                "Configured for GRID but **GRID_API_KEY not set** — "
                "add it to `.env` or switch `CS2_PROVIDER=hltv`."
            )
    else:
        st.caption(
            "Source: **demo data** — install the HLTV bridge "
            "(`cd services/cs2/hltv_bridge && npm install`) for live data."
        )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def _init_cache() -> None:
    import os
    try:
        from config import CS2_CACHE_DIR
    except ImportError:
        CS2_CACHE_DIR = os.path.join(
            os.path.dirname(__file__), "..", "data", "cs2_cache"
        )
    _cache_svc.init(CS2_CACHE_DIR)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("---")
        st.markdown("### CS2 Controls")

        status = dp.source_status()
        cs2_source_status_panel(status)

        if st.button("↺ Refresh CS2 data", key="cs2_refresh"):
            _clear_session()
            dp.reset_provider()
            st.cache_data.clear()
            st.success("Cache cleared. Data will re-fetch on next action.")
            st.rerun()

        if status.hltv_cb_open and status.hltv_cb_open_until:
            remaining = max(0, status.hltv_cb_open_until - time.time())
            cs2_cooldown_banner(remaining)

        if status.provider == "mock":
            cs2_mock_banner()

        # GRID audit tool is only meaningful when GRID is actively selected
        # (experimental path).  With HLTV primary it would just be noise.
        import os as _os
        if (
            _os.environ.get("CS2_PROVIDER", "hltv").lower() == "grid"
            and status.grid_configured
        ):
            _render_grid_audit_panel()

        st.markdown("---")


def _clear_session() -> None:
    for key in (_SK_RESULTS, _SK_UPCOMING, _SK_PLAYER, _SK_P_STATS,
                _SK_TEAM, _SK_T_STATS, _SK_T_RESULTS, _SK_TOURNAMENTS):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _is_stale(items: list) -> bool:
    return any(getattr(r, "_stale", False) for r in items if r is not None)


def _is_mock(items: list) -> bool:
    return any(getattr(r, "_source", "") == "mock" for r in items if r is not None)


def _handle_exc(exc: Exception, label: str = "data") -> None:
    from services.cs2.grid_provider import GRIDAuthError, GRIDNotConfigured
    from services.cs2.hltv_provider import HLTVCloudflare, HLTVCooldown, HLTVNotReady

    if isinstance(exc, GRIDNotConfigured):
        cs2_grid_no_key_banner()
    elif isinstance(exc, GRIDAuthError):
        st.error(
            f"GRID API key rejected while loading {label}. "
            "Check that GRID_API_KEY is valid and has CS2 Open Access.",
            icon="🔑",
        )
    elif isinstance(exc, HLTVCloudflare):
        st.warning(
            f"HLTV blocked the {label} request (Cloudflare). "
            "Stale cache shown where available. Works better from a home IP.",
            icon="🛡️",
        )
    elif isinstance(exc, HLTVCooldown):
        cs2_cooldown_banner()
    elif isinstance(exc, HLTVNotReady):
        st.error(str(exc))
    else:
        st.warning(f"Could not load {label}: {exc}", icon="⚠️")


# ---------------------------------------------------------------------------
# Tab: Overview
# Loads ONE endpoint automatically: getResults (6-h disk cache).
# Everything else is a button.
# ---------------------------------------------------------------------------


def _render_overview() -> None:
    status = dp.source_status()
    provider = status.provider
    cs2_section_header(
        "Overview",
        f"Recent CS2 results — source: {status.provider_label}. Cached 6 h.",
    )

    # ── Recent results — auto-loaded once, stored in session_state ──────────
    if _SK_RESULTS not in st.session_state:
        try:
            st.session_state[_SK_RESULTS] = get_recent_results(limit=20)
        except Exception as exc:
            _handle_exc(exc, "recent results")
            st.session_state[_SK_RESULTS] = []

    results = st.session_state.get(_SK_RESULTS, [])

    if results:
        if _is_stale(results):
            cs2_stale_banner("match results")
        if _is_mock(results):
            cs2_mock_banner()
        for r in results:
            cs2_match_result_card(r, compact=True)
        _source = results[0]._source if results else "hltv"
        cs2_source_badge(_source)
    else:
        cs2_no_data(
            "No recent results cached yet. "
            "Click **↺ Refresh CS2 data** in the sidebar to fetch."
        )

    # ── Upcoming — lazy, behind a button ────────────────────────────────────
    st.markdown("---")

    if _SK_UPCOMING in st.session_state:
        _show_upcoming(st.session_state[_SK_UPCOMING])
    else:
        if st.button("Load upcoming matches", key="cs2_load_upcoming"):
            with st.spinner("Fetching upcoming matches…"):
                try:
                    upcoming = get_upcoming()
                    st.session_state[_SK_UPCOMING] = upcoming
                except Exception as exc:
                    _handle_exc(exc, "upcoming matches")
                    st.session_state[_SK_UPCOMING] = []
            st.rerun()
        else:
            st.caption("Upcoming matches load on demand.")


def _show_upcoming(upcoming: list) -> None:
    st.markdown("#### Upcoming / Live Matches")
    if not upcoming:
        cs2_no_data(
            "No upcoming matches found. The current data provider returned an empty list. "
            "This is normal when no matches are currently scheduled."
        )
        return
    for r in upcoming[:10]:
        t1 = (r.team1.name if r.team1 else None) or "TBD"
        t2 = (r.team2.name if r.team2 else None) or "TBD"
        live_tag = " 🔴 LIVE" if r.is_live else ""
        event = r.event_name or ""
        fmt = r.format or ""
        date_str = format_match_date(r)
        st.markdown(
            f"<small style='color:#aaa'>{date_str}</small>{live_tag} &nbsp; "
            f"**{t1}** vs **{t2}** "
            f"<small style='color:#888'>({fmt}) {event}</small>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Tab: Matches
# Reuses session_state results loaded in Overview — zero extra requests.
# Match detail loads on demand per button.
# ---------------------------------------------------------------------------


def _render_matches() -> None:
    cs2_section_header("Matches", "Browse recent results. Detail loads on demand.")

    # Reuse results already loaded in Overview — no extra API calls.
    results = st.session_state.get(_SK_RESULTS)

    if results is None:
        st.info(
            "Open the **Overview** tab first to load recent results, "
            "or click **↺ Refresh CS2 data** in the sidebar.",
            icon="ℹ️",
        )
        return

    if not results:
        cs2_no_data("No results available. Use the sidebar to refresh.")
        return

    # Local filters applied to session_state data — zero new API calls.
    stars_filter = st.selectbox(
        "Filter by star rating",
        [0, 1, 2, 3, 4, 5],
        format_func=lambda x: "All matches" if x == 0 else f"{x}+ stars",
        key="cs2_matches_stars",
    )
    filtered = [r for r in results if getattr(r, "stars", 0) >= stars_filter]

    if _is_stale(filtered):
        cs2_stale_banner("match results")
    if _is_mock(filtered):
        cs2_mock_banner()

    st.caption(f"Showing {len(filtered)} results (session cache — no new requests).")

    for r in filtered:
        label = format_match_label(r)
        with st.expander(f"{format_match_date(r)}  {label}", expanded=False):
            _render_match_detail_inline(r)


def _render_match_detail_inline(r) -> None:
    t1 = (r.team1.name if r.team1 else None) or "TBD"
    t2 = (r.team2.name if r.team2 else None) or "TBD"
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"**{t1}**  {r.score_t1 if r.score_t1 is not None else '?'}")
    with col2:
        st.markdown(f"**{t2}**  {r.score_t2 if r.score_t2 is not None else '?'}")
    with col3:
        st.caption(f"{r.format or ''}  {r.event_name or ''}")

    if r.maps:
        st.markdown("**Per-map:**")
        for m in r.maps:
            mn = m.map_name or "?"
            ms1 = m.score_t1 if m.score_t1 is not None else "?"
            ms2 = m.score_t2 if m.score_t2 is not None else "?"
            st.markdown(f"  • {mn}: {ms1}–{ms2}")
    elif r.id:
        key = f"cs2_detail_{r.id}"
        if st.button("Load map detail", key=key):
            with st.spinner("Fetching match detail…"):
                try:
                    detail = get_match_detail(r.id)
                except Exception as exc:
                    _handle_exc(exc, "match detail")
                    detail = None
            if detail and detail.maps:
                for m in detail.maps:
                    mn = m.map_name or "?"
                    ms1 = m.score_t1 if m.score_t1 is not None else "?"
                    ms2 = m.score_t2 if m.score_t2 is not None else "?"
                    st.markdown(f"  • {mn}: {ms1}–{ms2}")
            else:
                cs2_no_data("Map detail not available for this match.")


# ---------------------------------------------------------------------------
# Tab: Players
# ---------------------------------------------------------------------------


def _render_players() -> None:
    status = dp.source_status()
    provider = status.provider

    cs2_section_header("Player Research", "Search by in-game name (IGN)")

    # GRID (experimental) path: Central Data has no free-text player search.
    # Point the user back to HLTV — the default — instead of dead-ending here.
    if provider == "grid" and not status.hltv_enabled:
        st.info(
            "**Player search is not available on the experimental GRID path.**  \n"
            "Switch back to the HLTV primary by setting `CS2_PROVIDER=hltv` "
            "in `.env`, or enable `CS2_ENABLE_HLTV_ENRICHMENT=true` alongside GRID.",
            icon="ℹ️",
        )
        return

    src_label = "HLTV" if provider in ("hltv",) or status.hltv_enabled else "data provider"
    st.caption(f"Search cached 24 h via {src_label}. Stats load on demand.")

    query = st.text_input(
        "Player IGN", placeholder="e.g. s1mple, NiKo, ZywOo",
        key="cs2_player_q",
    )

    if not query:
        cs2_no_data("Enter a player name above to begin.")
        return

    if st.button("Search player", key="cs2_player_search"):
        with st.spinner(f"Looking up {query!r}…"):
            try:
                player = find_player(query.strip())
            except Exception as exc:
                _handle_exc(exc, "player search")
                player = None
        if player:
            st.session_state[_SK_PLAYER] = player
            st.session_state.pop(_SK_P_STATS, None)
        else:
            st.warning(f"Player {query!r} not found on {src_label}.")
            st.session_state.pop(_SK_PLAYER, None)

    player = st.session_state.get(_SK_PLAYER)
    if not player:
        return

    # Profile.
    col1, col2 = st.columns([2, 3])
    with col1:
        st.markdown(f"### {player.ign or player.name or '—'}")
        if player.name and player.name != player.ign:
            st.caption(player.name)
        if player.nationality:
            st.caption(f"🌍 {player.nationality}")
        if player.team:
            st.caption(f"Team: **{player.team.name or '—'}**")
        if player._stale:
            cs2_stale_banner("player profile")

    # Stats — lazy.
    stats = st.session_state.get(_SK_P_STATS)
    if stats is None:
        if player.id and st.button(
            "Load player stats (cached 24 h)",
            key="cs2_player_stats_btn",
        ):
            with st.spinner("Loading player stats…"):
                try:
                    stats = load_player_stats(player.id)
                except Exception as exc:
                    _handle_exc(exc, "player stats")
                    stats = None
            if stats:
                st.session_state[_SK_P_STATS] = stats
            else:
                st.warning(
                    "Player stats unavailable right now — HLTV may be in "
                    "cooldown or Cloudflare-blocked. Stale cache is used "
                    "when available; otherwise retry shortly."
                )

    if stats:
        _render_player_stats(stats)


def _render_player_stats(stats) -> None:
    summary = build_player_summary(stats)
    if stats._stale:
        cs2_stale_banner("player stats")
    src_label = {"hltv": "HLTV", "grid": "GRID", "mixed": "HLTV + GRID", "mock": "demo"}.get(
        getattr(stats, "_source", ""), "")
    header = f"#### Career Stats ({src_label})" if src_label else "#### Career Stats"
    st.markdown(header)
    cs2_player_stat_cards(summary)

    if stats.opening_kills is not None:
        st.markdown("#### Opening Duels")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Opening Kills", stats.opening_kills)
        with c2:
            st.metric("Opening Deaths", stats.opening_deaths or "—")
        with c3:
            r = stats.opening_kill_ratio
            st.metric("Open K/D", f"{r:.2f}" if r else "—")

    st.markdown("#### Recent Map History")
    cs2_player_map_history_table(stats.recent_maps)

    avg_by_map = player_avg_kills_by_map(stats)
    if avg_by_map:
        st.markdown("#### Avg Kills by Map")
        import pandas as pd
        rows = [{"Map": m, "Avg K": f"{v:.1f}"}
                for m, v in sorted(avg_by_map.items(), key=lambda x: -x[1])]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Line Research")
    _render_player_line_research(stats)


def _render_player_line_research(stats) -> None:
    sample = build_player_sample_dict(stats)
    market = st.selectbox(
        "Market",
        ["Player Kills (map)", "Player Headshots"],
        key="cs2_p_market",
    )
    map_sel = st.selectbox(
        "Map filter",
        ["All maps"] + list(CS2_ACTIVE_MAPS),
        key="cs2_p_map",
    )
    line = st.number_input("Manual line", value=18.5, step=0.5, key="cs2_p_line")
    map_arg = None if map_sel == "All maps" else map_sel

    if market == "Player Kills (map)":
        proj = project_player_kills(sample, manual_line=line, selected_map=map_arg)
    else:
        proj = project_player_headshots(sample, manual_line=line)
    _render_projection(proj)


# ---------------------------------------------------------------------------
# Tab: Teams
# ---------------------------------------------------------------------------


def _render_teams() -> None:
    status = dp.source_status()
    provider = status.provider

    cs2_section_header("Team Research", "Search by team name")

    # GRID (experimental) path: Central Data has no free-text team search.
    if provider == "grid" and not status.hltv_enabled:
        st.info(
            "**Team search is not available on the experimental GRID path.**  \n"
            "Recent match results (including team names) still appear in the "
            "**Overview** and **Matches** tabs.  \n"
            "For full team research, set `CS2_PROVIDER=hltv` in `.env` "
            "(HLTV is the default primary).",
            icon="ℹ️",
        )
        return

    src_label = "HLTV" if provider in ("hltv",) or status.hltv_enabled else "data provider"
    st.caption(f"Search cached 24 h via {src_label}. Results and stats load on demand.")

    query = st.text_input(
        "Team name", placeholder="e.g. Natus Vincere, FaZe, G2",
        key="cs2_team_q",
    )

    if not query:
        cs2_no_data("Enter a team name above to begin.")
        return

    if st.button("Search team", key="cs2_team_search"):
        with st.spinner(f"Looking up {query!r}…"):
            try:
                team = find_team(query.strip())
            except Exception as exc:
                _handle_exc(exc, "team search")
                team = None
        if team:
            st.session_state[_SK_TEAM] = team
            st.session_state.pop(_SK_T_STATS, None)
            st.session_state.pop(_SK_T_RESULTS, None)
        else:
            st.warning(f"Team {query!r} not found on {src_label}.")
            st.session_state.pop(_SK_TEAM, None)

    team = st.session_state.get(_SK_TEAM)
    if not team:
        return

    # Profile card.
    col1, col2 = st.columns([2, 3])
    with col1:
        st.markdown(f"### {team.name or '—'}")
        if team.country:
            st.caption(f"🌍 {team.country}")
        if team.ranking:
            st.caption(f"World rank: #{team.ranking}")
        if team._stale:
            cs2_stale_banner("team profile")
    with col2:
        st.markdown("**Roster:**")
        cs2_team_roster_card(team)

    # Recent results — lazy.
    t_results = st.session_state.get(_SK_T_RESULTS)
    if t_results is None:
        if team.id and st.button("Load recent results (1 request)", key="cs2_t_results_btn"):
            with st.spinner("Loading results…"):
                try:
                    t_results = load_team_results(team.id, limit=15)
                except Exception as exc:
                    _handle_exc(exc, "team results")
                    t_results = []
            st.session_state[_SK_T_RESULTS] = t_results
    if t_results:
        st.markdown(f"#### Recent Results — {team.name}")
        if _is_stale(t_results):
            cs2_stale_banner("team results")
        fs = form_string(t_results, team.id, team.name, n=5)
        wr = win_rate(t_results, team.id, team.name)
        c1, c2 = st.columns(2)
        with c1:
            st.metric("Form (last 5)", fs)
        with c2:
            st.metric("Win rate", f"{wr * 100:.0f}%" if wr is not None else "—")
        for r in t_results[:10]:
            cs2_match_result_card(r, compact=True)

    # Team stats — lazy (4-5 requests when using HLTV).
    t_stats = st.session_state.get(_SK_T_STATS)
    if t_stats is None:
        if team.id and st.button(
            "Load map-pool stats (cached 24 h)",
            key="cs2_t_stats_btn",
        ):
            with st.spinner("Loading team stats…"):
                try:
                    t_stats = load_team_stats(team.id)
                except Exception as exc:
                    _handle_exc(exc, "team stats")
                    t_stats = None
            if t_stats:
                st.session_state[_SK_T_STATS] = t_stats
            else:
                st.warning(
                    "Map-pool stats unavailable right now — HLTV may be in "
                    "cooldown or Cloudflare-blocked. Retry shortly."
                )

    if t_stats:
        st.markdown(f"#### Map Pool — {team.name}")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Maps played", t_stats.maps_played or "—")
        with c2:
            wr2 = t_stats.win_rate
            st.metric("Overall win%", f"{wr2:.1f}%" if wr2 else "—")
        with c3:
            kpr = t_stats.kills_per_round
            st.metric("Kills/round", f"{kpr:.2f}" if kpr else "—")
        cs2_team_map_pool_table(t_stats)
        if t_stats._stale:
            cs2_stale_banner("team stats")

    # Match winner research — uses already-loaded session results only.
    _render_matchup_research()


def _render_matchup_research() -> None:
    with st.expander("Match Winner Research (manual entry)", expanded=False):
        st.caption(
            "Uses already-cached results. No additional requests unless "
            "you search a new team above first."
        )
        cola, colb = st.columns(2)
        with cola:
            name_a = st.text_input("Team A", key="cs2_mwr_a")
        with colb:
            name_b = st.text_input("Team B", key="cs2_mwr_b")

        if not name_a or not name_b:
            return

        if st.button("Compute lean", key="cs2_mwr_go"):
            results_all = st.session_state.get(_SK_RESULTS) or []
            if not results_all:
                st.warning("Load recent results in the Overview tab first.")
                return
            form_a = build_team_form(results_all, name_a, n=10)
            form_b = build_team_form(results_all, name_b, n=10)
            proj = project_match_winner(
                form_a, form_b, team_a_name=name_a, team_b_name=name_b
            )
            _render_projection(proj)


# ---------------------------------------------------------------------------
# Tab: Maps
# Pure derivation from already-loaded player/team session_state.
# Zero additional requests.
# ---------------------------------------------------------------------------


def _render_maps() -> None:
    cs2_section_header("Maps", "Derived from already-loaded player / team data")
    st.caption("No additional requests here. Load player or team stats first.")

    stats = st.session_state.get(_SK_P_STATS)
    if stats:
        avg_kills = player_avg_kills_by_map(stats)
        avg_rating = player_avg_rating_by_map(stats)
        if avg_kills:
            import pandas as pd
            ign = stats.ign or "Player"
            rows = [
                {
                    "Map":      m,
                    "Avg K":    f"{avg_kills.get(m, 0):.1f}" if m in avg_kills else "—",
                    "Avg Rat.": f"{avg_rating.get(m, 0):.2f}" if m in avg_rating else "—",
                    "N":        sum(
                        1 for e in stats.recent_maps if e.map_name == m
                    ),
                }
                for m in CS2_ACTIVE_MAPS
            ]
            st.markdown(f"#### {ign} — Kills by Map")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            cs2_no_data("Load player stats in the Players tab first.")
    else:
        cs2_no_data("Load player stats in the Players tab to see map breakdowns.")

    st.markdown("---")
    t_stats = st.session_state.get(_SK_T_STATS)
    if t_stats:
        cs2_section_header("Team Map Pool", t_stats.team_name or "")
        cs2_team_map_pool_table(t_stats)

        with st.expander("Compare vs second team", expanded=False):
            b_name = st.text_input("Team B name", key="cs2_map_b")
            if b_name and st.button("Load Team B stats", key="cs2_map_b_go"):
                with st.spinner(f"Loading {b_name!r}…"):
                    try:
                        tb = find_team(b_name)
                        sb = load_team_stats(tb.id) if (tb and tb.id) else None
                    except Exception as exc:
                        _handle_exc(exc, "Team B stats")
                        sb = None
                if sb:
                    rows = map_matchup_summary(t_stats, sb)
                    import pandas as pd
                    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                                 hide_index=True)
                else:
                    st.warning("Could not load Team B stats.")
    else:
        cs2_no_data("Load team stats in the Teams tab to see map pool data here.")


# ---------------------------------------------------------------------------
# Projection renderer
# ---------------------------------------------------------------------------


def _render_projection(proj) -> None:
    val = proj.projection
    is_nan = isinstance(val, float) and math.isnan(val)

    if is_nan:
        st.warning("Projection unavailable — see explanation below.")
    else:
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Projection", f"{val:.2f}")
        with c2:
            edge = proj.edge
            st.metric(
                "Edge",
                f"{edge:+.2f}"
                if not (isinstance(edge, float) and math.isnan(edge)) else "—",
            )
        with c3:
            label = proj.lean or "—"
            color = "#2ecc71" if label == "Over" else "#e74c3c" if label == "Under" else "#aaa"
            st.markdown(
                f"<div style='font-size:1.5rem;font-weight:bold;color:{color}'>{label}</div>",
                unsafe_allow_html=True,
            )

    if proj.beta:
        st.caption("⚠️ Beta — limited sample.")

    if proj.explanation:
        with st.expander("Explanation", expanded=False):
            for line in proj.explanation:
                st.markdown(f"• {line}")

    if proj.factors:
        with st.expander("Factors", expanded=False):
            for f in proj.factors:
                st.markdown(f"**{f.name}**: {f.note}")

    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(proj.confidence, "⚪")
    st.caption(
        f"Confidence: {conf_icon} {proj.confidence}"
        + (f" | {proj.sample_size} maps" if proj.sample_size > 0 else "")
    )


# ---------------------------------------------------------------------------
# GRID audit panel (sidebar) — inspect raw API responses
# ---------------------------------------------------------------------------


def _render_grid_audit_panel() -> None:
    """Sidebar expander: fetch raw GRID responses to audit actual field shapes.

    Called from inside ``with st.sidebar:`` — must use ``st.expander()``,
    NOT ``st.sidebar.expander()`` (which would create a nested sidebar).
    """
    import json as _json
    from services.cs2 import grid_provider as _grid

    with st.expander("🔬 GRID Response Audit", expanded=False):
        st.caption(
            "Fetch a real GRID response to see exactly what fields "
            "are available at your access level."
        )

        if st.button("Audit series list (2 nodes)", key="cs2_audit_series"):
            with st.spinner("Calling GRID allSeries…"):
                result = _grid.audit_series(limit=2)
            if result["error"]:
                st.error(f"GRID error: {result['error']}")
            else:
                src = result["source"]
                nodes = result["nodes"]
                st.success(f"Got {len(nodes)} node(s) [{src}]")
                for i, node in enumerate(nodes):
                    with st.expander(f"Node {i+1}: {node.get('id', '?')}", expanded=(i == 0)):
                        st.code(_json.dumps(node, indent=2, default=str), language="json")

        st.markdown("---")
        series_id = st.text_input(
            "Series ID for end-state audit", key="cs2_audit_es_id",
            help="Paste a series ID from the audit above to inspect its end-state file.",
        )
        if st.button("Audit end-state file", key="cs2_audit_endstate") and series_id.strip():
            with st.spinner(f"Downloading end-state for {series_id}…"):
                result = _grid.audit_endstate(series_id.strip())
            if result["error"]:
                st.error(f"GRID error: {result['error']}")
            elif not result["data"]:
                st.warning("Empty response — series may not be finished or not accessible.")
            else:
                src = result["source"]
                st.success(f"Got end-state [{src}]")
                st.code(_json.dumps(result["data"], indent=2, default=str), language="json")


# ---------------------------------------------------------------------------
# Tab: Tournaments
# ---------------------------------------------------------------------------


def _render_tournaments() -> None:
    import os as _os
    from services.cs2 import grid_provider as _grid

    cs2_section_header(
        "Tournaments",
        "CS2 event list — GRID Open Access only (experimental).",
    )

    requested = _os.environ.get("CS2_PROVIDER", "hltv").lower()

    # With HLTV primary, the gigobyte/hltv schema does not expose a
    # tournament list endpoint.  Be honest instead of rendering a broken tab.
    if requested != "grid":
        st.info(
            "Tournament listings are only available via the experimental "
            "GRID Open Access path.  HLTV (the default) does not expose an "
            "event-list endpoint in the gigobyte/hltv bridge.  \n"
            "To try GRID, set `CS2_PROVIDER=grid` and add `GRID_API_KEY` "
            "in `.env` — note the free Open Access tier may deny this query.",
            icon="ℹ️",
        )
        return

    if not _grid.is_configured():
        cs2_grid_no_key_banner()
        return

    st.info(
        "The `allTournaments` GraphQL query shape is pending format audit. "
        "Use the **🔬 GRID Response Audit** panel in the sidebar to inspect "
        "what fields GRID returns at your access level.",
        icon="⚠️",
    )

    if _SK_TOURNAMENTS in st.session_state:
        _show_tournaments(st.session_state[_SK_TOURNAMENTS])
        return

    if st.button("Try loading tournaments", key="cs2_load_tournaments"):
        with st.spinner("Fetching tournaments from GRID…"):
            try:
                st.session_state[_SK_TOURNAMENTS] = dp.tournaments(limit=30)
            except Exception as exc:
                _handle_exc(exc, "tournaments")
                st.session_state[_SK_TOURNAMENTS] = []
        st.rerun()
    else:
        st.caption("Tournament data loads on demand.")


def _show_tournaments(tournaments: list) -> None:
    if not tournaments:
        cs2_no_data(
            "No tournaments returned. The allTournaments query may require a higher "
            "GRID access level, or the schema fields may differ from what was assumed. "
            "Use the GRID Audit panel to inspect the raw response."
        )
        return

    st.markdown(f"**{len(tournaments)} events found**")
    cs2_tournament_table(tournaments)


# ---------------------------------------------------------------------------
# Backward-compat helpers (used by smoke tests only)
# ---------------------------------------------------------------------------


def _payload_is_stale(payload: object) -> bool:
    if isinstance(payload, dict):
        return bool(payload.get("_stale", False))
    if isinstance(payload, list):
        return any(isinstance(x, dict) and x.get("_stale") for x in payload)
    return False


def _default_map_index(options: list, selected: str | None) -> int:
    if selected and selected in options:
        return options.index(selected)
    return 0


def _filter_matches_by_map(matches: list[dict], map_name: str | None) -> list[dict]:
    if not map_name:
        return list(matches)
    return [
        m for m in matches
        if any(g.get("map") == map_name for g in (m.get("games") or []))
    ]
