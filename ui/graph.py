"""The main Research bar chart.

Bars are drawn only for rows with a parseable calendar ``GAME_DATE``. Hover HTML
shows full matchup text. With **≤30** bars per plot, opponent + month + day
annotations appear under every column. With **more than 30** bars, sparse
month/day anchors mark the timeline only — hover still carries opponent + date.

X positions use integer ticks ``0 … n−1`` (oldest game left → newest right). The **Games**
setting in **Filters → Season & Games** caps how many youthful games reach this chart —
same sample as projections and metrics.

Rows lacking ``_chart_ts`` after ingestion repair are excluded (warning),
never placeholders.
"""


from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, formatted_game_count_filter_caption
from ui.filter_state import get_state
from services.gamelog_service import parse_player_matchup_string
from utils.dates import parse_game_date
from utils.formatting import (
    dedupe_compact_axis_month_day,
    format_nba_matchup_line,
    format_nba_prop_chart_axis_day,
    format_nba_prop_chart_axis_month,
    infer_nba_home_from_row,
    infer_player_team_abbr_from_row,
    nba_game_id_sort_string,
    nba_prop_chart_hit_over,
)
from utils.prop_chart_hover import format_prop_research_chart_hover_html
from utils.nba_prop_chart_axes import nba_prop_chart_yaxis_config
from utils.research_bar_chart import (
    AXIS_DENSE_MAX_GAMES,
    format_sparse_chart_day,
    sparse_axis_tick_positions,
)

logger = logging.getLogger(__name__)


def _bar_value_label(y: float) -> str:
    """Compact stat text on each bar (integers omit decimals)."""
    if y is None or (isinstance(y, float) and (y != y or pd.isna(y))):
        return ""
    fv = float(y)
    nearest = round(fv)
    if abs(fv - nearest) < 1e-9:
        return str(int(nearest))
    return f"{fv:g}"


def exclude_rows_without_chart_datetime(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows where ``_chart_ts`` is NaT — they cannot represent a dated NBA game."""
    if df.empty or "_chart_ts" not in df.columns:
        return df, 0
    mask = df["_chart_ts"].notna()
    dropped = int((~mask).sum())
    if dropped == 0:
        return df, 0
    return df.loc[mask].reset_index(drop=True), dropped


def _sort_gid_series(df: pd.DataFrame) -> pd.Series:
    col = next((c for c in ("Game_ID", "GAME_ID") if c in df.columns), None)
    if col is None:
        return pd.Series(["0000000000"] * len(df), index=df.index, dtype=object)
    return df[col].map(nba_game_id_sort_string)


def repair_opponent_and_home_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Fill blank ``OPPONENT`` / ``IS_HOME`` using ``MATCHUP`` (cache-safe).

    SQLite records occasionally omit opponent chips while leaving ``MATCHUP``
    intact; without repair metadata disagrees across columns on the **same row**.
    """
    if "MATCHUP" not in df.columns:
        return df
    out = df.copy()
    if "OPPONENT" not in out.columns:
        out["OPPONENT"] = ""

    opp_raw = out["OPPONENT"].astype(object).where(out["OPPONENT"].notna(), "")
    opp_str = opp_raw.astype(str).str.strip()
    bad_opp = opp_str.eq("") | opp_str.str.lower().eq("nan")

    if bad_opp.any():
        tup_series = out.loc[bad_opp, "MATCHUP"].map(parse_player_matchup_string)
        tuples_list = tup_series.tolist()
        out.loc[bad_opp, "OPPONENT"] = [t[0] for t in tuples_list]
        if "IS_HOME" not in out.columns:
            out["IS_HOME"] = pd.Series(False, index=out.index)
        out.loc[bad_opp, "IS_HOME"] = [t[1] for t in tuples_list]

    if "IS_HOME" in out.columns:
        mh = out["IS_HOME"].isna()
        if mh.any():
            tup2 = out.loc[mh, "MATCHUP"].map(parse_player_matchup_string)
            tl = tup2.tolist()
            out.loc[mh, "IS_HOME"] = [t[1] for t in tl]
    else:
        tup3 = out["MATCHUP"].map(parse_player_matchup_string)
        out["IS_HOME"] = tup3.apply(lambda x: x[1])

    return out


def _clean_opponent_series(df: pd.DataFrame) -> pd.Series:
    if "OPPONENT" not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    s = df["OPPONENT"].astype(object).where(df["OPPONENT"].notna(), "")
    s = s.astype(str).str.strip()
    return s.mask(s.str.lower().eq("nan"), "")


def _minutes_display(val: Any) -> str:
    if val is None:
        return "—"
    try:
        f = float(val)
        if pd.isna(f):
            return "—"
        return f"{f:.1f}"
    except (TypeError, ValueError):
        return "—"


def _prepare_research_chart_base(
    filtered: pd.DataFrame, stat_col: str,
) -> tuple[pd.DataFrame, int] | str:
    """Return chronological playable rows (**oldest→newest**) plus DNP-drop count.

    Strings signal early-exit categories for ``st.info`` messaging:
    ``\"EMPTY_DNP\"`` vs ``\"EMPTY_DATES\"``."""
    chart_df = filtered.copy().reset_index(drop=True)
    chart_df = repair_opponent_and_home_columns(chart_df)

    if "GAME_DATE" not in chart_df.columns:
        chart_df["GAME_DATE"] = pd.NaT

    chart_df["_chart_ts"] = parse_game_date(chart_df["GAME_DATE"])
    chart_df["_sort_gid"] = _sort_gid_series(chart_df)
    chart_df = chart_df.sort_values(
        by=["_chart_ts", "_sort_gid"],
        ascending=[True, True],
        na_position="last",
    ).reset_index(drop=True)
    chart_df = chart_df.drop(columns=["_sort_gid"], errors="ignore")

    chart_df[stat_col] = pd.to_numeric(chart_df[stat_col], errors="coerce")
    chart_df, dnp_dropped = _drop_dnp(chart_df, stat_col)
    if chart_df.empty:
        return "EMPTY_DNP"

    chart_df["_opp_clean"] = _clean_opponent_series(chart_df)

    bad_dt_mask = chart_df["_chart_ts"].isna()
    nd_bad_dt = int(bad_dt_mask.sum())
    if nd_bad_dt:
        gid_col = next((c for c in ("Game_ID", "GAME_ID") if c in chart_df.columns), None)
        sample_txt = ""
        if gid_col:
            ids = chart_df.loc[bad_dt_mask, gid_col].astype(str).head(12).tolist()
            sample_txt = " Sample Game_ID values: " + ", ".join(ids)
        logger.warning(
            "Research chart excluding %d row(s): GAME_DATE unparsed after "
            "ingestion repair.%s",
            nd_bad_dt,
            sample_txt,
        )
        st.warning(
            f"Excluded **{nd_bad_dt}** game row(s) with no parseable game date "
            f"(cannot plot real calendar games).{sample_txt} "
            "Try **Refresh data** if this persists after cache fixes."
        )

    chart_df = chart_df.loc[~bad_dt_mask].reset_index(drop=True)
    if chart_df.empty:
        return "EMPTY_DATES"

    assert bool(chart_df["_chart_ts"].notna().all()), "chart pipeline leaked NaT dates"
    return chart_df, dnp_dropped


def _drop_dnp(plot_df: pd.DataFrame, stat_col: str) -> tuple[pd.DataFrame, int]:
    """Return (plot_df without DNP rows, count of rows dropped).

    DNP = the player did not actually play. We detect that via the `MIN`
    column when present (NaN or <= 0) and otherwise fall back to a NaN
    value in the selected stat. Extracted as a pure helper so the
    behaviour is easy to unit-test without Streamlit.
    """
    if plot_df is None or plot_df.empty:
        return (plot_df if plot_df is not None else pd.DataFrame()), 0
    if "MIN" in plot_df.columns:
        minutes_num = pd.to_numeric(plot_df["MIN"], errors="coerce")
        played_mask = minutes_num.fillna(0) > 0
    elif stat_col in plot_df.columns:
        played_mask = pd.to_numeric(plot_df[stat_col], errors="coerce").notna()
    else:
        played_mask = pd.Series([True] * len(plot_df), index=plot_df.index)
    dropped = int((~played_mask).sum())
    kept = plot_df.loc[played_mask].reset_index(drop=True)
    return kept, dropped


def render_research_graph(
    filtered: pd.DataFrame,
    *,
    stat_col: str,
    stat_label: str,
    prop_line: float,
    overlay_col: str | None = None,
    overlay_label: str | None = None,
    height: int = 420,
    player_id: int | None = None,
    league_id: str = "00",
    teammate_df: pd.DataFrame | None = None,
) -> None:
    """Dense vs sparse Research bar chart. Sample size comes from **Filters → Games**."""
    if filtered is None or filtered.empty or stat_col not in filtered.columns:
        st.info("No games match the current filter. Try loosening a filter.")
        return

    prepared = _prepare_research_chart_base(filtered, stat_col)
    if isinstance(prepared, str):
        if prepared == "EMPTY_DNP":
            st.info("All filtered games were DNPs for this player. Loosen the filter.")
        else:
            st.warning(
                "No games left to chart — every filtered row lacked a usable "
                "`GAME_DATE`. Check filters or refresh cached game logs."
            )
        return

    chart_df, dnp_dropped = prepared

    _pid = int(player_id) if player_id is not None else 0
    if _pid > 0:
        try:
            from services.research_tooltip_enrichment import enrich_research_chart_tooltip_columns

            chart_df = enrich_research_chart_tooltip_columns(
                chart_df,
                player_id=_pid,
                league_id=league_id or "00",
                stat_col=stat_col,
                teammate_df=teammate_df,
            )
        except Exception as exc:
            # Enrichment hits stats.nba.com once per *unique* game (PBP, line score,
            # and for REB charts also player tracking). Never block the chart on failure.
            logger.warning(
                "research tooltip enrichment skipped (%s); chart will use base hover text",
                exc,
                exc_info=True,
            )

    is_half = abs(prop_line - round(prop_line)) > 1e-9
    use_overlay = bool(overlay_col) and overlay_col in chart_df.columns

    ys: list[float] = []
    hovers: list[str] = []
    colors: list[str] = []
    overlay_vals: list[float] = []

    axis_months_work: list[str] = []
    axis_days_work: list[str] = []
    iso_dates_axis: list[str] = []
    opponents_upper_axis: list[str] = []
    axis_abbr_lines: list[str] = []

    for idx in range(len(chart_df)):
        row = chart_df.iloc[idx]
        ts = row["_chart_ts"]
        if pd.isna(ts):
            raise AssertionError("row-mapping bug: NaT _chart_ts at position %s" % idx)

        opp_clean = str(row["_opp_clean"]).strip()
        ih = infer_nba_home_from_row(row)

        axis_months_work.append(format_nba_prop_chart_axis_month(ts))
        axis_days_work.append(format_nba_prop_chart_axis_day(ts))
        iso_dates_axis.append(ts.strftime("%Y-%m-%d"))
        opp_u = opp_clean.upper() if opp_clean else ""
        opponents_upper_axis.append(opp_u)
        axis_abbr_lines.append(opp_u if opp_u else "—")

        val = row[stat_col]
        ys.append(float(val) if pd.notna(val) else float("nan"))

        hit_over = nba_prop_chart_hit_over(val, prop_line, is_half_line=is_half)
        colors.append(COLORS["over"] if hit_over else COLORS["under"])

        loc = "Home" if ih is True else ("Away" if ih is False else "Unknown")
        team_abbr_row = infer_player_team_abbr_from_row(row)
        matchup_disp = format_nba_matchup_line(team_abbr_row, opp_clean, ih)

        # Tooltip HTML uses this same ``row`` as the bar (same y-value / stat_col cell).
        hovers.append(
            format_prop_research_chart_hover_html(
                row,
                stat_label=stat_label,
                stat_col=stat_col,
                stat_value=val,
                prop_line=prop_line,
                is_half_line=is_half,
                chart_ts=ts,
                opponent=opp_clean or "—",
                location=loc,
                matchup_display=matchup_disp,
                minutes_display=_minutes_display(row.get("MIN")),
            )
        )

        if use_overlay:
            ov = pd.to_numeric(row[overlay_col], errors="coerce")
            overlay_vals.append(float(ov) if pd.notna(ov) else float("nan"))

    axis_months, axis_days = dedupe_compact_axis_month_day(
        axis_months_work,
        axis_days_work,
        iso_dates=iso_dates_axis,
        opponents_upper=opponents_upper_axis,
    )

    x_positions = list(range(len(chart_df)))

    if not (
        len(x_positions)
        == len(ys)
        == len(hovers)
        == len(colors)
        == len(axis_months)
        == len(axis_days)
        == len(axis_abbr_lines)
    ):
        raise AssertionError(
            "chart row-mapping bug: parallel arrays diverged "
            f"{len(x_positions)=} {len(ys)=} {len(hovers)=} {len(colors)=} "
            f"{len(axis_months)=} {len(axis_days)=} {len(axis_abbr_lines)=}"
        )
    if use_overlay and len(overlay_vals) != len(ys):
        raise AssertionError("overlay length mismatch")

    n_plot = len(chart_df)
    use_sparse_axis = n_plot > AXIS_DENSE_MAX_GAMES
    crowded = n_plot > 10
    show_bar_interior_numbers = n_plot <= AXIS_DENSE_MAX_GAMES

    yaxis_cfg = nba_prop_chart_yaxis_config(
        stat_col=stat_col,
        ys=ys,
        prop_line=float(prop_line),
    )

    dense_abbrev = (-0.048, -0.084, -0.120)
    sparse_month_y = -0.055
    sparse_day_y = -0.098

    fig = go.Figure()
    bar_text = [_bar_value_label(y) for y in ys]
    text_size = 10 if crowded else 11

    trace_kwargs = dict(
        x=x_positions,
        y=ys,
        marker_color=colors,
        hovertext=hovers,
        hovertemplate="%{hovertext}<extra></extra>",
        hoverinfo="text",
        name=stat_label,
        marker=dict(line=dict(width=0)),
    )
    if show_bar_interior_numbers:
        trace_kwargs["text"] = bar_text
        trace_kwargs["textposition"] = "inside"
        trace_kwargs["insidetextanchor"] = "start"
        trace_kwargs["textfont"] = dict(color="#f8fafc", size=text_size)

    fig.add_trace(go.Bar(**trace_kwargs))

    fig.add_hline(
        y=prop_line, line_color=COLORS["line"], line_dash="dash",
        annotation_text=f"Line {prop_line:g}", annotation_position="top right",
        annotation_font_color=COLORS["line"],
    )

    if use_overlay:
        fig.add_trace(go.Scatter(
            x=x_positions,
            y=overlay_vals,
            mode="lines+markers", name=overlay_label or overlay_col,
            yaxis="y2",
            line=dict(color=COLORS["overlay"], width=2),
            marker=dict(color=COLORS["overlay"], size=6),
        ))
        fig.update_layout(
            yaxis2=dict(
                title=overlay_label or overlay_col,
                overlaying="y", side="right",
                showgrid=False,
            )
        )

    abbr_color = "#e2e8f0"
    month_color = "#94a3b8"
    day_color = "#cbd5e1"

    if use_sparse_axis:
        tick_idxs = sparse_axis_tick_positions(chart_df["_chart_ts"].tolist())
        for ti in tick_idxs:
            ts = chart_df.iloc[ti]["_chart_ts"]
            fig.add_annotation(
                xref="x",
                yref="paper",
                x=float(ti),
                y=sparse_month_y,
                text=format_nba_prop_chart_axis_month(ts),
                showarrow=False,
                font=dict(size=10, color=month_color),
                xanchor="center",
                yanchor="top",
            )
            fig.add_annotation(
                xref="x",
                yref="paper",
                x=float(ti),
                y=sparse_day_y,
                text=format_sparse_chart_day(ts),
                showarrow=False,
                font=dict(size=11, color=day_color),
                xanchor="center",
                yanchor="top",
            )
        bottom_px = 72
        bargap = 0.12 if n_plot > 60 else 0.18
    else:
        _yp = dense_abbrev
        for i in range(n_plot):
            fig.add_annotation(
                xref="x",
                yref="paper",
                x=float(i),
                y=_yp[0],
                text=axis_abbr_lines[i],
                showarrow=False,
                font=dict(size=11, color=abbr_color),
                xanchor="center",
                yanchor="top",
            )
            fig.add_annotation(
                xref="x",
                yref="paper",
                x=float(i),
                y=_yp[1],
                text=axis_months[i],
                showarrow=False,
                font=dict(size=10, color=month_color),
                xanchor="center",
                yanchor="top",
            )
            fig.add_annotation(
                xref="x",
                yref="paper",
                x=float(i),
                y=_yp[2],
                text=axis_days[i],
                showarrow=False,
                font=dict(size=11, color=day_color),
                xanchor="center",
                yanchor="top",
            )
        bottom_px = 96 if crowded else 88
        bargap = 0.28 if crowded else 0.25

    fig.update_layout(
        height=height,
        margin=dict(t=30, b=bottom_px, l=40, r=20),
        showlegend=use_overlay,
        hoverlabel=dict(
            bgcolor="#111827",
            bordercolor="#374151",
            font=dict(
                size=12,
                color="#f3f4f6",
                family=(
                    "ui-monospace, SFMono-Regular, Menlo, Monaco, "
                    "Consolas, Liberation Mono, Courier New, monospace"
                ),
            ),
            align="left",
        ),
        xaxis=dict(
            showticklabels=False,
            zeroline=False,
            automargin=True,
            range=(-0.5, float(max(0, n_plot - 1)) + 0.5),
        ),
        yaxis=dict(title=stat_label, **yaxis_cfg),
        bargap=bargap,
        uniformtext=dict(minsize=8, mode="hide"),
    )
    st.plotly_chart(fig, use_container_width=True)

    cg = len(chart_df)
    fs = get_state()
    cap_lbl = formatted_game_count_filter_caption(
        fs.game_count_mode,
        custom_n=fs.game_count_custom_n,
    )
    st.caption(
        f"**Filters → Games:** {cap_lbl}. **{cg}** bar{'s' if cg != 1 else ''} "
        "(playable dates) · oldest→newest left→right."
    )
    if use_sparse_axis:
        st.caption(
            "Showing sparse month/date ticks (>30 bars). Hover any bar for opponent and full date."
        )
    if dnp_dropped:
        st.caption(
            f"{dnp_dropped} DNP game{'s' if dnp_dropped != 1 else ''} "
            "hidden before charting."
        )
