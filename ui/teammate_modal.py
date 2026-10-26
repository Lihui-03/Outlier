"""Filter-by-Teammate modal.

Scrollable dark table with W / W/O / PLAYER / POS / MIN / selected-stat columns.
Uses `FilterState.teammate_modes`; filter math stays in `filter_service`.

The **subject player** (the player being researched) is merged into the list
but cannot self-filter: W/W/O are disabled on that row. Any stale self-entry
in `teammate_modes` is stripped when the modal opens.
"""

from __future__ import annotations

import html as html_escape
import math
from collections.abc import Callable
from typing import Any

import pandas as pd
import streamlit as st

from services.teammate_service import fetch_teammate_summary, list_teammates
from utils.formatting import fmt_number
from utils.nba_logos import nba_team_logo_url_png

from . import components as ui_components
from .filter_state import FilterState


_MODAL_SESSION_FLAG = "_open_tm_modal"
_EDIT_TAGS_FLAG = "_tm_modal_edit_tags"

# Match `build_teammate_table` schema so merges work when the roster frame is empty.
_TEAM_TABLE_COLS: tuple[str, ...] = (
    "PLAYER_ID", "PLAYER", "POSITION", "NUM",
    "GAMES", "AVG_MIN", "STAT_AVGS", "GAME_IDS",
)

# Column proportions: W, W/O, PLAYER (avatar+name), POS, MIN, stat
_COL_RATIOS = [0.52, 0.52, 3.85, 0.68, 0.82, 1.05]


def stat_column_heading(market_col: str, market_label: str) -> str:
    """Short uppercase header aligned with sidebar stat / column code."""
    if market_col and str(market_col).strip():
        return str(market_col).strip().upper()
    return str(market_label or "STAT").strip().upper()


# ---------------------------------------------------------------------------
# Toggle semantics (exposed so tests and the summary bar can reuse it)
# ---------------------------------------------------------------------------


def on_pill_click(
    state: FilterState,
    pid: int,
    target: str,
    *,
    subject_player_id: int | None = None,
) -> None:
    """Apply the WITH / WITHOUT toggle rules to `state.teammate_modes`.

    Rules:
        * Clicking the currently-active pill toggles that teammate off.
        * Clicking the opposite pill switches the mode (no both-active).
        * Any other input is a no-op.

    The **subject player** (researched player) cannot toggle — no filter state.

    This is the single source of truth for teammate-mode transitions and
    is unit-tested in `_smoke_test.py`.
    """
    if subject_player_id is not None and int(pid) == int(subject_player_id):
        return
    if target not in ("with", "without"):
        return
    cur = state.teammate_modes.get(int(pid))
    if cur == target:
        state.teammate_modes.pop(int(pid), None)
    else:
        state.teammate_modes[int(pid)] = target


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _fmt_stat(value: float | int | None, *, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        if isinstance(value, float) and math.isnan(value):
            return "—"
    except Exception:
        pass
    return fmt_number(value, decimals=digits)


def _reopen_modal() -> None:
    """Keep the dialog open across the rerun triggered by a pill click."""
    st.session_state[_MODAL_SESSION_FLAG] = True


def _merge_subject_player_into_table(
    df: pd.DataFrame | None,
    *,
    subject_id: int,
    subject_name: str,
    subject_position_hint: str,
    team_id: int | None,
    season_api: str,
    season_type: str,
    league_id: str = "00",
) -> pd.DataFrame:
    """Ensure the researched player appears as a row (roster excludes them elsewhere).

    When the teammate table failed to load (empty frame), still inject one row so
    the modal shows the researched player with W/W/O disabled — layout + self-row
    rules stay consistent without changing filter_service.
    """
    if df is None:
        roster = pd.DataFrame(columns=list(_TEAM_TABLE_COLS))
    else:
        roster = df.copy()
    if roster.empty or "PLAYER_ID" not in roster.columns:
        roster = pd.DataFrame(columns=list(_TEAM_TABLE_COLS))

    if not roster.empty:
        pids = (
            pd.to_numeric(roster["PLAYER_ID"], errors="coerce")
            .fillna(-1).astype(int)
        )
        if int(subject_id) in set(pids.tolist()):
            return roster

    summ = fetch_teammate_summary(
        int(subject_id), season_api,
        season_type=season_type,
        league_id=league_id,
    )
    pos = (subject_position_hint or "").strip()
    if team_id:
        buddy_roster = list_teammates(
            team_id, season_api,
            exclude_player_id=None,
            league_id=league_id,
        )
        if (buddy_roster is not None and not buddy_roster.empty
                and "PLAYER_ID" in buddy_roster.columns):
            hit = buddy_roster[
                buddy_roster["PLAYER_ID"].astype("Int64") == int(subject_id)]
            if not hit.empty:
                pos = str(hit.iloc[0].get("POSITION") or pos or "")

    row = {
        "PLAYER_ID":  int(subject_id),
        "PLAYER":     subject_name.strip() or str(subject_id),
        "POSITION":   pos,
        "NUM":        "",
        "GAMES":      int(summ["games"]),
        "AVG_MIN":    float(summ["avg_min"])
        if summ["avg_min"] == summ["avg_min"]
        else float("nan"),
        "STAT_AVGS":  summ["stat_avgs"],
        "GAME_IDS":   summ["game_ids"],
    }
    subject_only = pd.DataFrame([row])
    if roster.empty:
        return subject_only
    return pd.concat([subject_only, roster], ignore_index=True)


def _render_team_season_selector_bar(
    *,
    season_ui: str,
    team_display: str,
    team_abbr: str,
    logo_url_fn: Callable[[str | None], str | None] = nba_team_logo_url_png,
) -> None:
    logo = logo_url_fn(team_abbr) if team_abbr else None
    img = (
        f'<img class="pp-tm-sel-logo" src="{logo}" alt="" '
        'onerror="this.style.display=\'none\'"/>'
        if logo else ""
    )
    safe_team = html_escape.escape(team_display or team_abbr or "Team")
    safe_season = html_escape.escape(season_ui or "—")
    st.markdown(
        f"""
        <div class="pp-tm-selector-bar">
            <div class="pp-tm-sel-left">
                <span class="pp-tm-sel-season">{safe_season}</span>
                {img}
                <span class="pp-tm-sel-team">{safe_team}</span>
            </div>
            <span class="pp-tm-sel-chev" aria-hidden="true">▾</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_static_table_header(market_heading: str) -> None:
    """Column labels aligned with `_render_row` (uppercase muted)."""
    mh = html_escape.escape(market_heading.upper())
    st.markdown(
        f"""
        <div class="pp-tm-head pp-tm-head-v2">
            <div style="flex:{_COL_RATIOS[0]};text-align:center">W</div>
            <div style="flex:{_COL_RATIOS[1]};text-align:center">W/O</div>
            <div style="flex:{_COL_RATIOS[2]}">PLAYER</div>
            <div style="flex:{_COL_RATIOS[3]};text-align:center">POS</div>
            <div style="flex:{_COL_RATIOS[4]};text-align:right">MIN</div>
            <div style="flex:{_COL_RATIOS[5]};text-align:right">
                <span class="market-pill">{mh}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_row(
    row: dict[str, Any],
    state: FilterState,
    *,
    market_col: str,
    subject_player_id: int | None,
) -> None:
    pid = int(row["PLAYER_ID"])
    cur = state.teammate_modes.get(pid)
    is_subject = (
        subject_player_id is not None
        and int(pid) == int(subject_player_id)
    )
    is_with = (not is_subject) and cur == "with"
    is_without = (not is_subject) and cur == "without"
    pills_disabled = bool(is_subject)
    hs_active = None if is_subject else cur

    cols = st.columns(_COL_RATIOS, vertical_alignment="center")

    with cols[0]:
        with_help = (
            "You can't filter WITH / WITHOUT yourself."
            if pills_disabled else "Games where this teammate also played"
        )
        if ui_components.action_pill(
                "+",
                key=f"tm_with_{pid}",
                active=is_with,
                disabled=pills_disabled,
                help=with_help):
            on_pill_click(state, pid, "with", subject_player_id=subject_player_id)
            _reopen_modal()
            st.rerun()

    with cols[1]:
        wo_help = (
            "You can't filter WITH / WITHOUT yourself."
            if pills_disabled else "Games where this teammate did not play"
        )
        if ui_components.action_pill(
                "−",
                key=f"tm_wo_{pid}",
                active=is_without,
                disabled=pills_disabled,
                help=wo_help):
            on_pill_click(state, pid, "without", subject_player_id=subject_player_id)
            _reopen_modal()
            st.rerun()

    dim_self = " pp-tm-self-dim" if is_subject else ""

    with cols[2]:
        name = str(row.get("PLAYER", "") or "")
        status_tag = state.teammate_availability.get(pid)
        status_html = ui_components.status_chip(status_tag)
        avatar = ui_components.headshot_html(pid, name or "", active=hs_active)

        badge_chunks: list[str] = []
        if status_html:
            badge_chunks.append(status_html)
        badge_row = (
            f'<div class="pp-tm-name-badges">{"".join(badge_chunks)}</div>'
            if badge_chunks else ""
        )
        player_block = (
            f'<div class="pp-tm-player-cell{dim_self}">'
            f'{avatar}<div class="pp-tm-name-stack">'
            f'<div class="pp-tm-name">{html_escape.escape(name)}</div>{badge_row}</div></div>'
        )
        st.markdown(player_block, unsafe_allow_html=True)

    with cols[3]:
        pos = row.get("POSITION") or ""
        pos_html = (f'<span class="pp-tm-pos">{pos}</span>'
                    if pos else '<span style="opacity:0.3">—</span>')
        st.markdown(
            f'<div style="text-align:center" class="pp-tm-pos-cell{dim_self}">'
            f'{pos_html}</div>',
            unsafe_allow_html=True,
        )

    with cols[4]:
        st.markdown(
            f'<div class="pp-tm-stat{dim_self}">'
            f'{_fmt_stat(row.get("AVG_MIN"))}</div>',
            unsafe_allow_html=True,
        )

    with cols[5]:
        value = _get_market_avg(row, market_col)
        try:
            dim = value is None or (isinstance(value, float) and math.isnan(value))
        except Exception:
            dim = True
        cls = "pp-tm-market-value dim" if dim else "pp-tm-market-value"
        cls = f"{cls}{dim_self}"
        st.markdown(
            f'<div class="{cls}">{_fmt_stat(value)}</div>',
            unsafe_allow_html=True,
        )


def _render_tag_editor(df: pd.DataFrame, state: FilterState) -> None:
    """Compact availability editor. Rendered only when the user toggles
    'Edit availability tags' — keeps default rows uncluttered."""
    st.caption("Set a status tag per teammate. Tags are stored only for this session.")
    options = ["—", "OUT", "GTD", "QUES", "ACTIVE"]
    # 3-column grid of selectboxes for compact editing.
    chunks = [df.iloc[i:i + 3] for i in range(0, len(df), 3)]
    for chunk in chunks:
        cols = st.columns(len(chunk), vertical_alignment="center")
        for (_, row), col in zip(chunk.iterrows(), cols):
            pid = int(row["PLAYER_ID"])
            current = (state.teammate_availability.get(pid) or "").upper() or "—"
            if current not in options:
                current = "—"
            with col:
                choice = st.selectbox(
                    f"Tag · {row.get('PLAYER', '')}",
                    options,
                    index=options.index(current),
                    key=f"tm_status_{pid}",
                )
                if choice == "—":
                    state.teammate_availability.pop(pid, None)
                else:
                    state.teammate_availability[pid] = choice


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _get_market_avg(row: dict[str, Any], market_col: str) -> float | None:
    """Look up the market-specific average from the STAT_AVGS dict column.

    Falls back to the legacy MARKET_AVG column so existing cached tables
    (built before the STAT_AVGS refactor) still render correctly.
    """
    stat_avgs = row.get("STAT_AVGS")
    if isinstance(stat_avgs, dict):
        val = stat_avgs.get(market_col)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # Legacy fallback for pre-refactor cached DataFrames.
    legacy = row.get("MARKET_AVG")
    if legacy is not None:
        try:
            return float(legacy)
        except (TypeError, ValueError):
            pass
    return None


def _assign_market_sort_key(df: pd.DataFrame, market_col: str) -> pd.DataFrame:
    def cell(r: pd.Series) -> float:
        v = _get_market_avg(r.to_dict(), market_col)
        try:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return float("-inf")
            return float(v)
        except (TypeError, ValueError):
            return float("-inf")

    out = df.copy()
    out["_tm_sort_market"] = out.apply(cell, axis=1)
    return out


def render_teammate_filter_modal(
    teammate_df: pd.DataFrame | None,
    state: FilterState,
    *,
    market_col: str,
    market_label: str,
    season_api: str,
    season_ui: str,
    season_type: str,
    team_abbr: str,
    team_display_name: str,
    team_id: int | None,
    subject_player_id: int,
    subject_player_name: str,
    subject_position: str | None = None,
    league_id: str = "00",
    logo_url_fn: Callable[[str | None], str | None] = nba_team_logo_url_png,
) -> None:
    """Full modal body. Invoked from ``@st.dialog`` in ``app.py``."""
    _render_team_season_selector_bar(
        season_ui=season_ui,
        team_display=team_display_name,
        team_abbr=team_abbr,
        logo_url_fn=logo_url_fn,
    )

    sid = int(subject_player_id)
    state.teammate_modes.pop(sid, None)

    df = _merge_subject_player_into_table(
        teammate_df,
        subject_id=sid,
        subject_name=subject_player_name,
        subject_position_hint=subject_position or "",
        team_id=team_id,
        season_api=season_api,
        season_type=season_type,
        league_id=league_id,
    )
    if df.empty:
        st.info("No teammate data available for this player/season yet.")
        return

    with st.expander("Search · sort · tags", expanded=False):
        c1, c2, c3 = st.columns([2.5, 1.5, 1.2], vertical_alignment="center")
        with c1:
            query = st.text_input(
                "Search", key="_tm_modal_search",
                placeholder="Filter by name…",
                label_visibility="collapsed",
            )
        with c2:
            sort = st.selectbox(
                "Sort",
                options=[
                    "Minutes (high → low)",
                    "Name (A → Z)",
                    f"{market_label} (high → low)",
                ],
                key="_tm_modal_sort",
                label_visibility="collapsed",
            )
        with c3:
            edit_tags = st.toggle(
                "Tags",
                value=bool(st.session_state.get(_EDIT_TAGS_FLAG, False)),
                key=_EDIT_TAGS_FLAG,
                help="Edit OUT / GTD / QUES / ACTIVE badges (session only).",
            )

    if query:
        q = query.strip().lower()
        df = df[df["PLAYER"].fillna("").str.lower().str.contains(q)]

    if sort.startswith("Name"):
        df = df.sort_values("PLAYER", ascending=True, na_position="last")
    elif sort.startswith(market_label):
        df = _assign_market_sort_key(df, market_col)
        df = df.sort_values("_tm_sort_market", ascending=False, na_position="last")
        df = df.drop(columns=["_tm_sort_market"], errors="ignore")
    else:
        df = df.sort_values("AVG_MIN", ascending=False, na_position="last")

    if edit_tags:
        with st.expander("Availability tags", expanded=True):
            _render_tag_editor(df, state)

    active_count = sum(1 for m in state.teammate_modes.values()
                       if m in ("with", "without"))

    stat_hdr = stat_column_heading(market_col, market_label)
    _render_static_table_header(stat_hdr)

    scroll = st.container(height=420, border=False)
    with scroll:
        if df.empty:
            st.caption("No teammates match the current search.")
        else:
            row_list = list(df.iterrows())
            for i, (_, row) in enumerate(row_list):
                _render_row(
                    row.to_dict(),
                    state,
                    market_col=market_col,
                    subject_player_id=sid,
                )
                if i < len(row_list) - 1:
                    st.markdown(
                        '<div class="pp-tm-row-gap" aria-hidden="true"></div>',
                        unsafe_allow_html=True,
                    )

    st.markdown("---")
    footer_left, footer_mid, footer_right = st.columns([2.0, 1.0, 1.05],
                                                        vertical_alignment="center")
    with footer_left:
        st.caption(
            f"{active_count} active teammate filter{'s' if active_count != 1 else ''}"
            if active_count else
            "No teammate filters active.",
        )
    with footer_mid:
        if st.button("Clear filters", key="_tm_modal_clear",
                     use_container_width=True, disabled=active_count == 0):
            state.teammate_modes.clear()
            _reopen_modal()
            st.rerun()
    with footer_right:
        if st.button("Done", key="_tm_modal_done", type="primary",
                     use_container_width=True):
            st.session_state.pop(_MODAL_SESSION_FLAG, None)
            st.rerun()
