"""NBA Player Prop Research Dashboard — Streamlit entry point.

Run with:
    streamlit run app.py

Sidebar drives everything (player, season, stat, manual line, weights,
toggles). The main area splits into five tabs: Overview, Game Logs,
Splits, Trends, Projection Details.

This file is deliberately long but flat — each tab is its own small
function so you can read top to bottom.
"""

from __future__ import annotations

import logging
import math

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (
    ASSETS_DIR,
    DEFAULT_ADJUSTMENTS,
    DEFAULT_SEASON_TYPE_LABEL,
    DEFAULT_WEIGHTS,
    QUARTER_STAT_COLS,
    SEASON_TYPES,
    STAT_COLUMNS,
    STAT_OVERLAY_CHIPS,
    THRESHOLD_STATS,
    current_nba_season,
    recent_seasons,
)
from database import cache_db
from models.hit_rate import hit_rate, hit_rates_over_windows
from models.metrics import (
    compute_filtered_vs_excluded_diff,
    compute_graph_avg,
    compute_hit_rate,
    compute_season_avg,
)
from models.projection import compute_edge, project
from models.rolling import compute_rolling_summary, rolling_series
from models.splits import home_away_split, opponent_history, rest_day_split, win_loss_split
from models.stats import add_derived_stats, resolve_stat_column
from services import (
    defense_service,
    filter_service,
    gamelog_service,
    player_service,
    quarter_service,
    rankings_service,
    schedule_service,
    teammate_service,
)
from services.nba_client import reset_circuit_breaker
from ui import filter_modal as filter_modal_module
from ui import layout as ui_layout
from ui import teammate_modal as teammate_modal_module
from ui.filter_state import get_state as get_filter_state
from ui.cs2_page import render_cs2_page
from ui.game_markets_page import render_game_markets_page
from ui.graph import render_research_graph
from ui.theme import inject_theme
from utils.formatting import (
    compact_season_label,
    fmt_number,
    fmt_percent,
    fmt_signed,
    over_under_label,
)
from utils.logging_setup import configure_logging

configure_logging()
logger = logging.getLogger("app")


# ---------------------------------------------------------------------------
# Page + styling
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="NBA Prop Research",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _inject_css() -> None:
    css_path = ASSETS_DIR / "styles.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)


_inject_css()
inject_theme()


# ---------------------------------------------------------------------------
# Cached data loaders (Streamlit in-process cache; underlying services also
# use SQLite for cross-session persistence).
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False, ttl=60 * 60)
def load_player_search(query: str, active_only: bool) -> pd.DataFrame:
    return player_service.search_players(query, active_only=active_only)


@st.cache_data(show_spinner=False, ttl=60 * 60)
def load_player_info(player_id: int) -> dict | None:
    return player_service.get_player_team_for_season(player_id, current_nba_season())


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def load_game_log(player_id: int, season: str, season_type: str) -> pd.DataFrame:
    df = gamelog_service.fetch_player_game_log(player_id, season, season_type=season_type)
    return add_derived_stats(df)


@st.cache_data(show_spinner=False, ttl=60 * 60)
def load_next_opponent(team_id: int | None, season: str) -> dict | None:
    return schedule_service.get_next_opponent(team_id, season)


@st.cache_data(show_spinner=False, ttl=12 * 60 * 60)
def load_matchup_info(opponent_team_id: int | None, stat_base_col: str, season: str) -> dict | None:
    if not opponent_team_id or not stat_base_col:
        return None
    return defense_service.matchup_factor(opponent_team_id, stat_base_col, season)


@st.cache_data(show_spinner=False, ttl=12 * 60 * 60)
def load_rank_maps(season: str, season_type: str) -> dict[str, dict[int, int]]:
    """Team-level rank dictionaries for every supported opp-rank chip."""
    try:
        return rankings_service.build_chip_rank_maps(season, season_type=season_type)
    except Exception as exc:
        logger.warning("rank-map build failed: %s", exc)
        return {}


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def load_teammate_roster(team_id: int | None, season: str,
                         exclude_player_id: int | None) -> pd.DataFrame:
    try:
        return teammate_service.list_teammates(team_id, season,
                                               exclude_player_id=exclude_player_id)
    except Exception as exc:
        logger.warning("teammate roster load failed: %s", exc)
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def load_teammate_game_ids(teammate_id: int, season: str,
                           season_type: str) -> set[str]:
    try:
        return teammate_service.teammate_game_ids(teammate_id, season,
                                                  season_type=season_type)
    except Exception as exc:
        logger.warning("teammate game ids fetch failed for %s: %s", teammate_id, exc)
        return set()


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def load_teammate_summary_table(team_id: int | None, season: str,
                                season_type: str,
                                exclude_player_id: int | None) -> pd.DataFrame:
    """Roster + per-teammate aggregates used by the Filter-by-Teammate modal.

    `market_col` is intentionally NOT a parameter here. The underlying
    build_teammate_table now stores ALL stat averages in a STAT_AVGS dict
    column, so switching markets (Points → Assists etc.) does NOT bust this
    cache key or trigger a fresh 15-player game-log rebuild.

    The underlying per-teammate game log fetches are SQLite-cached, so the
    first open for a given team/season is slow, but subsequent opens are
    near-instant. See README for the latency note.
    """
    try:
        return teammate_service.build_teammate_table(
            team_id, season, season_type, exclude_player_id,
        )
    except Exception as exc:
        logger.warning("teammate summary table load failed: %s", exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Base stats we have team-level opponent-allowed data for.
_MATCHUP_SUPPORTED = {"PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV"}

# For derived / threshold combos, pick the dominant component as a stand-in
# for opponent matchup lookups.
_DERIVED_MATCHUP_PROXY = {
    "PRA": "PTS", "PA": "PTS", "RA": "REB", "PR": "PTS",
    "Stocks": "STL", "DD": "PTS", "TD": "PTS",
    # Quarter stats use their parent stat
    "Q1_PTS": "PTS", "Q1_REB": "REB", "Q1_AST": "AST",
    "Q1_FG3M": "FG3M",
    "H1_PTS": "PTS", "H1_REB": "REB", "H1_AST": "AST",
    "H1_FG3M": "FG3M",
}


def _stat_base_column(stat_col: str) -> str | None:
    """Map a stat column (possibly a derived combo) to a base stat we can
    look up in LeagueDashTeamStats Opponent splits. Returns None when the
    stat has no reasonable analog."""
    if stat_col in _DERIVED_MATCHUP_PROXY:
        return _DERIVED_MATCHUP_PROXY[stat_col]
    return stat_col if stat_col in _MATCHUP_SUPPORTED else None


def _derive_next_opponent(gl: pd.DataFrame, next_opp_info: dict | None) -> dict:
    """Pick a best-effort opponent descriptor for UI display.

    Prefers ScoreboardV2 (today's game). Falls back to most recent opponent
    from the player's game log, clearly labelling the source.
    """
    if next_opp_info and next_opp_info.get("opponent_team_id"):
        info = schedule_service.resolve_opponent_team(next_opp_info["opponent_team_id"]) or {}
        return {
            "source": "today_scoreboard",
            "is_home": next_opp_info.get("is_home"),
            "team_id": next_opp_info["opponent_team_id"],
            "team_abbreviation": info.get("team_abbreviation", ""),
            "team_name": info.get("team_name", ""),
            "note": "Opponent from today's scoreboard.",
        }

    if gl is not None and not gl.empty and "OPPONENT" in gl.columns:
        abbr = gl.iloc[0].get("OPPONENT", "") or ""
        info = schedule_service.resolve_opponent_by_abbr(abbr) or {}
        return {
            "source": "last_game",
            "is_home": bool(gl.iloc[0].get("IS_HOME", False)),
            "team_id": info.get("team_id"),
            "team_abbreviation": abbr,
            "team_name": info.get("team_name", ""),
            "note": "Showing last game's opponent (no confirmed next game in today's schedule).",
        }

    return {"source": "none", "is_home": None, "team_id": None,
            "team_abbreviation": "", "team_name": "",
            "note": "No opponent info available."}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def render_sidebar() -> dict:
    st.sidebar.title("Prop Research")
    st.sidebar.caption("Free, local-only. Type a prop line and compare it to a transparent projection.")

    # --- Player search ---------------------------------------------------
    st.sidebar.subheader("1. Player")
    query = st.sidebar.text_input(
        "Search player", value="Jalen Brunson",
        help="Type any part of a player's name. Searches the bundled nba_api static list (no network).",
    )
    active_only = st.sidebar.checkbox("Active players only", value=True)

    results = load_player_search(query, active_only) if query and len(query) >= 2 else pd.DataFrame()
    selected_player = None
    if results.empty:
        st.sidebar.info("Type at least 2 characters to search.")
    else:
        options = {f"{row['full_name']} ({'active' if row['is_active'] else 'historical'})": int(row["id"])
                   for _, row in results.iterrows()}
        label = st.sidebar.selectbox("Pick player", list(options.keys()))
        selected_player = {"id": options[label], "name": label.rsplit(" (", 1)[0]}

    # --- Season + stat ---------------------------------------------------
    st.sidebar.subheader("2. Season & Stat")
    seasons = recent_seasons(n=8)
    season = st.sidebar.selectbox(
        "Season", seasons, index=0,
        help="NBA season label uses the starting year, e.g. 2025-26 means the season that began in October 2025.",
    )

    season_type_labels = list(SEASON_TYPES.keys())
    default_type_idx = season_type_labels.index(DEFAULT_SEASON_TYPE_LABEL)
    season_type_label = st.sidebar.selectbox(
        "Season type", season_type_labels, index=default_type_idx,
        help=("Which slice of the season to load. 'All' fetches Preseason, "
              "Regular Season, and Playoffs and combines them."),
    )
    season_type = SEASON_TYPES[season_type_label]

    stat_label = st.sidebar.selectbox("Stat", list(STAT_COLUMNS.keys()),
                                      index=list(STAT_COLUMNS.keys()).index("Points"))
    stat_col = resolve_stat_column(stat_label)

    # --- Manual prop line ------------------------------------------------
    # For threshold-based (0/1) stats like DD and TD the sportsbook line is
    # almost always 0.5, so we default there automatically.
    _is_threshold_stat = stat_col in THRESHOLD_STATS
    _default_line = 0.5 if _is_threshold_stat else 24.5

    # Reset the stored line whenever the user switches to/from a threshold stat
    # so the input reflects the right default without requiring manual editing.
    if st.session_state.get("_prop_line_stat") != stat_col:
        st.session_state["_prop_line_stat"] = stat_col
        st.session_state["_prop_line_value"] = _default_line

    st.sidebar.subheader("3. Prop Line")
    if _is_threshold_stat:
        st.sidebar.caption("DD/TD: line is typically 0.5 (Over = yes, they get one).")
    manual_line = st.sidebar.number_input(
        "Posted line (you type this)",
        min_value=0.0, max_value=150.0,
        value=float(st.session_state.get("_prop_line_value", _default_line)),
        step=0.5, format="%.1f",
        key="_prop_line_widget",
    )
    # Keep session state in sync with user edits.
    st.session_state["_prop_line_value"] = manual_line
    hit_threshold = manual_line  # hit threshold always equals the prop line

    # --- Projection config ----------------------------------------------
    st.sidebar.subheader("4. Projection")
    w5 = st.sidebar.slider("Weight: Last 5", 0.0, 1.0, float(DEFAULT_WEIGHTS["w5"]), 0.05)
    w10 = st.sidebar.slider("Weight: Last 10", 0.0, 1.0, float(DEFAULT_WEIGHTS["w10"]), 0.05)
    wSeason = st.sidebar.slider("Weight: Season", 0.0, 1.0, float(DEFAULT_WEIGHTS["wSeason"]), 0.05)
    st.sidebar.caption("Weights are auto-normalized to sum to 1.")

    apply_matchup = st.sidebar.checkbox("Apply matchup adjustment",
                                        value=DEFAULT_ADJUSTMENTS["matchup"])
    apply_home_away = st.sidebar.checkbox("Apply home/away adjustment",
                                          value=DEFAULT_ADJUSTMENTS["home_away"])
    apply_minutes = st.sidebar.checkbox("Apply minutes adjustment",
                                        value=DEFAULT_ADJUSTMENTS["minutes"])

    projected_minutes_override = None
    if apply_minutes:
        projected_minutes_override = st.sidebar.number_input(
            "Projected minutes (optional, blank = L5 mean)",
            min_value=0.0, max_value=60.0, value=0.0, step=0.5, format="%.1f",
            help="0 means 'use last-5 average'.",
        )
        if projected_minutes_override == 0:
            projected_minutes_override = None

    # --- Cache controls --------------------------------------------------
    st.sidebar.subheader("5. Data")
    if st.sidebar.button("Refresh data (clear cache)"):
        if selected_player:
            gamelog_service.invalidate_player_cache(selected_player["id"], season)
        cache_db.clear_namespace("schedule")
        cache_db.clear_namespace("defense")
        cache_db.clear_namespace("pbp_v3")
        cache_db.clear_namespace("box_roster_v3")
        cache_db.clear_namespace("qbox")
        # Reset the nba_api circuit breaker + per-endpoint failure cache so
        # a manual refresh actually retries even if the server was unavailable
        # a moment ago. Without this, the in-process cooldowns would block all
        # live fetches for up to 90s after the last failure.
        reset_circuit_breaker()
        # Clear Streamlit in-process cache too.
        load_player_search.clear()
        load_player_info.clear()
        load_game_log.clear()
        load_next_opponent.clear()
        load_matchup_info.clear()
        load_teammate_summary_table.clear()
        st.sidebar.success("Cache cleared and circuit breaker reset. Data will re-fetch.")
        st.rerun()

    return {
        "player": selected_player,
        "season": season,
        "season_type": season_type,
        "season_type_label": season_type_label,
        "stat_label": stat_label,
        "stat_col": stat_col,
        "manual_line": float(manual_line),
        "hit_threshold": float(hit_threshold),
        "is_threshold_stat": _is_threshold_stat,
        "weights": {"w5": w5, "w10": w10, "wSeason": wSeason},
        "apply_matchup": apply_matchup,
        "apply_home_away": apply_home_away,
        "apply_minutes": apply_minutes,
        "projected_minutes": projected_minutes_override,
    }


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def render_overview(ctx: dict) -> None:
    p = ctx["player"]
    if not p:
        st.warning("Pick a player from the sidebar to get started.")
        return

    gl = ctx["game_log"]
    stat_col = ctx["stat_col"]
    stat_label = ctx["stat_label"]
    line = ctx["manual_line"]
    projection = ctx["projection"]
    edge = ctx["edge"]
    l10_hit = ctx["hit_rate_l10"]
    opp = ctx["opponent_desc"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Player", p["name"])
    col2.metric(
        "Opponent",
        opp.get("team_abbreviation") or "-",
        help=opp.get("note", ""),
    )
    col3.metric("Stat", stat_label)
    col4.metric("Manual Line", fmt_number(line, decimals=1))

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Projection", fmt_number(projection.final, decimals=2))
    col6.metric("Edge", fmt_signed(edge, decimals=2),
                delta=fmt_signed(edge, decimals=2) if not math.isnan(edge) else None)
    col7.metric("Over/Under Lean", over_under_label(edge))
    col8.metric("Hit Rate (L10)", fmt_percent(l10_hit.rate))

    n_games = 0 if gl is None or gl.empty else len(gl)
    st.caption(
        f"Based on {n_games} games — {ctx['season']} · {ctx['season_type_label']}."
    )

    st.divider()

    if gl.empty:
        st.info(
            f"No {ctx['season_type_label'].lower()} games found for {p['name']} in "
            f"{ctx['season']}. Try another season or season type."
        )
        return

    # Plain-English takeaway
    lean = over_under_label(edge)
    takeaway = (
        f"**Takeaway.** For {p['name']}'s {stat_label.lower()} line of {fmt_number(line, decimals=1)}, "
        f"the projection lands at {fmt_number(projection.final, decimals=2)}, "
        f"an edge of {fmt_signed(edge, decimals=2)}. "
        f"Last-10 hit rate over {fmt_number(ctx['hit_threshold'], decimals=1)} is "
        f"{fmt_percent(l10_hit.rate)} ({l10_hit.hits}/{l10_hit.games})."
    )
    if lean in ("Over", "Under"):
        takeaway += f" Model leans **{lean}**."
    else:
        takeaway += " Model sees no clear edge."
    st.markdown(takeaway)

    if opp.get("source") == "last_game":
        st.caption(
            "Note: opponent shown is from the player's most recent game because "
            "we could not confirm a game scheduled for today via the free scoreboard endpoint."
        )

    # If the user enabled an adjustment that produced a neutral (1.0) factor,
    # tell them — otherwise the toggle looks like it did nothing.
    neutralized = [f.name for f in projection.factors if abs(f.factor - 1.0) < 1e-9]
    if neutralized:
        names = ", ".join(n.replace("_", " ") for n in neutralized)
        st.caption(f"Adjustment(s) {names} returned neutral (factor = 1.00). "
                   "See the Projection Details tab for why.")


def render_game_logs(ctx: dict) -> None:
    gl = ctx["game_log"]
    stat_col = ctx["stat_col"]
    if gl.empty:
        st.info("No games to show.")
        return

    st.subheader("Game Log")

    display_cols = [
        "GAME_DATE", "SEASON_TYPE", "MATCHUP", "WL", "MIN",
        # Core
        "PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV",
        # Tier 1 extras
        "OREB", "DREB", "FTM", "FTA", "FG3A", "PF",
        # Derived combos
        "PRA", "PA", "RA", "PR", "Stocks",
        # Threshold (0/1)
        "DD", "TD",
        # Quarter / half (appear when lazy-loaded)
        "Q1_PTS", "Q1_REB", "Q1_AST", "Q1_FG3M",
        "H1_PTS", "H1_REB", "H1_AST", "H1_FG3M",
        "REST_DAYS",
    ]
    present = [c for c in display_cols if c in gl.columns]
    view = gl[present].copy()
    if "GAME_DATE" in view.columns:
        view["GAME_DATE"] = pd.to_datetime(view["GAME_DATE"]).dt.date
    if "MIN" in view.columns:
        view["MIN"] = pd.to_numeric(view["MIN"], errors="coerce").round(1)

    # Filter controls
    show_type_filter = "SEASON_TYPE" in gl.columns and gl["SEASON_TYPE"].nunique() > 1
    if show_type_filter:
        col1, col2, col3, col4 = st.columns([1, 1, 1, 2])
        with col4:
            type_options = ["All"] + sorted(gl["SEASON_TYPE"].dropna().unique().tolist())
            type_filter = st.selectbox("Season type", type_options, index=0)
    else:
        col1, col2, col3 = st.columns([1, 1, 2])
        type_filter = "All"
    with col1:
        home_filter = st.selectbox("Location", ["All", "Home", "Away"], index=0)
    with col2:
        wl_filter = st.selectbox("Result", ["All", "Wins", "Losses"], index=0)
    with col3:
        opp_options = ["All"] + sorted(gl["OPPONENT"].dropna().unique().tolist()) if "OPPONENT" in gl.columns else ["All"]
        opp_filter = st.selectbox("Opponent", opp_options, index=0)

    mask = pd.Series([True] * len(gl), index=gl.index)
    if home_filter == "Home":
        mask &= gl["IS_HOME"] == True
    elif home_filter == "Away":
        mask &= gl["IS_HOME"] == False
    if wl_filter == "Wins":
        mask &= gl["IS_WIN"] == True
    elif wl_filter == "Losses":
        mask &= gl["IS_WIN"] == False
    if opp_filter != "All":
        mask &= gl["OPPONENT"] == opp_filter
    if type_filter != "All" and "SEASON_TYPE" in gl.columns:
        mask &= gl["SEASON_TYPE"] == type_filter

    filtered_view = view[mask.values]
    st.dataframe(
        filtered_view,
        use_container_width=True,
        hide_index=True,
        column_config={
            stat_col: st.column_config.NumberColumn(
                label=f"{stat_col} (selected)",
                help="The currently selected stat.",
                format="%.1f",
            )
        },
    )

    st.caption(f"{len(filtered_view)} of {len(gl)} games shown.")


def render_splits(ctx: dict) -> None:
    gl = ctx["game_log"]
    stat_col = ctx["stat_col"]
    stat_label = ctx["stat_label"]
    if gl.empty:
        st.info("No games to split.")
        return

    rolling = compute_rolling_summary(gl, stat_col)
    hr_table = hit_rates_over_windows(gl, stat_col, ctx["hit_threshold"])

    st.subheader(f"Rolling averages — {stat_label}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("L5", fmt_number(rolling.last5, 2))
    c2.metric("L10", fmt_number(rolling.last10, 2))
    c3.metric("L20", fmt_number(rolling.last20, 2))
    c4.metric("Season", fmt_number(rolling.season_mean, 2))

    c5, c6, c7 = st.columns(3)
    c5.metric("Median", fmt_number(rolling.season_median, 2))
    c6.metric("Std. dev", fmt_number(rolling.season_std, 2))
    c7.metric("Games played", str(rolling.games_played))

    st.divider()

    st.subheader(f"Hit rate over {fmt_number(ctx['hit_threshold'], 1)}")
    hr_display = hr_table.copy()
    hr_display["rate"] = hr_display["rate"].apply(lambda x: fmt_percent(x))
    st.dataframe(hr_display, hide_index=True, use_container_width=True)

    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Home vs Away")
        ha = home_away_split(gl, stat_col)
        if ha.empty:
            st.caption("Not enough data.")
        else:
            st.dataframe(ha, hide_index=True, use_container_width=True)
    with col_b:
        st.subheader("Win vs Loss")
        wl = win_loss_split(gl, stat_col)
        if wl.empty:
            st.caption("Not enough data.")
        else:
            st.dataframe(wl, hide_index=True, use_container_width=True)

    st.subheader("Rest days")
    rd = rest_day_split(gl, stat_col)
    if rd.empty:
        st.caption("Not enough data.")
    else:
        st.dataframe(rd, hide_index=True, use_container_width=True)

    st.divider()

    st.subheader("Vs. opponent history")
    opp_abbr = ctx["opponent_desc"].get("team_abbreviation") or ""
    if not opp_abbr:
        st.caption("No opponent context available.")
    else:
        oh = opponent_history(gl, stat_col, opp_abbr)
        cA, cB, cC = st.columns(3)
        cA.metric(f"Games vs {opp_abbr}", str(oh["games"]))
        cB.metric("Mean", fmt_number(oh["mean"], 2))
        cC.metric("Median", fmt_number(oh["median"], 2))
        if oh["recent"]:
            recent_df = pd.DataFrame(oh["recent"])
            if "date" in recent_df.columns:
                recent_df["date"] = pd.to_datetime(recent_df["date"]).dt.date
            st.dataframe(recent_df, hide_index=True, use_container_width=True)


def render_trends(ctx: dict) -> None:
    gl = ctx["game_log"]
    stat_col = ctx["stat_col"]
    stat_label = ctx["stat_label"]
    if gl.empty:
        st.info("No trend data available.")
        return

    window = st.slider("Rolling window (games)", min_value=3, max_value=20, value=5, step=1)

    stat_trend = rolling_series(gl, stat_col, window=window)
    if not stat_trend.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=stat_trend["GAME_DATE"], y=stat_trend[stat_col],
            mode="lines+markers", name=stat_label,
        ))
        fig.add_trace(go.Scatter(
            x=stat_trend["GAME_DATE"], y=stat_trend["rolling_mean"],
            mode="lines", name=f"Rolling L{window}",
            line=dict(dash="dash"),
        ))
        fig.add_hline(y=ctx["manual_line"], line_dash="dot",
                      annotation_text=f"Line {fmt_number(ctx['manual_line'], 1)}",
                      annotation_position="top left")
        fig.update_layout(
            title=f"{stat_label} per game",
            xaxis_title="Game date",
            yaxis_title=stat_label,
            height=420,
            margin=dict(t=60, b=40, l=40, r=20),
        )
        st.plotly_chart(fig, use_container_width=True)

    if "MIN" in gl.columns:
        minutes_trend = rolling_series(gl, "MIN", window=window)
        if not minutes_trend.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=minutes_trend["GAME_DATE"], y=minutes_trend["MIN"],
                mode="lines+markers", name="Minutes",
            ))
            fig2.add_trace(go.Scatter(
                x=minutes_trend["GAME_DATE"], y=minutes_trend["rolling_mean"],
                mode="lines", name=f"Rolling L{window}",
                line=dict(dash="dash"),
            ))
            fig2.update_layout(
                title="Minutes per game",
                xaxis_title="Game date",
                yaxis_title="Minutes",
                height=360,
                margin=dict(t=60, b=40, l=40, r=20),
            )
            st.plotly_chart(fig2, use_container_width=True)

    # Simple usage approximation from box-score columns. True usage requires
    # team totals; we approximate as (FGA + 0.44*FTA + TOV) per game. Labeled
    # clearly as approximate in the UI.
    if all(c in gl.columns for c in ("FGA", "FTA", "TOV")):
        usage_df = gl[["GAME_DATE", "FGA", "FTA", "TOV"]].copy()
        usage_df["USAGE_PROXY"] = (
            pd.to_numeric(usage_df["FGA"], errors="coerce")
            + 0.44 * pd.to_numeric(usage_df["FTA"], errors="coerce")
            + pd.to_numeric(usage_df["TOV"], errors="coerce")
        )
        usage_df = usage_df.sort_values("GAME_DATE")
        fig3 = px.line(
            usage_df, x="GAME_DATE", y="USAGE_PROXY",
            title="Usage proxy (FGA + 0.44*FTA + TOV) — approximate, no team totals",
            markers=True,
        )
        fig3.update_layout(height=320, margin=dict(t=60, b=40, l=40, r=20))
        st.plotly_chart(fig3, use_container_width=True)
        st.caption(
            "Usage proxy is a rough single-player approximation. A true usage rate "
            "needs team minutes and possessions; that's a v2 enhancement."
        )


def render_projection_details(ctx: dict) -> None:
    projection = ctx["projection"]
    line = ctx["manual_line"]
    edge = ctx["edge"]

    st.subheader("Projection breakdown")

    c1, c2, c3 = st.columns(3)
    c1.metric("Base", fmt_number(projection.base, 2))
    c2.metric("Final", fmt_number(projection.final, 2))
    c3.metric("Line", fmt_number(line, 1))

    c4, c5, c6 = st.columns(3)
    c4.metric("Edge", fmt_signed(edge, 2))
    c5.metric("Lean", over_under_label(edge))
    c6.metric(
        "L5 / L10 / Season",
        f"{fmt_number(projection.last5, 1)} / "
        f"{fmt_number(projection.last10, 1)} / "
        f"{fmt_number(projection.season_mean, 1)}",
    )

    st.divider()

    # Weights: clean mini-display, no raw JSON.
    st.markdown("**Weights (normalized to sum to 1)**")
    wc1, wc2, wc3 = st.columns(3)
    wc1.metric("Last 5", fmt_percent(projection.weights["w5"]))
    wc2.metric("Last 10", fmt_percent(projection.weights["w10"]))
    wc3.metric("Season", fmt_percent(projection.weights["wSeason"]))

    st.divider()

    # Adjustment factors: one clean block per factor.
    if not projection.factors:
        st.info("No adjustments applied. Toggle matchup / home-away / minutes in the sidebar.")
    else:
        st.markdown("**Adjustment factors**")
        for f in projection.factors:
            with st.container(border=True):
                hc1, hc2 = st.columns([1, 4])
                hc1.metric(f.name.replace("_", " ").title(), f"×{f.factor:.3f}")
                hc2.caption(f.note)

    st.divider()

    st.markdown("**Plain-English explanation**")
    for line_text in projection.explanation:
        st.write(f"- {line_text}")


# ---------------------------------------------------------------------------
# Research tab (filter-aware, default view)
# ---------------------------------------------------------------------------


def render_research(ctx: dict) -> None:
    """Filter-driven Research view: key metrics + bar chart + stat-card
    grid + teammate row. Uses `filtered_log` from ctx; excluded_log for
    diff computations."""
    filtered = ctx["game_log"]
    excluded = ctx.get("excluded_log", pd.DataFrame())
    full = ctx.get("full_game_log", filtered)
    state = get_filter_state()

    # Stale-data warning: shown when the gamelog service had to fall back to
    # SQLite data that was beyond the normal TTL (stats.nba.com was unavailable).
    if (full is not None and not full.empty
            and "_stale" in full.columns and full["_stale"].any()):
        st.warning(
            "⚠️ Live NBA data is temporarily unavailable. "
            "Showing cached data from a previous session — projections and "
            "hit rates may not include the most recent game(s). "
            "Click **Refresh data** in the sidebar when stats.nba.com is back.",
            icon=None,
        )

    stat_col = ctx["stat_col"]
    stat_label = ctx["stat_label"]
    manual_line = ctx["manual_line"]
    projection = ctx["projection"]
    edge = ctx["edge"]

    # Sample header
    hr = compute_hit_rate(filtered, stat_col, manual_line)
    filtered_avg = compute_graph_avg(filtered, stat_col, mode=state.avg_or_median)
    season_avg = compute_season_avg(full, stat_col, mode=state.avg_or_median)
    narrowed_total = ctx.get("narrowed_total", len(filtered))
    sample_text = (
        f"{len(filtered)} of {narrowed_total} filtered games shown "
        f"(full season: {len(full)})"
    )

    ui_layout.render_key_metrics_row(
        hit_rate_row=hr,
        projection_final=projection.final,
        edge=edge,
        manual_line=manual_line,
        filtered_avg=filtered_avg,
        season_avg=season_avg,
        sample_size_text=sample_text,
    )

    st.divider()

    if filtered is None or filtered.empty:
        st.info(
            "No games match the current filter. Open the Filters modal and "
            "clear or loosen a constraint."
        )
        return

    overlay_col = state.overlay_stat
    overlay_label = None
    if overlay_col:
        for lbl, col in STAT_OVERLAY_CHIPS.items():
            if col == overlay_col:
                overlay_label = lbl
                break

    render_research_graph(
        filtered,
        stat_col=stat_col,
        stat_label=stat_label,
        prop_line=float(manual_line),
        overlay_col=overlay_col,
        overlay_label=overlay_label,
        player_id=int(ctx.get("player_id") or 0) or None,
        league_id="10" if st.session_state.get("_props_league") == "wnba" else "00",
        teammate_df=ctx.get("teammate_df"),
    )

    st.divider()
    st.markdown("##### Per-stat averages (filtered vs excluded)")
    ui_layout.render_stat_card_grid(filtered, excluded, mode=state.avg_or_median)

    teammate_df = ctx.get("teammate_df", pd.DataFrame())
    if not teammate_df.empty:
        st.divider()
        st.markdown("##### Teammate WITH / WITHOUT")
        st.caption(
            "Click a teammate to filter the sample to games WITH or WITHOUT them. "
            "Δ shows the filtered-sample stat difference (with − without) for this market."
        )
        ui_layout.render_teammate_row(
            teammate_df,
            diffs_by_pid=ctx.get("teammate_diffs", {}),
            state=state,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


_APP_MODES = ("NBA Player Props", "WNBA Player Props", "Game Markets", "CS2")


def _render_mode_selector() -> str:
    """Top-level mode switcher. Sits above the numbered player-prop sections.

    Held in session state as ``_app_mode`` so the choice survives reruns
    without being reset when the rest of the sidebar rerenders.
    """
    st.sidebar.markdown("### Mode")
    current = st.session_state.get("_app_mode", _APP_MODES[0])
    if current == "Player Props":
        # Renamed when WNBA mode was added; migrate legacy session state.
        current = "NBA Player Props"
        st.session_state["_app_mode"] = current
    try:
        idx = _APP_MODES.index(current)
    except ValueError:
        idx = 0
    mode = st.sidebar.radio(
        "Research mode", _APP_MODES, index=idx,
        key="_app_mode",
        label_visibility="collapsed",
        help=("NBA Player Props = NBA per-player research. "
              "WNBA Player Props = WNBA (League 10) — same UI, separate data. "
              "Game Markets = NBA matchup totals, team totals, and spreads. "
              "CS2 = Counter-Strike 2 research (PandaScore)."),
    )
    st.sidebar.divider()
    return mode


def main() -> None:
    mode = _render_mode_selector()

    if mode == "Game Markets":
        # Game Markets uses its own sidebar section (nothing needed here)
        # and renders its own page. The player-props sidebar is intentionally
        # skipped so the UI stays uncluttered when the user is in this mode.
        render_game_markets_page()
        return

    if mode == "CS2":
        # Fully separate product area — own services/cs2 namespace, own
        # filters, own cache namespace. Renders its own sidebar section.
        render_cs2_page()
        return

    if mode == "WNBA Player Props":
        from ui.wnba_props_page import run_wnba_player_props

        run_wnba_player_props()
        return

    # --- NBA Player Props (default research experience) --------------------
    st.session_state["_props_league"] = "nba"

    sidebar = render_sidebar()
    st.title("NBA Player Prop Research")

    player = sidebar["player"]
    if not player:
        st.info("Search for a player in the sidebar to begin.")
        return

    # --- Fetch the data ------------------------------------------------------
    spinner_msg = (f"Loading game log for {player['name']} "
                   f"({sidebar['season']} · {sidebar['season_type_label']})...")
    with st.spinner(spinner_msg):
        try:
            game_log = load_game_log(player["id"], sidebar["season"], sidebar["season_type"])
        except Exception as exc:
            logger.exception("game log load failed")
            st.error(f"Could not load game log: {exc}")
            game_log = pd.DataFrame()

    # --- Lazy quarter-stat loading (only when a Q1/H1 stat is selected) ------
    if sidebar["stat_col"] in QUARTER_STAT_COLS and not game_log.empty:
        already_cached = quarter_service.quarter_stats_are_cached(game_log)
        if not already_cached:
            total_games = len(game_log)
            st.info(
                f"First-time load of 1Q/1H stats for {player['name']} — "
                f"up to {total_games * 2} play-by-play + roster lookups to stats.nba.com "
                "(often several minutes for a full season). "
                "Results are cached permanently in SQLite; revisits load quickly from disk."
            )
            progress_bar = st.progress(0, text="Fetching quarter stats…")

            def _update_progress(done: int, total: int) -> None:
                pct = min(done / max(total, 1), 1.0)
                progress_bar.progress(pct, text=f"1Q/1H stats: {done}/{total}")

            try:
                game_log = quarter_service.enrich_with_quarter_stats(
                    game_log, player["id"], progress_callback=_update_progress
                )
            except Exception as exc:
                logger.warning("quarter stats load failed: %s", exc)
                st.warning(f"Could not load quarter stats: {exc}")
            finally:
                progress_bar.empty()
        else:
            with st.spinner("Loading quarter stats from cache…"):
                try:
                    game_log = quarter_service.enrich_with_quarter_stats(
                        game_log, player["id"], progress_callback=None
                    )
                except Exception as exc:
                    logger.warning("quarter stats cache read failed: %s", exc)
                    st.warning(f"Could not load quarter stats: {exc}")

    player_info = load_player_info(player["id"]) or {}
    team_id = player_info.get("team_id")

    next_opp_info = load_next_opponent(team_id, sidebar["season"])
    opponent_desc = _derive_next_opponent(game_log, next_opp_info)

    # Matchup factor computed for the relevant base column.
    base_col_for_matchup = _stat_base_column(sidebar["stat_col"])
    matchup_info = None
    if sidebar["apply_matchup"] and base_col_for_matchup and opponent_desc.get("team_id"):
        try:
            matchup_info = load_matchup_info(opponent_desc["team_id"], base_col_for_matchup, sidebar["season"])
        except Exception as exc:
            logger.warning("matchup info fetch failed: %s", exc)

    # --- Projection ----------------------------------------------------------
    projection = project(
        game_log,
        sidebar["stat_col"],
        weights=sidebar["weights"],
        is_home_next=opponent_desc.get("is_home"),
        matchup_info=matchup_info,
        projected_minutes=sidebar["projected_minutes"],
        apply_matchup=sidebar["apply_matchup"],
        apply_home_away=sidebar["apply_home_away"],
        apply_minutes=sidebar["apply_minutes"],
    )
    edge = compute_edge(projection.final, sidebar["manual_line"])
    l10_hit = hit_rate(game_log, sidebar["stat_col"], sidebar["hit_threshold"], last_n=10)

    # --- Filter pipeline -----------------------------------------------------
    filter_state = get_filter_state()
    opp_abbr = opponent_desc.get("team_abbreviation") if opponent_desc else None
    opp_team_id = opponent_desc.get("team_id") if opponent_desc else None

    rank_maps = load_rank_maps(sidebar["season"], sidebar["season_type"])
    teammate_df = load_teammate_roster(team_id, sidebar["season"],
                                       exclude_player_id=player["id"])

    # Rich teammate table used by the Filter-by-Teammate modal. Lazily
    # built — the cold path pulls one game log per teammate, but every
    # call is SQLite-cached so reopens are fast. market_col is NOT part of
    # the cache key; STAT_AVGS inside the table covers all markets.
    teammate_summary = load_teammate_summary_table(
        team_id,
        sidebar["season"],
        sidebar["season_type"],
        player["id"],
    )

    # Only fetch game-id sets for teammates the user has actually toggled;
    # each extra fetch is a full game-log call and we want the cold path
    # to stay snappy.
    teammate_gid_map: dict[int, set[str]] = {}
    for pid in list(filter_state.teammate_modes.keys()):
        teammate_gid_map[int(pid)] = load_teammate_game_ids(
            int(pid), sidebar["season"], sidebar["season_type"]
        )

    filter_result = filter_service.apply_all_filters(
        game_log,
        filter_state,
        opponent_abbr=opp_abbr,
        teammate_game_ids=teammate_gid_map,
        opp_rank_maps=rank_maps,
    )
    filtered_log = filter_result.filtered
    excluded_log = filter_result.excluded

    # Pre-compute teammate diffs (stat_col avg WITH minus WITHOUT) only for
    # teammates that already have their GAME_IDS available in the summary
    # table (populated from the cached gamelog) or in teammate_gid_map
    # (from toggled filters). We never make a fresh network call here —
    # diffing is a purely in-memory operation on already-cached data.
    import math as _math
    teammate_diffs: dict[int, float | None] = {}
    _summary_for_diff = teammate_summary if (
        teammate_summary is not None and not teammate_summary.empty
    ) else teammate_df
    if not _summary_for_diff.empty:
        for _, row in _summary_for_diff.head(6).iterrows():
            pid = int(row.get("PLAYER_ID", 0))
            # Prefer game-id set from the already-loaded summary table so we
            # don't trigger any extra fetches just to render the summary bar.
            gid_set = (
                teammate_gid_map.get(pid)
                or (row.get("GAME_IDS") if isinstance(row.get("GAME_IDS"), set) else None)
            )
            if not gid_set:
                teammate_diffs[pid] = None
                continue
            with_df = filter_service.apply_teammate_filter(
                game_log, {pid: gid_set}, {pid: "with"}
            )
            without_df = filter_service.apply_teammate_filter(
                game_log, {pid: gid_set}, {pid: "without"}
            )
            a = compute_graph_avg(with_df, sidebar["stat_col"], mode=filter_state.avg_or_median)
            b = compute_graph_avg(without_df, sidebar["stat_col"], mode=filter_state.avg_or_median)
            if _math.isnan(a) or _math.isnan(b):
                teammate_diffs[pid] = None
            else:
                teammate_diffs[pid] = a - b

    ctx = {
        **sidebar,
        "player_id": int(player["id"]),
        "game_log": filtered_log,          # existing tabs become filter-aware
        "full_game_log": game_log,          # unfiltered baseline
        "excluded_log": excluded_log,
        "narrowed_total": filter_result.narrowed_total,
        "opponent_desc": opponent_desc,
        "matchup_info": matchup_info,
        "projection": projection,
        "edge": edge,
        "hit_rate_l10": l10_hit,
        "teammate_df": teammate_summary if teammate_summary is not None and not teammate_summary.empty else teammate_df,
        "teammate_roster": teammate_df,
        "teammate_summary": teammate_summary,
        "teammate_diffs": teammate_diffs,
        "rank_maps": rank_maps,
    }

    # --- Top bar + modal -----------------------------------------------------
    open_filters = ui_layout.render_top_bar(
        player_name=player["name"],
        team_abbr=(player_info or {}).get("team_abbreviation") or "",
        market_label=sidebar["stat_label"],
        opponent_abbr=opp_abbr or "",
    )

    active_chips = filter_state.active_chips()
    ui_layout.render_active_filter_strip(active_chips)

    # `st.dialog` is only available on Streamlit >= 1.32. We declare it
    # inline so Streamlit wires the click handler correctly.
    @st.dialog("Filters", width="large")  # type: ignore[misc]
    def _filters_dialog() -> None:
        filter_modal_module.render_filter_modal(
            market_col=sidebar["stat_col"],
            opponent_team_id=opp_team_id,
            opponent_abbr=opp_abbr,
            teammate_df=teammate_df,
            season=sidebar["season"],
            season_type=sidebar["season_type"],
            rank_maps=rank_maps,
        )

    if open_filters:
        _filters_dialog()

    # Dedicated Filter-by-Teammate modal. Kept separate from the main
    # Filters dialog so the scan flow stays compact. The teammate summary
    # bar in the Research view sets `_open_tm_modal` on button click.
    _team_dn = ""
    _pi = player_info or {}
    _city = (_pi.get("team_city") or "").strip()
    _tname = (_pi.get("team_name") or "").strip()
    if _city and _tname:
        _team_dn = f"{_city} {_tname}"
    elif _tname:
        _team_dn = _tname
    else:
        _team_dn = (_pi.get("team_abbreviation") or "").strip()

    @st.dialog("Filter by Teammate", width="large")  # type: ignore[misc]
    def _teammate_dialog() -> None:
        teammate_modal_module.render_teammate_filter_modal(
            teammate_summary,
            filter_state,
            market_col=sidebar["stat_col"],
            market_label=sidebar["stat_label"],
            season_api=sidebar["season"],
            season_ui=compact_season_label(sidebar["season"]),
            season_type=sidebar["season_type"],
            team_abbr=(_pi.get("team_abbreviation") or ""),
            team_display_name=_team_dn,
            team_id=team_id,
            subject_player_id=int(player["id"]),
            subject_player_name=str(player["name"]),
            subject_position=_pi.get("position"),
        )

    if st.session_state.pop("_open_tm_modal", False):
        _teammate_dialog()

    # --- Tabs (Research is now the default leftmost tab) --------------------
    tab_research, tab_overview, tab_logs, tab_splits, tab_trends, tab_proj = st.tabs([
        "Research", "Overview", "Game Logs", "Splits", "Trends", "Projection Details",
    ])

    with tab_research:
        render_research(ctx)
    with tab_overview:
        render_overview(ctx)
    with tab_logs:
        render_game_logs(ctx)
    with tab_splits:
        render_splits(ctx)
    with tab_trends:
        render_trends(ctx)
    with tab_proj:
        render_projection_details(ctx)


if __name__ == "__main__":
    main()
