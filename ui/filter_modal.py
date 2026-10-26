"""The Filters modal with four tabs: Suggested, Opp Rankings, Splits, Stats.

Opened from the top bar via `st.dialog`. Each tab reads from and writes
to the shared `FilterState` in session state. The modal is a thin view
layer — every piece of interpretation/data lookup lives in the services.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from config import (
    DEFENSIVE_TIMEFRAMES,
    GAME_COUNT_RADIO_OPTIONS,
    OPP_RANK_SECTIONS,
    SEASON_SELECTOR_OPTIONS,
    SPLITS_CHIPS,
    STAT_OVERLAY_CHIPS,
)
from services import suggestion_service

from .filter_state import FilterState, get_state


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_filter_modal(*, market_col: str,
                        opponent_team_id: int | None,
                        opponent_abbr: str | None,
                        teammate_df: pd.DataFrame,
                        season: str,
                        season_type: str,
                        rank_maps: dict[str, dict[int, int]],
                        season_selector_options: tuple[str, ...] | None = None,
                        league_id: str = "00") -> None:
    """Render the modal body. Must be called from inside a `@st.dialog`
    function because it uses the Streamlit component-tree semantics."""

    state = get_state()

    # --- Season & Games (top strip, always visible) -----------------------
    opts = season_selector_options or SEASON_SELECTOR_OPTIONS
    _render_season_and_games(state, season_selector_options=opts)

    st.divider()

    tab_sug, tab_opp, tab_splits, tab_stats, tab_help = st.tabs(
        ["Suggested", "Opp Rankings", "Splits", "Stats", "Help"]
    )

    with tab_sug:
        _render_suggested_tab(
            state, market_col, opponent_team_id, season, season_type,
            league_id=league_id,
        )
    with tab_opp:
        _render_opp_rankings_tab(state, rank_maps)
    with tab_splits:
        _render_splits_tab(state, opponent_abbr)
    with tab_stats:
        _render_stats_tab(state)
    with tab_help:
        _render_help_tab()

    st.divider()
    _render_footer(state)


# ---------------------------------------------------------------------------
# Section: season & games
# ---------------------------------------------------------------------------


def _render_season_and_games(
    state: FilterState,
    *,
    season_selector_options: tuple[str, ...],
) -> None:
    st.markdown("##### Season & Games")
    col1, col2, col3, col4 = st.columns([1.2, 1.2, 1.5, 1])

    with col1:
        idx = season_selector_options.index(state.season_selection) \
            if state.season_selection in season_selector_options else 0
        state.season_selection = st.radio(
            "Season", season_selector_options, index=idx,
            horizontal=True, key="fm-season",
            help="'All' merges the three most recent seasons. Others match "
                 "the nba_api season label.",
        )

    with col2:
        labels = tuple(lbl for lbl, _ in GAME_COUNT_RADIO_OPTIONS)
        code_from_label = dict(GAME_COUNT_RADIO_OPTIONS)
        label_from_code = {c: lbl for lbl, c in GAME_COUNT_RADIO_OPTIONS}

        gm = state.game_count_mode
        if gm not in label_from_code:
            gm = "30"
            state.game_count_mode = gm
        ix = labels.index(label_from_code[gm])
        pick = st.radio(
            "Games", labels, index=ix,
            horizontal=True, key="fm-games",
            help="Caps the filtered sample (and Research bar chart) to the youngest N "
                 "played games — applied **after** every other filter. "
                 "**Max** uses the full narrowed sample.",
        )
        state.game_count_mode = code_from_label[pick]

    with col3:
        if state.game_count_mode == "Custom":
            state.game_count_custom_n = int(st.number_input(
                "Custom last N", min_value=1, max_value=200,
                value=int(state.game_count_custom_n), step=1,
                key="fm-custom-n",
            ))
        else:
            st.caption("Custom N only active when 'Custom' is selected.")

    with col4:
        state.lock_game_count = st.checkbox(
            "Lock", value=bool(state.lock_game_count), key="fm-lock",
            help="Keep the selected game count and season when switching players.",
        )


# ---------------------------------------------------------------------------
# Section: Suggested
# ---------------------------------------------------------------------------


def _render_suggested_tab(
    state: FilterState,
    market_col: str,
    opponent_team_id: int | None,
    season: str,
    season_type: str,
    *,
    league_id: str = "00",
) -> None:
    st.caption(
        "Rule-based suggestions for this market. Green = favourable matchup, "
        "red = tough matchup. Click a chip to toggle the underlying filter."
    )

    chips = suggestion_service.build_suggested(
        market_col=market_col,
        game_log=pd.DataFrame(),  # not needed for current rules
        opponent_team_id=opponent_team_id,
        season=season,
        season_type=season_type,
        league_id=league_id,
    )

    cols_per_row = 3
    rows = [chips[i:i + cols_per_row] for i in range(0, len(chips), cols_per_row)]
    for row_chips in rows:
        cols = st.columns(cols_per_row)
        for col, chip in zip(cols, row_chips):
            with col:
                _render_suggested_chip(state, chip)


def _render_suggested_chip(state: FilterState, chip: Any) -> None:
    is_beta = chip.status == "beta"
    is_on = _suggested_chip_is_on(state, chip.key)

    prefix = "● " if is_on else "○ "
    badge = f"  ({chip.badge})" if chip.badge else ""
    beta = "  · beta" if is_beta else ""
    button_label = f"{prefix}{chip.label}{badge}{beta}"
    clicked = st.button(button_label, key=f"sug-{chip.key}",
                        help=chip.reason, use_container_width=True)
    if clicked:
        _toggle_suggested_chip(state, chip)


def _suggested_chip_is_on(state: FilterState, key: str) -> bool:
    if key == "minutes_high":
        return ("MIN" in state.stat_ranges)
    if key == "home_away":
        return state.home_away is not None
    if key == "h2h":
        return state.h2h_only
    if key == "b2b":
        return state.b2b_only
    if key.startswith("opp_") and key.endswith("_rank"):
        return bool(state.opp_rank_active.get(key))
    return bool(state.beta_chips_active.get(key))


def _toggle_suggested_chip(state: FilterState, chip: Any) -> None:
    key = chip.key
    if chip.status == "beta":
        state.beta_chips_active[key] = not state.beta_chips_active.get(key, False)
        return

    if key == "minutes_high":
        if "MIN" in state.stat_ranges:
            state.stat_ranges.pop("MIN", None)
        else:
            # We can't compute L10 avg here without the game log; UI sets
            # a placeholder that the layout layer can refine on render.
            state.stat_ranges["MIN"] = (30.0, None)
    elif key == "home_away":
        state.home_away = None if state.home_away else "home"
    elif key == "h2h":
        state.h2h_only = not state.h2h_only
    elif key == "b2b":
        state.b2b_only = not state.b2b_only
    elif key.startswith("opp_") and key.endswith("_rank"):
        state.opp_rank_active[key] = not state.opp_rank_active.get(key, False)


# ---------------------------------------------------------------------------
# Section: Opp Rankings
# ---------------------------------------------------------------------------


def _render_opp_rankings_tab(state: FilterState,
                             rank_maps: dict[str, dict[int, int]]) -> None:
    tf_idx = DEFENSIVE_TIMEFRAMES.index(state.opp_rank_timeframe) \
        if state.opp_rank_timeframe in DEFENSIVE_TIMEFRAMES else 0
    state.opp_rank_timeframe = st.radio(
        "Timeframe", DEFENSIVE_TIMEFRAMES, index=tf_idx,
        horizontal=True, key="fm-tf",
        help="MVP uses season-long ranks regardless of selection; L5/L10/L15 "
             "labels are present to show the future-work surface.",
    )

    for section_name, chips in OPP_RANK_SECTIONS.items():
        with st.expander(section_name, expanded=(section_name == "Team Defense")):
            cols = st.columns(3)
            for i, chip_spec in enumerate(chips):
                with cols[i % 3]:
                    _render_opp_chip(state, chip_spec, rank_maps)


def _render_opp_chip(state: FilterState, chip_spec: dict,
                     rank_maps: dict[str, dict[int, int]]) -> None:
    key = chip_spec["key"]
    label = chip_spec["label"]
    is_beta = chip_spec.get("status") != "real"
    is_on = (
        state.beta_chips_active.get(key, False) if is_beta
        else state.opp_rank_active.get(key, False)
    )
    has_data = key in rank_maps and len(rank_maps.get(key) or {}) > 0
    badge = "beta" if is_beta else ("no data" if not has_data else "")
    prefix = "● " if is_on else "○ "
    full = f"{prefix}{label}"
    if badge:
        full += f"  · {badge}"
    clicked = st.button(full, key=f"opp-{key}", use_container_width=True,
                        help=("Narrow to games vs. teams ranked top-10 easiest "
                              "for this stat." if not is_beta else
                              "Beta: requires data not reliably available from "
                              "free nba_api endpoints. Toggling has no effect."))
    if clicked:
        if is_beta:
            state.beta_chips_active[key] = not is_on
        else:
            state.opp_rank_active[key] = not is_on


# ---------------------------------------------------------------------------
# Section: Splits
# ---------------------------------------------------------------------------


def _render_splits_tab(state: FilterState, opponent_abbr: str | None) -> None:
    st.caption(
        "Classic sample splits. Mutually exclusive options (Home/Away, "
        "Win/Loss, Regular/Playoffs) act as three-way toggles."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        state.home_away = _tri_state(
            "Location", ["home", "away"], state.home_away, "fm-loc"
        )
    with col2:
        state.win_loss = _tri_state(
            "Result", ["win", "loss"], state.win_loss, "fm-wl"
        )
    with col3:
        state.regular_playoff = _tri_state(
            "Phase", ["regular", "playoffs"], state.regular_playoff, "fm-phase"
        )

    st.markdown(" ")
    col4, col5, col6 = st.columns(3)
    with col4:
        state.b2b_only = st.checkbox(
            "B2B only", value=state.b2b_only, key="fm-b2b",
            help="Keep only games where REST_DAYS <= 1."
        )
    with col5:
        state.h2h_only = st.checkbox(
            f"H2H vs {opponent_abbr or 'current opp'}",
            value=state.h2h_only, key="fm-h2h",
            disabled=not opponent_abbr,
        )
    with col6:
        state.margin_filter = _tri_state(
            "Margin", ["close", "blowout"], state.margin_filter, "fm-margin",
            labels={"close": "Close (±5)", "blowout": "Blowout (>15)"},
        )

    st.markdown("##### Rest days")
    col7, col8 = st.columns(2)
    with col7:
        rmin_input = st.number_input(
            "Min rest (days, leave -1 for no min)",
            min_value=-1, max_value=14,
            value=state.rest_min if state.rest_min is not None else -1,
            step=1, key="fm-rest-min",
        )
        state.rest_min = None if rmin_input < 0 else int(rmin_input)
    with col8:
        rmax_input = st.number_input(
            "Max rest (days, leave -1 for no max)",
            min_value=-1, max_value=14,
            value=state.rest_max if state.rest_max is not None else -1,
            step=1, key="fm-rest-max",
        )
        state.rest_max = None if rmax_input < 0 else int(rmax_input)

    st.markdown("##### Other splits")
    beta_chips = [c for c in SPLITS_CHIPS if c.get("status") == "beta"]
    cols = st.columns(len(beta_chips)) if beta_chips else None
    for i, c in enumerate(beta_chips):
        with cols[i]:
            on = state.beta_chips_active.get(c["key"], False)
            label = f"{'● ' if on else '○ '}{c['label']}  · beta"
            if st.button(label, key=f"split-{c['key']}", use_container_width=True,
                         help="Beta: requires closing-odds data we don't pull."):
                state.beta_chips_active[c["key"]] = not on


def _tri_state(label: str, options: list[str], current: str | None,
               key: str, labels: dict[str, str] | None = None) -> str | None:
    """Three-way radio: option A, option B, or 'Any'."""
    display_opts = ["Any"] + [labels.get(o, o.capitalize()) if labels else o.capitalize()
                              for o in options]
    if current is None:
        idx = 0
    else:
        raw_idx = options.index(current) if current in options else 0
        idx = raw_idx + 1
    pick = st.radio(label, display_opts, index=idx, horizontal=True, key=key)
    if pick == "Any":
        return None
    # Map display back to option key.
    for opt in options:
        display = labels.get(opt, opt.capitalize()) if labels else opt.capitalize()
        if display == pick:
            return opt
    return None


# ---------------------------------------------------------------------------
# Section: Stats
# ---------------------------------------------------------------------------


def _render_stats_tab(state: FilterState) -> None:
    st.caption(
        "Pick how averages are computed and which stat to overlay on the "
        "main bar chart. Also set per-stat range filters to narrow the sample."
    )
    col1, col2 = st.columns(2)
    with col1:
        mode_idx = 0 if state.avg_or_median == "avg" else 1
        pick = st.radio("Mode", ["Average", "Median"], index=mode_idx,
                        horizontal=True, key="fm-mode")
        state.avg_or_median = "avg" if pick == "Average" else "median"
    with col2:
        overlay_labels = ["None"] + list(STAT_OVERLAY_CHIPS.keys())
        cur_label = "None"
        if state.overlay_stat:
            for lbl, col in STAT_OVERLAY_CHIPS.items():
                if col == state.overlay_stat:
                    cur_label = lbl
                    break
        idx = overlay_labels.index(cur_label) if cur_label in overlay_labels else 0
        pick = st.selectbox("Graph overlay stat", overlay_labels, index=idx,
                            key="fm-overlay",
                            help="When set, the main Research graph overlays a "
                                 "blue line tracking this stat across the "
                                 "filtered games.")
        state.overlay_stat = STAT_OVERLAY_CHIPS.get(pick) if pick != "None" else None

    st.markdown("##### Per-stat range filters")
    st.caption("Set a min/max to narrow the sample. Leave blank to ignore.")
    chosen_chips = list(STAT_OVERLAY_CHIPS.items())
    rows = [chosen_chips[i:i + 3] for i in range(0, len(chosen_chips), 3)]
    for chips_in_row in rows:
        cols = st.columns(3)
        for (label, col_name), ui_col in zip(chips_in_row, cols):
            with ui_col:
                _render_stat_range_chip(state, label, col_name)


def _render_stat_range_chip(state: FilterState, label: str, col_name: str) -> None:
    existing = state.stat_ranges.get(col_name, (None, None))
    lo_str = "" if existing[0] is None else f"{existing[0]:g}"
    hi_str = "" if existing[1] is None else f"{existing[1]:g}"

    with st.expander(f"{label} range", expanded=col_name in state.stat_ranges):
        lo_in = st.text_input(f"Min ({col_name})", value=lo_str,
                              key=f"fm-range-{col_name}-lo")
        hi_in = st.text_input(f"Max ({col_name})", value=hi_str,
                              key=f"fm-range-{col_name}-hi")
        lo = _parse_float(lo_in)
        hi = _parse_float(hi_in)
        if lo is None and hi is None:
            state.stat_ranges.pop(col_name, None)
        else:
            state.stat_ranges[col_name] = (lo, hi)


def _parse_float(s: str) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Section: Help
# ---------------------------------------------------------------------------


def _render_help_tab() -> None:
    st.markdown("""
    ### Understanding Filters

    **Sample filtering.** Every filter *narrows* the set of games the app
    looks at. Start with all the player's games for the selected season,
    then each active filter removes games that don't match. The metrics,
    graph bars, and stat cards all recompute from the surviving sample.

    **Red vs green matchup ranking.** When a rank badge shows `#5/30`,
    that means the opposing defence ranks 5th easiest in that stat.
    Green = favourable matchup (top third), red = tough (bottom third),
    grey = middle.

    **Average vs Median.** The mode toggle (Stats tab) affects every card
    and summary number. Median is more robust to one or two outlier
    games; Average is more sensitive to them.

    **Teammate with / without.** A teammate filter keeps only games where
    the player *and* that teammate both played (WITH) or only games where
    the teammate did not play (WITHOUT). We determine "did they play" by
    intersecting both players' game logs — a simple and free method that
    works for any pair of players.

    **Diff vs excluded.** Each stat card shows `+2.14 vs excluded`. That
    is the gap between the filtered sample's value and the value for
    every game that *didn't* match the filter. Large positive diffs mean
    the filter is selecting games where this stat runs hot.

    **Beta chips.** Some chips (Play Types, Shooting Zones, Def vs
    Position, Closing Spread) aren't reliably covered by free nba_api
    endpoints. They render so you can see the full product surface, but
    toggling them has no effect on the sample. The README lists every
    beta chip.
    """)


# ---------------------------------------------------------------------------
# Footer (Apply / Clear)
# ---------------------------------------------------------------------------


def _render_footer(state: FilterState) -> None:
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("Clear all", key="fm-clear", use_container_width=True):
            state.clear()
            st.rerun()
    with col2:
        if st.button("Apply", key="fm-apply", type="primary",
                     use_container_width=True):
            st.rerun()
    with col3:
        active_count = len(state.active_chips())
        st.markdown(
            f"<div class='pp-section' style='text-align:right'>"
            f"{active_count} active filter{'s' if active_count != 1 else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )
