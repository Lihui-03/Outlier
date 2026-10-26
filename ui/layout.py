"""Top-level layout pieces for the Research tab.

These helpers glue primitives together into the three visual sections of
the Research view:

    * `render_top_bar` — always-on summary strip with the Filters button.
    * `render_key_metrics_row` — the most important numbers up top.
    * `render_stat_card_grid` — the per-stat card grid under the chart.
    * `render_teammate_row` — the With/Without cards.

Everything reads from a single `ctx` dict built in `app.py`, making it
easy to add new cards without rewriting the callers.
"""

from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

from config import STAT_OVERLAY_CHIPS
from models.metrics import compute_stat_card_diffs, compute_usage_proxy
from utils.formatting import fmt_number, fmt_percent, fmt_signed, over_under_label

from . import components


# ---------------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------------


def render_top_bar(*, player_name: str, team_abbr: str, market_label: str,
                   opponent_abbr: str, on_open_filters_key: str = "open-filters") -> bool:
    """Render the always-visible top bar. Returns True when the user just
    clicked the Filters button."""
    left, right = st.columns([4, 1])
    with left:
        title = f"<span class='pp-topbar-title'>{html.escape(player_name)}</span>"
        if team_abbr:
            title += f" <span class='pp-topbar-subtitle'>{html.escape(team_abbr)}</span>"
        sub_parts = [f"Market: <b>{html.escape(market_label)}</b>"]
        if opponent_abbr:
            sub_parts.append(f"vs <b>{html.escape(opponent_abbr)}</b>")
        sub = " · ".join(sub_parts)
        st.markdown(
            f"""
            <div class='pp-topbar'>
              <div>{title}<div class='pp-topbar-subtitle'>{sub}</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        clicked = st.button("Open Filters", key=on_open_filters_key,
                            type="primary", use_container_width=True,
                            help="Open the full filter modal.")
    return clicked


def render_active_filter_strip(active: list[tuple[str, str]]) -> None:
    """Show a one-line strip of currently-active filter chips."""
    components.render_chip_row(
        [{"label": lbl, "on": True} for _, lbl in active]
    ) if active else components.section_label("No filters active — showing the full sample.")


# ---------------------------------------------------------------------------
# Key metrics row
# ---------------------------------------------------------------------------


def render_key_metrics_row(*, hit_rate_row, projection_final: float,
                           edge: float, manual_line: float,
                           filtered_avg: float, season_avg: float,
                           sample_size_text: str) -> None:
    """Six-cell summary row: hit rate ratio, hit rate %, projection, edge,
    filtered avg, season avg."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Hit Rate", hit_rate_row.ratio_text,
              help="Filtered games above the prop line / filtered sample size.")
    c2.metric("Hit %", fmt_percent(hit_rate_row.rate),
              help="Same as ratio, expressed as a percentage.")
    c3.metric("Projection", fmt_number(projection_final, 2),
              help="Weighted projection using L5 / L10 / Season and any "
                   "active adjustments (from the sidebar).")
    c4.metric("Edge", fmt_signed(edge, 2),
              delta=f"Line {fmt_number(manual_line, 1)}",
              help="Projection minus the manual prop line. "
                   f"Lean: {over_under_label(edge)}.")
    c5.metric("Filtered Avg", fmt_number(filtered_avg, 2),
              help="Average (or median, per Stats tab) across the filtered sample.")
    c6.metric("Season Avg", fmt_number(season_avg, 2),
              help="Full-sample baseline for this season + season type. "
                   "Doesn't respond to filters.")
    st.caption(f"Sample: {sample_size_text}")


# ---------------------------------------------------------------------------
# Stat card grid
# ---------------------------------------------------------------------------


# Default set of per-game cards displayed under the main chart. Ordered
# most-important first so users scanning left-to-right see PTS/AST/REB
# before minute-level context.
DEFAULT_STAT_CARD_COLS: tuple[str, ...] = ("PTS", "AST", "REB", "FG3M", "MIN", "FGA")


def render_stat_card_grid(filtered: pd.DataFrame, excluded: pd.DataFrame,
                          mode: str = "avg",
                          cols: tuple[str, ...] = DEFAULT_STAT_CARD_COLS) -> None:
    """Grid of stat cards. Adds a 'USG% proxy' card when FGA/FTA/TOV/MIN
    are all present."""
    rows = compute_stat_card_diffs(filtered, excluded, list(cols), mode=mode)

    # USG% approximation card (computed from the series, not stored cols).
    usg_filtered = compute_usage_proxy(filtered).mean() if not filtered.empty else float("nan")
    usg_excluded = compute_usage_proxy(excluded).mean() if (excluded is not None and not excluded.empty) else None
    if usg_filtered == usg_filtered and usg_excluded is not None:
        usg_diff = usg_filtered - usg_excluded
    else:
        usg_diff = None

    usg_row = {"col": "USG proxy (per-48)", "filtered": usg_filtered,
               "excluded": usg_excluded, "diff": usg_diff}
    rows.append(usg_row)

    cols_per_row = 4
    buckets = [rows[i:i + cols_per_row] for i in range(0, len(rows), cols_per_row)]
    for bucket in buckets:
        ui_cols = st.columns(cols_per_row)
        for card_data, ui_col in zip(bucket, ui_cols):
            with ui_col:
                components.stat_card(
                    title=card_data["col"],
                    value=card_data["filtered"],
                    diff=card_data["diff"],
                )


# ---------------------------------------------------------------------------
# Teammate row
# ---------------------------------------------------------------------------


def render_teammate_row(teammate_df: pd.DataFrame,
                        diffs_by_pid: dict[int, float | None],
                        state: Any,
                        max_visible: int = 6) -> None:
    """Compact summary bar: shows active WITH/WITHOUT chips and a
    'Filter by Teammate' button that opens the dedicated modal.

    The old card-row UI is replaced by this lighter summary; full scanning
    now happens inside the modal rendered in `ui/teammate_modal.py`.

    `diffs_by_pid` is kept in the signature for backwards compatibility
    with the call site but is no longer rendered here.
    """
    _ = diffs_by_pid  # unused now, kept in signature for compatibility
    _ = max_visible   # likewise

    if teammate_df is None or teammate_df.empty:
        # Still let the user open the modal (e.g. data will load on demand).
        if st.button("Filter by Teammate", key="open_tm_modal_empty",
                     type="primary", use_container_width=False):
            st.session_state["_open_tm_modal"] = True
            st.rerun()
        st.caption("No teammate data loaded yet for this player/season.")
        return

    # Build a quick name lookup so summary chips can show real names.
    name_by_pid: dict[int, str] = {}
    if "PLAYER_ID" in teammate_df.columns and "PLAYER" in teammate_df.columns:
        try:
            name_by_pid = dict(zip(
                teammate_df["PLAYER_ID"].astype(int),
                teammate_df["PLAYER"].astype(str),
            ))
        except (TypeError, ValueError):
            name_by_pid = {}

    active_modes: list[tuple[int, str]] = [
        (int(pid), mode) for pid, mode in state.teammate_modes.items()
        if mode in ("with", "without")
    ]

    left, right = st.columns([4, 1])

    with left:
        if active_modes:
            # Render summary chips inline; each chip has a quick × remover
            # (a tiny Streamlit button). HTML is used for the visual chip,
            # but the remove control has to be a real widget.
            chip_cols = st.columns([2] * min(len(active_modes), 6) + [1])
            for idx, (pid, mode) in enumerate(active_modes[:6]):
                col = chip_cols[idx]
                name = name_by_pid.get(pid, f"#{pid}")
                sym = "+" if mode == "with" else "−"
                cls = "pp-tm-summary-chip " + ("with-mode" if mode == "with" else "without-mode")
                with col:
                    st.markdown(
                        f'<span class="{cls}"><span class="sym">{sym}</span>'
                        f'{html.escape(name)}</span>',
                        unsafe_allow_html=True,
                    )
                    if st.button("Remove", key=f"tm_summary_remove_{pid}",
                                 help=f"Remove {name} filter",
                                 use_container_width=True):
                        state.teammate_modes.pop(pid, None)
                        st.rerun()
            if len(active_modes) > 6:
                st.caption(f"+{len(active_modes) - 6} more active")
        else:
            st.caption("No teammate filters active.")

    with right:
        label = (
            f"Filter by Teammate ({len(active_modes)} active)"
            if active_modes else "Filter by Teammate"
        )
        if st.button(label, key="open_tm_modal",
                     type="primary", use_container_width=True):
            st.session_state["_open_tm_modal"] = True
            st.rerun()
