"""WNBA player prop research — parallel UX to NBA (``app.py``).

Uses stats.nba.com **LeagueID 10**, calendar-year season strings (``\"2025\"``),
and separate session filter state (``filter_state_wnba`` via
``_props_league == \"wnba\"``).

Tab renderers are shared with NBA by lazy-importing ``app`` inside
``run_wnba_player_props`` to avoid import cycles.
"""

from __future__ import annotations

import logging
import math

import pandas as pd
import streamlit as st

from config import (
    DEFAULT_ADJUSTMENTS,
    DEFAULT_SEASON_TYPE_LABEL,
    DEFAULT_WEIGHTS,
    SEASON_TYPES,
    STAT_OVERLAY_CHIPS,
)
from league_config import wnba as wnba_cfg
from database import cache_db
from models.hit_rate import hit_rate
from models.metrics import compute_graph_avg, compute_hit_rate, compute_season_avg
from models.projection import compute_edge, project
from models.stats import add_derived_stats, resolve_stat_column
from services import (
    defense_service,
    filter_service,
    gamelog_service,
    player_service,
    rankings_service,
    schedule_service,
    teammate_service,
)
from services.nba_client import reset_circuit_breaker
from services.wnba import LEAGUE_ID
from ui import filter_modal as filter_modal_module
from ui import layout as ui_layout
from ui import teammate_modal as teammate_modal_module
from ui.filter_state import FilterState, get_state as get_filter_state
from ui.graph import render_research_graph
from utils.formatting import compact_season_label
from utils.wnba_logos import wnba_team_logo_url_png

logger = logging.getLogger(__name__)

_MATCHUP_SUPPORTED = {"PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV"}
_DERIVED_MATCHUP_PROXY = {
    "PRA": "PTS",
    "PA": "PTS",
    "RA": "REB",
    "PR": "PTS",
    "Stocks": "STL",
}


def _stat_base_column(stat_col: str) -> str | None:
    if stat_col in _DERIVED_MATCHUP_PROXY:
        return _DERIVED_MATCHUP_PROXY[stat_col]
    return stat_col if stat_col in _MATCHUP_SUPPORTED else None


def _derive_next_opponent(
    gl: pd.DataFrame,
    next_opp_info: dict | None,
) -> dict:
    if next_opp_info and next_opp_info.get("opponent_team_id"):
        info = schedule_service.resolve_opponent_team(
            next_opp_info["opponent_team_id"],
            league_id=LEAGUE_ID,
        ) or {}
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
        info = schedule_service.resolve_opponent_by_abbr(abbr, league_id=LEAGUE_ID) or {}
        return {
            "source": "last_game",
            "is_home": bool(gl.iloc[0].get("IS_HOME", False)),
            "team_id": info.get("team_id"),
            "team_abbreviation": abbr,
            "team_name": info.get("team_name", ""),
            "note": "Showing last game's opponent (no confirmed next game in today's schedule).",
        }

    return {
        "source": "none",
        "is_home": None,
        "team_id": None,
        "team_abbreviation": "",
        "team_name": "",
        "note": "No opponent info available.",
    }


# ---------------------------------------------------------------------------
# Cached loaders (Streamlit)
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False, ttl=60 * 60)
def wnba_load_player_search(query: str, active_only: bool) -> pd.DataFrame:
    return player_service.search_players(query, active_only, league="wnba")


@st.cache_data(show_spinner=False, ttl=60 * 60)
def wnba_load_player_info(player_id: int) -> dict | None:
    """CommonPlayerInfo for WNBA — ``season`` string reserved for callers."""
    return player_service.get_player_team_for_season(
        player_id,
        str(wnba_cfg.current_wnba_season_year()),
        league_id=LEAGUE_ID,
    )


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def wnba_load_game_log(
    player_id: int,
    season: str,
    season_type: str,
) -> pd.DataFrame:
    raw = gamelog_service.fetch_player_game_log(
        player_id,
        season,
        season_type=season_type,
        league_id=LEAGUE_ID,
    )
    return add_derived_stats(
        raw,
        derived_stats=wnba_cfg.WNBA_DERIVED_STATS,
        threshold_stats={},
    )


@st.cache_data(show_spinner=False, ttl=60 * 60)
def wnba_load_next_opponent(team_id: int | None, season: str) -> dict | None:
    return schedule_service.get_next_opponent(
        team_id, season, league_id=LEAGUE_ID,
    )


@st.cache_data(show_spinner=False, ttl=12 * 60 * 60)
def wnba_load_matchup_info(
    opponent_team_id: int | None,
    stat_base_col: str,
    season: str,
) -> dict | None:
    if not opponent_team_id or not stat_base_col:
        return None
    return defense_service.matchup_factor(
        opponent_team_id, stat_base_col, season, league_id=LEAGUE_ID,
    )


@st.cache_data(show_spinner=False, ttl=12 * 60 * 60)
def wnba_load_rank_maps(season: str, season_type: str) -> dict[str, dict[int, int]]:
    try:
        return rankings_service.build_chip_rank_maps(
            season, season_type=season_type, league_id=LEAGUE_ID,
        )
    except Exception as exc:
        logger.warning("wnba rank-map build failed: %s", exc)
        return {}


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def wnba_load_teammate_roster(
    team_id: int | None,
    season: str,
    exclude_player_id: int | None,
) -> pd.DataFrame:
    try:
        return teammate_service.list_teammates(
            team_id, season,
            exclude_player_id=exclude_player_id,
            league_id=LEAGUE_ID,
        )
    except Exception as exc:
        logger.warning("wnba teammate roster load failed: %s", exc)
        return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def wnba_load_teammate_game_ids(
    teammate_id: int,
    season: str,
    season_type: str,
) -> set[str]:
    try:
        return teammate_service.teammate_game_ids(
            teammate_id, season,
            season_type=season_type,
            league_id=LEAGUE_ID,
        )
    except Exception as exc:
        logger.warning("wnba teammate game ids fetch failed for %s: %s", teammate_id, exc)
        return set()


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def wnba_load_teammate_summary_table(
    team_id: int | None,
    season: str,
    season_type: str,
    exclude_player_id: int | None,
) -> pd.DataFrame:
    try:
        return teammate_service.build_teammate_table(
            team_id, season, season_type, exclude_player_id,
            league_id=LEAGUE_ID,
        )
    except Exception as exc:
        logger.warning("wnba teammate summary table load failed: %s", exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def render_sidebar_wnba() -> dict:
    st.sidebar.title("WNBA Prop Research")
    st.sidebar.caption("Same tools as NBA — powered by WNBA stats.nba.com data.")

    st.sidebar.subheader("1. Player")
    query = st.sidebar.text_input(
        "Search player",
        value="",
        key="wnba_player_query",
        help="Search WNBA players (static nba_api list + League 10).",
    )
    active_only = st.sidebar.checkbox(
        "Active players only", value=True, key="wnba_active_only",
    )

    results = (
        wnba_load_player_search(query, active_only)
        if query and len(query) >= 2 else pd.DataFrame()
    )
    selected_player = None
    if results.empty:
        st.sidebar.info("Type at least 2 characters to search a WNBA player.")
    else:
        options = {
            f"{row['full_name']} ({'active' if row['is_active'] else 'historical'})": int(row["id"])
            for _, row in results.iterrows()
        }
        label = st.sidebar.selectbox("Pick player", list(options.keys()), key="wnba_pick_player")
        selected_player = {"id": options[label], "name": label.rsplit(" (", 1)[0]}

    st.sidebar.subheader("2. Season & Stat")
    seasons = wnba_cfg.wnba_season_strings_recent(8)
    season = st.sidebar.selectbox(
        "Season",
        seasons,
        index=0,
        key="wnba_season_pick",
        help="WNBA uses a single calendar year label (e.g. 2025) on stats.nba.com.",
    )

    season_type_labels = list(SEASON_TYPES.keys())
    default_type_idx = season_type_labels.index(DEFAULT_SEASON_TYPE_LABEL)
    season_type_label = st.sidebar.selectbox(
        "Season type", season_type_labels,
        index=default_type_idx,
        key="wnba_season_type",
    )
    season_type = SEASON_TYPES[season_type_label]

    stat_keys = list(wnba_cfg.WNBA_STAT_COLUMNS.keys())
    stat_label = st.sidebar.selectbox(
        "Stat",
        stat_keys,
        index=stat_keys.index("Points"),
        key="wnba_stat_label",
    )
    stat_col = resolve_stat_column(
        stat_label, stat_columns=wnba_cfg.WNBA_STAT_COLUMNS,
    )
    _is_threshold = stat_col in wnba_cfg.WNBA_THRESHOLD_STATS
    _default_line = 0.5 if _is_threshold else 18.5

    if st.session_state.get("_wnba_prop_line_stat") != stat_col:
        st.session_state["_wnba_prop_line_stat"] = stat_col
        st.session_state["_wnba_prop_line_value"] = _default_line

    st.sidebar.subheader("3. Prop Line")
    manual_line = st.sidebar.number_input(
        "Posted line (you type this)",
        min_value=0.0, max_value=150.0,
        value=float(st.session_state.get("_wnba_prop_line_value", _default_line)),
        step=0.5, format="%.1f",
        key="_wnba_prop_line_widget",
    )
    st.session_state["_wnba_prop_line_value"] = manual_line
    hit_threshold = manual_line

    st.sidebar.subheader("4. Projection")
    w5 = st.sidebar.slider("Weight: Last 5", 0.0, 1.0, float(DEFAULT_WEIGHTS["w5"]), 0.05, key="wnba_w5")
    w10 = st.sidebar.slider("Weight: Last 10", 0.0, 1.0, float(DEFAULT_WEIGHTS["w10"]), 0.05, key="wnba_w10")
    wSeason = st.sidebar.slider("Weight: Season", 0.0, 1.0, float(DEFAULT_WEIGHTS["wSeason"]), 0.05, key="wnba_ws")

    apply_matchup = st.sidebar.checkbox(
        "Apply matchup adjustment", value=DEFAULT_ADJUSTMENTS["matchup"], key="wnba_matchup",
    )
    apply_home_away = st.sidebar.checkbox(
        "Apply home/away adjustment", value=DEFAULT_ADJUSTMENTS["home_away"], key="wnba_ha",
    )
    apply_minutes = st.sidebar.checkbox(
        "Apply minutes adjustment", value=DEFAULT_ADJUSTMENTS["minutes"], key="wnba_min",
    )
    projected_minutes_override = None
    if apply_minutes:
        projected_minutes_override = st.sidebar.number_input(
            "Projected minutes (optional, blank = L5 mean)",
            min_value=0.0, max_value=60.0, value=0.0, step=0.5, format="%.1f",
            key="wnba_proj_min",
            help="0 means 'use last-5 average'.",
        )
        if projected_minutes_override == 0:
            projected_minutes_override = None

    st.sidebar.subheader("5. Data")
    if st.sidebar.button("Refresh data (clear cache)", key="wnba_refresh"):
        if selected_player:
            gamelog_service.invalidate_player_cache(
                selected_player["id"], season, league_id=LEAGUE_ID,
            )
        cache_db.clear_namespace("schedule")
        cache_db.clear_namespace("defense")
        cache_db.clear_namespace("rankings")
        reset_circuit_breaker()
        wnba_load_player_search.clear()
        wnba_load_player_info.clear()
        wnba_load_game_log.clear()
        wnba_load_next_opponent.clear()
        wnba_load_matchup_info.clear()
        wnba_load_rank_maps.clear()
        wnba_load_teammate_roster.clear()
        wnba_load_teammate_game_ids.clear()
        wnba_load_teammate_summary_table.clear()
        st.sidebar.success("WNBA cache cleared. Data will re-fetch.")
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
        "is_threshold_stat": _is_threshold,
        "weights": {"w5": w5, "w10": w10, "wSeason": wSeason},
        "apply_matchup": apply_matchup,
        "apply_home_away": apply_home_away,
        "apply_minutes": apply_minutes,
        "projected_minutes": projected_minutes_override,
    }


def run_wnba_player_props() -> None:
    import app as app_main  # noqa: WPS433 — lazy import; tabs shared with NBA page

    st.session_state["_props_league"] = "wnba"
    if "filter_state_wnba" not in st.session_state:
        st.session_state["filter_state_wnba"] = FilterState(
            season_selection=wnba_cfg.default_wnba_filter_season_label(),
        )

    st.title("WNBA Player Prop Research")

    sidebar = render_sidebar_wnba()
    player = sidebar["player"]
    if not player:
        st.info("Search for a WNBA player in the sidebar to begin.")
        return

    spinner_msg = (
        f"Loading WNBA game log for {player['name']} "
        f"({sidebar['season']} · {sidebar['season_type_label']})..."
    )
    with st.spinner(spinner_msg):
        try:
            game_log = wnba_load_game_log(
                player["id"], sidebar["season"], sidebar["season_type"],
            )
        except Exception as exc:
            logger.exception("wnba game log load failed")
            st.error(f"Could not load game log: {exc}")
            game_log = pd.DataFrame()

    player_info = wnba_load_player_info(player["id"]) or {}
    team_id = player_info.get("team_id")

    next_opp_info = wnba_load_next_opponent(team_id, sidebar["season"])
    opponent_desc = _derive_next_opponent(game_log, next_opp_info)

    base_col_for_matchup = _stat_base_column(sidebar["stat_col"])
    matchup_info = None
    if sidebar["apply_matchup"] and base_col_for_matchup and opponent_desc.get("team_id"):
        try:
            matchup_info = wnba_load_matchup_info(
                opponent_desc["team_id"],
                base_col_for_matchup,
                sidebar["season"],
            )
        except Exception as exc:
            logger.warning("wnba matchup info fetch failed: %s", exc)

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
    l10_hit = hit_rate(
        game_log, sidebar["stat_col"], sidebar["hit_threshold"], last_n=10,
    )

    filter_state = get_filter_state()
    opp_abbr = opponent_desc.get("team_abbreviation") if opponent_desc else None
    opp_team_id = opponent_desc.get("team_id") if opponent_desc else None

    rank_maps = wnba_load_rank_maps(sidebar["season"], sidebar["season_type"])
    teammate_df = wnba_load_teammate_roster(
        team_id, sidebar["season"], exclude_player_id=player["id"],
    )
    teammate_summary = wnba_load_teammate_summary_table(
        team_id,
        sidebar["season"],
        sidebar["season_type"],
        player["id"],
    )

    teammate_gid_map: dict[int, set[str]] = {}
    for pid in list(filter_state.teammate_modes.keys()):
        teammate_gid_map[int(pid)] = wnba_load_teammate_game_ids(
            int(pid), sidebar["season"], sidebar["season_type"],
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

    import math as _math
    teammate_diffs: dict[int, float | None] = {}
    _summary_for_diff = (
        teammate_summary
        if teammate_summary is not None and not teammate_summary.empty
        else teammate_df
    )
    if not _summary_for_diff.empty:
        for _, row in _summary_for_diff.head(6).iterrows():
            pid = int(row.get("PLAYER_ID", 0))
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
        "game_log": filtered_log,
        "full_game_log": game_log,
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

    open_filters = ui_layout.render_top_bar(
        player_name=player["name"],
        team_abbr=(player_info or {}).get("team_abbreviation") or "",
        market_label=sidebar["stat_label"],
        opponent_abbr=opp_abbr or "",
        on_open_filters_key="open-filters-wnba",
    )

    active_chips = filter_state.active_chips()
    ui_layout.render_active_filter_strip(active_chips)

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
            season_selector_options=wnba_cfg.WNBA_SEASON_SELECTOR_OPTIONS,
            league_id=LEAGUE_ID,
        )

    if open_filters:
        _filters_dialog()

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
            league_id=LEAGUE_ID,
            logo_url_fn=wnba_team_logo_url_png,
        )

    if st.session_state.pop("_open_tm_modal", False):
        _teammate_dialog()

    tab_research, tab_overview, tab_logs, tab_splits, tab_trends, tab_proj = st.tabs([
        "Research", "Overview", "Game Logs", "Splits", "Trends", "Projection Details",
    ])

    with tab_research:
        app_main.render_research(ctx)
    with tab_overview:
        app_main.render_overview(ctx)
    with tab_logs:
        app_main.render_game_logs(ctx)
    with tab_splits:
        app_main.render_splits(ctx)
    with tab_trends:
        app_main.render_trends(ctx)
    with tab_proj:
        app_main.render_projection_details(ctx)
