"""Game Markets page — team-level totals, team totals, and spreads.

Rendered top-to-bottom inside a single Streamlit function so the flow is
easy to follow:

    1. Matchup input row (team A, team B, season, date probe)
    2. Market + side + manual-line row
    3. Lightweight matchup filter chips (last N, regular/playoffs, H2H only)
    4. Summary cards (projection, line, edge, lean, sample size)
    5. Main chart (bar chart of recent relevant metric)
    6. Tabs: Overview, Recent Games, Splits, H2H, Projection Details, Half Analysis

Half markets show a "Load half-level data (beta)" button. Clicking it
runs ``linescore_service.enrich_team_log_with_halves`` for both teams —
one BoxScoreSummaryV2 call per recent game, permanently cached. Until
loaded, half projections use heuristic shares and are tagged ``beta``.

State: all widgets use local keys under ``st.session_state["_gm_*"]``.
No coupling to the player-props FilterState.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    COLORS,
    GAME_MARKET_DEFAULT_LINE,
    GAME_MARKET_KIND,
    GAME_MARKET_NEEDS_SIDE,
    GAME_MARKET_RECENT_N,
    GAME_MARKETS,
    recent_seasons,
)
from services import (
    game_market_projection_service as projection_service,
    linescore_service,
    matchup_service,
    team_service,
)
from services.matchup_service import PairContext
from utils.formatting import (
    fmt_number,
    fmt_signed,
    format_team_margin,
    spread_interpretation,
)

logger = logging.getLogger(__name__)


def _market_window(market: str) -> str:
    """Map a market name to the ``market_window`` key used by
    ``spread_interpretation`` so half markets get half-specific phrasing.
    """
    if market.startswith("1st Half"):
        return "1h"
    if market.startswith("2nd Half"):
        return "2h"
    return "game"


# ---------------------------------------------------------------------------
# Cached loaders (Streamlit-level). Underlying services also use SQLite,
# so the two layers combine into: rerun → in-process cache hit; fresh
# session → SQLite cache hit (if within TTL); cold → live API.
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False, ttl=12 * 60 * 60)
def _load_pair_context_base(team_a_id: int, team_b_id: int,
                            season: str, season_type: str,
                            home_team_id: int | None) -> PairContext:
    """Single source of truth for per-matchup data.

    Returns a PairContext with team logs + advanced table already fetched.
    Streamlit caches the whole dataclass, so repeat renders within the
    session avoid even the SQLite round-trip. ``st.cache_data`` returns
    a deep copy, so the caller may safely mutate (e.g. enrich with half
    line scores) without corrupting the cached value.
    """
    return matchup_service.build_pair_context(
        team_a_id, team_b_id, season, season_type,
        recent_n=GAME_MARKET_RECENT_N, home_team_id=home_team_id,
    )


# ---------------------------------------------------------------------------
# Small render helpers
# ---------------------------------------------------------------------------


def _team_option_label(team: dict[str, Any]) -> str:
    abbr = team.get("abbreviation", "?")
    name = team.get("full_name", "?")
    return f"{abbr} — {name}"


def _teams_dropdown(label: str, teams: list[dict[str, Any]], *, key: str,
                    default_id: int | None = None) -> int:
    """Render a team dropdown and return the chosen team_id."""
    if not teams:
        st.selectbox(label, ["(no teams available)"], key=key, disabled=True)
        return 0
    options = {_team_option_label(t): int(t["id"]) for t in teams}
    labels = list(options.keys())
    default_idx = 0
    if default_id is not None:
        for i, t in enumerate(teams):
            if int(t.get("id", 0)) == default_id:
                default_idx = i
                break
    picked = st.selectbox(label, labels, index=default_idx, key=key)
    return options[picked]


def _summary_card(col, title: str, value: str, delta: str | None = None,
                  help: str | None = None) -> None:
    """Metric-card wrapper that uses st.metric so Streamlit handles sizing."""
    with col:
        st.metric(title, value, delta=delta, help=help)


# ---------------------------------------------------------------------------
# Filter application (minimal, inline; no coupling to filter_service)
# ---------------------------------------------------------------------------


def _apply_sample_filters(log: pd.DataFrame, *,
                          last_n: int | None,
                          location: str | None,
                          game_type: str | None,
                          h2h_abbr: str | None,
                          b2b_only: bool) -> pd.DataFrame:
    """Narrow a team's game log for the chart/tabs sample.

    Filters are pure, in-memory, and independent — every combination
    is valid. ``location`` and ``game_type`` used to share one variable,
    which made them mutually exclusive (the later one always won). They
    are now separate args so a user can say "home + playoffs" and both
    are applied.

    last_n is applied last so H/A or H2H filters do not get truncated
    before narrowing to the most recent N qualifying games.
    """
    if log is None or log.empty:
        return log
    out = log
    if location == "home" and "IS_HOME" in out.columns:
        out = out[out["IS_HOME"] == True]
    elif location == "away" and "IS_HOME" in out.columns:
        out = out[out["IS_HOME"] == False]
    if game_type == "regular" and "SEASON_TYPE" in out.columns:
        out = out[out["SEASON_TYPE"].astype(str).str.lower().eq("regular season")]
    elif game_type == "playoffs" and "SEASON_TYPE" in out.columns:
        out = out[out["SEASON_TYPE"].astype(str).str.lower().eq("playoffs")]
    if h2h_abbr and "OPPONENT" in out.columns:
        out = out[out["OPPONENT"].fillna("").astype(str).str.upper()
                  == h2h_abbr.upper()]
    if b2b_only and "REST_DAYS" in out.columns:
        out = out[pd.to_numeric(out["REST_DAYS"], errors="coerce").fillna(99).le(0)]
    if last_n and last_n > 0:
        out = out.head(last_n)
    return out


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------


def _chart_metric_and_label(market: str) -> tuple[str, str]:
    """Return (column_name, human_label) for the chart metric per market."""
    if market == "Full Game Total":
        return ("GAME_TOTAL", "Combined total")
    if market == "Team Total":
        return ("PTS", "Points scored")
    if market == "Full Game Spread":
        return ("MARGIN", "Scoring margin")
    if market == "1st Half Total":
        return ("TOTAL_1H", "1H combined total")
    if market == "2nd Half Total":
        return ("TOTAL_2H", "2H combined total")
    if market == "1st Half Spread":
        return ("MARGIN_1H", "1H scoring margin")
    if market == "2nd Half Spread":
        return ("MARGIN_2H", "2H scoring margin")
    return ("PTS", "Points")


def _render_chart(sample: pd.DataFrame, market: str, manual_line: float,
                  sel_abbr: str) -> None:
    """Bar chart: green when the metric beats the line (or covers), red when under.

    For totals the bar value *is* the quantity on the y-axis and the
    reference line sits at ``manual_line``. For spreads the bar value
    is the selected team's scoring margin and the reference line sits
    at the *cover threshold* ``-manual_line`` (e.g. posted -4.5 → cover
    when margin > +4.5). Labels spell this out instead of just echoing
    the posted number, which was confusing when the hline moved away
    from the raw number the user typed.
    """
    col, label = _chart_metric_and_label(market)
    if sample is None or sample.empty or col not in sample.columns:
        st.info("Not enough games to chart for this market and filter.")
        return

    plot_df = sample.copy()
    plot_df = plot_df[plot_df[col].notna()]
    if plot_df.empty:
        if market.startswith(("1st Half", "2nd Half")):
            st.info(f"No {label} data yet. Use **Load half-level data** above "
                    "to fetch per-game line scores for the recent sample.")
        else:
            st.info(f"No {label} data in the filtered sample.")
        return

    # Chart reads oldest → newest left-to-right so the most recent game
    # is closest to the projection.
    plot_df = plot_df.sort_values("GAME_DATE")
    kind = GAME_MARKET_KIND.get(market, "total")

    if kind == "total":
        threshold = manual_line
        annotation = f"Line {manual_line:.1f}"
    else:
        # Spread: selected team covers when their margin > -posted_line.
        threshold = -manual_line
        posted_fmt = format_team_margin(sel_abbr, manual_line)
        annotation = f"{posted_fmt} · cover line {threshold:+.1f}"

    colors = [COLORS["over"] if v > threshold else COLORS["under"]
              for v in plot_df[col]]

    # Compact labels: "Apr 05 vs NYK" keeps both context and axis density.
    x_labels = []
    for d, o in zip(plot_df["GAME_DATE"], plot_df.get("OPPONENT", "")):
        try:
            day_str = pd.to_datetime(d).strftime("%b %d")
        except Exception:
            day_str = str(d)
        opp_str = str(o) if o else ""
        x_labels.append(f"{day_str} vs {opp_str}" if opp_str else day_str)

    fig = go.Figure()
    fig.add_bar(
        x=x_labels, y=plot_df[col], marker=dict(color=colors),
        hovertemplate=f"%{{x}}<br>{label}: %{{y:.1f}}<extra></extra>",
    )
    fig.add_hline(
        y=threshold,
        line_dash="dot", line_color=COLORS["line"],
        annotation_text=annotation,
        annotation_position="top left",
    )
    fig.update_layout(
        title=f"{sel_abbr} · {label} — most recent games",
        xaxis_title="", yaxis_title=label,
        xaxis=dict(tickangle=-30),
        margin=dict(t=50, b=60, l=40, r=20),
        height=380, showlegend=False,
        bargap=0.25,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Clarify what each perspective means so the user doesn't wonder
    # whether "ATL game totals" and "NYK game totals" would disagree.
    if market == "Full Game Total":
        st.caption(f"Bars show combined game totals from {sel_abbr}'s recent schedule. "
                   "Both teams share the same game totals, so this view is equivalent "
                   "whichever side you anchor on.")
    elif market == "Team Total":
        st.caption(f"Bars show {sel_abbr}'s points scored per game — not the combined total.")
    elif market == "Full Game Spread":
        st.caption(f"Bars show {sel_abbr}'s scoring margin (positive = "
                   f"{sel_abbr} won by that much). Green bars clear the "
                   "cover line you entered above.")
    elif market in ("1st Half Spread", "2nd Half Spread"):
        half_label = "1st half" if market.startswith("1st") else "2nd half"
        st.caption(f"Bars show {sel_abbr}'s {half_label} margin. Green bars "
                   "clear the cover line you entered above.")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def _render_overview(ctx: PairContext, market: str,
                     proj: projection_service.GameMarketProjection,
                     side_team_id: int | None) -> None:
    abbr_a = ctx.team_a.get("abbreviation", "A")
    abbr_b = ctx.team_b.get("abbreviation", "B")
    st.markdown(
        f"**Matchup:** {abbr_a} vs {abbr_b} · **Market:** {market}"
        + (f" (side: "
           f"{abbr_a if side_team_id == int(ctx.team_a.get('id') or 0) else abbr_b})"
           if side_team_id is not None else "")
    )
    st.markdown("**Explanation**")
    for line in proj.explanation:
        st.write(f"- {line}")


def _render_recent_games(sample: pd.DataFrame, abbr: str) -> None:
    if sample is None or sample.empty:
        st.info("No recent games to show.")
        return
    keep = [c for c in ("GAME_DATE", "MATCHUP", "WL", "MIN", "PTS",
                        "PTS_AGAINST", "GAME_TOTAL", "MARGIN", "REST_DAYS",
                        "PTS_1H", "PTS_2H", "TOTAL_1H", "TOTAL_2H",
                        "MARGIN_1H", "MARGIN_2H")
            if c in sample.columns]
    view = sample[keep].copy()
    if "GAME_DATE" in view.columns:
        view["GAME_DATE"] = pd.to_datetime(view["GAME_DATE"]).dt.date
    st.caption(f"Showing {len(view)} game(s) for {abbr}.")
    st.dataframe(view, hide_index=True, use_container_width=True)


def _render_splits(log: pd.DataFrame, abbr: str) -> None:
    if log is None or log.empty:
        st.info("No data.")
        return
    ha = matchup_service.home_away_split(log)
    rows = []
    for tag in ("home", "away"):
        r = ha.get(tag, {})
        rows.append({
            "Split": tag.capitalize(),
            "Games": r.get("games", 0),
            "PPG": fmt_number(r.get("pts_for"), 1),
            "PPG Allowed": fmt_number(r.get("pts_against"), 1),
            "Avg Margin": fmt_signed(r.get("margin"), 1),
        })
    st.markdown(f"**{abbr} — Home vs Away**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if "IS_WIN" in log.columns:
        wl_rows = []
        for tag, mask in (("Wins", log["IS_WIN"] == True),
                          ("Losses", log["IS_WIN"] == False)):
            sub = log[mask]
            wl_rows.append({
                "Result": tag, "Games": len(sub),
                "PPG": fmt_number(pd.to_numeric(sub.get("PTS"),
                                                errors="coerce").mean(), 1),
                "PPG Allowed": fmt_number(pd.to_numeric(sub.get("PTS_AGAINST"),
                                                        errors="coerce").mean(), 1),
            })
        st.markdown(f"**{abbr} — Wins vs Losses**")
        st.dataframe(pd.DataFrame(wl_rows), hide_index=True, use_container_width=True)


def _render_h2h(ctx: PairContext) -> None:
    abbr_b = ctx.team_b.get("abbreviation", "")
    abbr_a = ctx.team_a.get("abbreviation", "")
    summary = matchup_service.h2h_summary(ctx.log_a, abbr_b, recent_n=5)
    if summary["games"] == 0:
        st.info(f"No head-to-head games between {abbr_a} and {abbr_b} in this season sample.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("H2H games", str(summary["games"]))
    c2.metric("Avg combined total", fmt_number(summary["recent_total_avg"], 1))
    c3.metric(f"{abbr_a} avg margin", fmt_signed(summary["recent_margin_avg"], 1))

    if summary["recent_rows"]:
        df = pd.DataFrame(summary["recent_rows"])
        if "GAME_DATE" in df.columns:
            df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
        st.dataframe(df, hide_index=True, use_container_width=True)


def _render_projection_details(proj: projection_service.GameMarketProjection,
                                *, market_kind: str = "total",
                                side_abbr: str | None = None) -> None:
    """Transparent breakdown — matches the card/metric style used by the
    player-prop Projection Details tab so switching modes doesn't feel
    jarring.

    For spread markets (``market_kind == "margin"``) the projection and
    line are rendered in sportsbook notation (e.g. ``TOR +1.3``) to match
    the top summary cards and interpretation caption.
    """
    c1, c2, c3, c4 = st.columns(4)
    if market_kind == "margin":
        c1.metric("Projected margin", format_team_margin(side_abbr, proj.projection))
        c2.metric("Market", format_team_margin(side_abbr, proj.manual_line))
    else:
        c1.metric("Projection", fmt_number(proj.projection, 1))
        c2.metric("Line", fmt_number(proj.manual_line, 1))
    c3.metric("Edge", fmt_signed(proj.edge, 1))
    c4.metric("Lean", proj.lean + (" · beta" if proj.beta else ""))

    if proj.beta:
        st.warning("This projection uses a heuristic because per-game "
                   "line-score data is not loaded yet. "
                   "Click **Load half-level data** above to replace with real "
                   "per-game 1H/2H averages.", icon=None)

    st.divider()

    st.markdown("**Plain-English explanation**")
    if proj.explanation:
        for line in proj.explanation:
            st.write(f"- {line}")
    else:
        st.caption("No explanation recorded for this projection.")

    st.divider()

    st.markdown("**Factors**")
    if not proj.factors:
        st.caption("No factor breakdown available.")
        return
    for f in proj.factors:
        with st.container(border=True):
            hc1, hc2 = st.columns([1, 4])
            # Factor values span a wide numeric range (pace ~1.0 vs ppg ~110).
            # 2 decimals is a good universal default; pace-like factors
            # keep enough precision to see the clamp behaviour.
            display = fmt_number(f.value, 3) if abs(f.value) < 10 else fmt_number(f.value, 1)
            hc1.metric(f.name.replace("_", " ").title(), display)
            hc2.caption(f.note or "")


def _render_half_analysis(ctx: PairContext, halves_loaded: bool) -> None:
    st.markdown("**Half-by-half breakdown (last 10 games per team)**")
    if not halves_loaded:
        st.info("Per-game 1H/2H data not loaded yet. Click "
                "**Load half-level data (beta)** above to enable this tab.")
        return

    rows = []
    for log, info in ((ctx.log_a, ctx.team_a), (ctx.log_b, ctx.team_b)):
        if log is None or log.empty:
            continue
        head = log.head(10)
        rows.append({
            "Team": info.get("abbreviation", "?"),
            "Games": len(head),
            "1H PPG": fmt_number(pd.to_numeric(head.get("PTS_1H"),
                                               errors="coerce").mean(), 1),
            "2H PPG": fmt_number(pd.to_numeric(head.get("PTS_2H"),
                                               errors="coerce").mean(), 1),
            "1H Allowed": fmt_number(pd.to_numeric(head.get("OPP_PTS_1H"),
                                                    errors="coerce").mean(), 1),
            "2H Allowed": fmt_number(pd.to_numeric(head.get("OPP_PTS_2H"),
                                                    errors="coerce").mean(), 1),
            "Avg 1H Margin": fmt_signed(pd.to_numeric(head.get("MARGIN_1H"),
                                                       errors="coerce").mean(), 1),
            "Avg 2H Margin": fmt_signed(pd.to_numeric(head.get("MARGIN_2H"),
                                                       errors="coerce").mean(), 1),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.caption("No half-level data in the sample.")


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------


def render_game_markets_page() -> None:
    """Render the whole Game Markets page.

    Called from ``app.py`` when ``_app_mode == "game_markets"``.
    """
    st.title("Game Markets")
    st.caption("Matchup-level research for totals, team totals, and spreads. "
               "You type the posted number; the model estimates a projection "
               "from free nba_api data (TeamGameLog + advanced splits).")

    teams = team_service.list_teams()
    if not teams:
        st.error("Could not load the NBA team list. Check your connection.")
        return

    # ── Row 1: matchup inputs ──────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 1])
    # Stable defaults: Hawks vs Knicks per the user's example.
    default_a = next((t["id"] for t in teams
                      if t.get("abbreviation") == "ATL"), teams[0]["id"])
    default_b = next((t["id"] for t in teams
                      if t.get("abbreviation") == "NYK"),
                     teams[1]["id"] if len(teams) > 1 else teams[0]["id"])
    with c1:
        team_a_id = _teams_dropdown("Team A (home by default)", teams,
                                    key="_gm_team_a", default_id=default_a)
    with c2:
        team_b_id = _teams_dropdown("Team B (away by default)", teams,
                                    key="_gm_team_b", default_id=default_b)
    with c3:
        seasons = recent_seasons(n=5)
        season = st.selectbox("Season", seasons, index=0, key="_gm_season")

    if team_a_id == team_b_id:
        st.warning("Pick two different teams.")
        return

    # ── Row 2: market + side + manual line ─────────────────────────────
    c4, c5, c6 = st.columns([2, 2, 1])
    with c4:
        market = st.selectbox("Market", GAME_MARKETS, index=0, key="_gm_market")
    with c5:
        if GAME_MARKET_NEEDS_SIDE.get(market, False):
            team_a = next(t for t in teams if int(t["id"]) == team_a_id)
            team_b = next(t for t in teams if int(t["id"]) == team_b_id)
            side_options = [team_a_id, team_b_id]
            side_labels = {
                team_a_id: f"{team_a.get('abbreviation','A')} "
                           f"({team_a.get('full_name','')})",
                team_b_id: f"{team_b.get('abbreviation','B')} "
                           f"({team_b.get('full_name','')})",
            }
            # Key is scoped to the current team pair so swapping matchup
            # doesn't resurrect a stale selection pointing at a team that
            # is no longer in the dropdown.
            side_team_id = st.radio(
                "Side", options=side_options,
                format_func=lambda tid: side_labels.get(tid, str(tid)),
                horizontal=True,
                key=f"_gm_side_{team_a_id}_{team_b_id}",
            )
        else:
            side_team_id = None
            st.caption("No side selection for this market.")
    with c6:
        default_line = GAME_MARKET_DEFAULT_LINE.get(market, 0.0)
        kind = GAME_MARKET_KIND.get(market, "total")

        if kind == "margin":
            # Sportsbook-style input: separate sign from magnitude so the
            # user doesn't have to remember that "favourite = negative".
            # Defaults split the signed config default into its two parts.
            default_sign = "-" if float(default_line) < 0 else "+"
            default_mag = abs(float(default_line))
            sign_options = ["-", "+"]
            # Scope sign to the market so Full-game vs 1st-half etc. each
            # have their own sticky choice without leaking across markets.
            sign = st.radio(
                "Spread side",
                options=sign_options,
                index=sign_options.index(default_sign),
                horizontal=True,
                key=f"_gm_spread_sign_{market}",
                help=("Minus = favourite (must win by more). "
                      "Plus = underdog (may lose by up to, or win outright)."),
            )
            magnitude = st.number_input(
                "Points",
                min_value=0.0, max_value=50.0,
                value=float(default_mag),
                step=0.5, format="%.1f",
                key=f"_gm_spread_mag_{market}",
            )
            # Internal signed value. Projection engine stays unchanged.
            manual_line = -float(magnitude) if sign == "-" else float(magnitude)
        else:
            # Key is scoped to the market so switching markets always shows
            # a sensible default. Unchanged for totals / team totals.
            manual_line = st.number_input(
                "Manual line",
                min_value=0.0, max_value=400.0,
                value=float(default_line),
                step=0.5, format="%.1f",
                key=f"_gm_line_{market}",
            )

    # Plain-English interpretation for spread markets — rendered under the
    # input row so the user can always see what their posted number means.
    if kind == "margin" and side_team_id is not None:
        sel_team = next((t for t in teams if int(t["id"]) == side_team_id), {})
        sel_abbr = sel_team.get("abbreviation", "?")
        interpretation = spread_interpretation(
            sel_abbr, manual_line,
            market_window=_market_window(market),
        )
        if interpretation:
            formatted_market = format_team_margin(sel_abbr, manual_line)
            st.info(f"**{formatted_market}** — {interpretation}")

    # Season type toggle (Regular Season is the sensible default for most markets).
    season_type = st.radio("Season type",
                           ["Regular Season", "Playoffs"],
                           horizontal=True, key="_gm_season_type")

    # ── Row 3: sample filter chips ─────────────────────────────────────
    # These filters are independent of each other; previously Location
    # and Game Type shared a single `mode` variable so the later one
    # always clobbered the earlier one.
    with st.expander("Sample filters", expanded=False):
        fc1, fc2, fc3, fc4 = st.columns([1, 1, 1, 1])
        with fc1:
            last_n_label = st.selectbox("Last N games",
                                         ["5", "10", "15", "20", "Max"],
                                         index=1, key="_gm_lastn")
            last_n = None if last_n_label == "Max" else int(last_n_label)
        with fc2:
            location_label = st.selectbox("Location",
                                          ["All", "Home", "Away"],
                                          index=0, key="_gm_loc")
            loc_filter: str | None = None
            if location_label == "Home":
                loc_filter = "home"
            elif location_label == "Away":
                loc_filter = "away"
        with fc3:
            gt_label = st.selectbox("Game type",
                                     ["All", "Regular Season", "Playoffs"],
                                     index=0, key="_gm_stmode")
            gt_filter: str | None = None
            if gt_label == "Regular Season":
                gt_filter = "regular"
            elif gt_label == "Playoffs":
                gt_filter = "playoffs"
        with fc4:
            h2h_only = st.checkbox("H2H only", value=False, key="_gm_h2h")
            b2b_only = st.checkbox("B2B only", value=False, key="_gm_b2b")

    # ── Load pair context (cached) ─────────────────────────────────────
    with st.spinner(f"Loading {season} game logs for both teams…"):
        try:
            ctx = _load_pair_context_base(team_a_id, team_b_id, season,
                                          season_type, home_team_id=team_a_id)
        except Exception as exc:
            logger.exception("pair context load failed")
            st.error(f"Could not load matchup data: {exc}")
            return

    if ctx.is_stale:
        st.warning("Live NBA data temporarily unavailable — showing cached "
                   "team game logs. Projections may not include the latest game.",
                   icon=None)

    if ctx.log_a.empty and ctx.log_b.empty:
        st.warning("No games found for either team in this season/season type. "
                   "Try a different season or switch season type.")
        return

    # ── Half-level data loader (lazy, beta) ────────────────────────────
    is_half_market = market.startswith(("1st Half", "2nd Half"))
    if is_half_market:
        halves_a = linescore_service.halves_are_cached(ctx.log_a.head(10)) \
            if not ctx.log_a.empty else True
        halves_b = linescore_service.halves_are_cached(ctx.log_b.head(10)) \
            if not ctx.log_b.empty else True
        halves_loaded_session = (
            "TOTAL_1H" in ctx.log_a.columns
            and ctx.log_a["TOTAL_1H"].notna().any()
        ) if not ctx.log_a.empty else False

        cc1, cc2 = st.columns([3, 1])
        with cc1:
            st.caption("Half markets use heuristic shares of the full-game "
                       "projection until per-game line scores are loaded. "
                       "Loading is a one-time step per team+season; results "
                       "are cached permanently.")
        with cc2:
            load_button = st.button("Load half-level data (beta)",
                                     key="_gm_load_halves",
                                     use_container_width=True,
                                     disabled=halves_loaded_session)

        if load_button or (halves_a and halves_b and not halves_loaded_session):
            # Either the user asked to load, or everything is already cached
            # so enrichment is effectively free (just reads SQLite).
            with st.spinner("Fetching per-game line scores…"):
                progress = st.progress(0, text="Line scores…")

                def _cb(done: int, total: int) -> None:
                    progress.progress(min(done / max(total, 1), 1.0),
                                      text=f"Line scores: {done}/{total}")

                try:
                    ctx.log_a = linescore_service.enrich_team_log_with_halves(
                        ctx.log_a, team_a_id, progress_callback=_cb, max_games=10,
                    )
                    ctx.log_b = linescore_service.enrich_team_log_with_halves(
                        ctx.log_b, team_b_id, progress_callback=_cb, max_games=10,
                    )
                    # Refresh recent slices post-enrich.
                    ctx.recent_a = ctx.log_a.head(GAME_MARKET_RECENT_N).copy()
                    ctx.recent_b = ctx.log_b.head(GAME_MARKET_RECENT_N).copy()
                except Exception as exc:
                    logger.warning("half enrichment failed: %s", exc)
                    st.warning(f"Could not load half-level data: {exc}")
                finally:
                    progress.empty()

    halves_loaded = (
        not ctx.log_a.empty
        and "TOTAL_1H" in ctx.log_a.columns
        and ctx.log_a["TOTAL_1H"].notna().any()
    )

    # ── Projection ─────────────────────────────────────────────────────
    try:
        proj = projection_service.project(
            market, ctx, manual_line=manual_line,
            side_team_id=side_team_id, recent_n=GAME_MARKET_RECENT_N,
        )
    except Exception as exc:
        logger.exception("game-market projection failed")
        st.error(f"Projection failed: {exc}")
        return

    # ── Summary cards ──────────────────────────────────────────────────
    st.divider()
    s1, s2, s3, s4, s5 = st.columns(5)
    kind = GAME_MARKET_KIND.get(market, "total")
    if kind == "margin":
        # Sportsbook-style cards for spreads: "TEAM +X.X" everywhere so
        # projection and posted line share the same visual language.
        sel_team = next((t for t in teams if int(t["id"]) == side_team_id), {})
        sel_abbr = sel_team.get("abbreviation", "?")
        _summary_card(s1, "Projected margin",
                      format_team_margin(sel_abbr, proj.projection),
                      help="Model's estimate of the selected team's margin "
                           "of victory (negative = loss).")
        _summary_card(s2, "Market",
                      format_team_margin(sel_abbr, manual_line),
                      help="The posted spread you entered.")
    else:
        _summary_card(s1, "Projection", fmt_number(proj.projection, 1),
                      help="Model estimate for the selected market.")
        _summary_card(s2, "Manual line", fmt_number(manual_line, 1))
    _summary_card(s3, "Edge", fmt_signed(proj.edge, 1),
                  delta=fmt_signed(proj.edge, 1) if not math.isnan(proj.edge) else None,
                  help="Projection minus line (totals) or projected margin "
                       "minus cover threshold (spreads).")
    _summary_card(s4, "Lean", proj.lean + (" · beta" if proj.beta else ""))
    _summary_card(s5, "Sample", f"{proj.sample_n} gp",
                  help="Games used in the projection baseline.")

    # ── Filter the chart sample (relative to the side / market) ────────
    # Chart uses the selected side's log when a side is chosen; otherwise
    # team A as the default anchor (e.g. Full Game Total shows team A's
    # combined-totals bar chart, which is equivalent to team B's up to
    # perspective).
    if side_team_id is not None and side_team_id == team_b_id:
        chart_log = ctx.log_b
        chart_abbr = ctx.team_b.get("abbreviation", "B")
        other_abbr = ctx.team_a.get("abbreviation", "A")
    else:
        chart_log = ctx.log_a
        chart_abbr = ctx.team_a.get("abbreviation", "A")
        other_abbr = ctx.team_b.get("abbreviation", "B")

    sample = _apply_sample_filters(
        chart_log, last_n=last_n,
        location=loc_filter, game_type=gt_filter,
        h2h_abbr=other_abbr if h2h_only else None, b2b_only=b2b_only,
    )

    st.divider()
    _render_chart(sample, market, manual_line, chart_abbr)

    # ── Tabs ───────────────────────────────────────────────────────────
    tab_over, tab_recent, tab_splits, tab_h2h, tab_proj, tab_half = st.tabs(
        ["Overview", "Recent Games", "Splits", "H2H", "Projection Details",
         "Half Analysis"]
    )
    with tab_over:
        _render_overview(ctx, market, proj, side_team_id)
    with tab_recent:
        _render_recent_games(sample, chart_abbr)
    with tab_splits:
        st.markdown(f"### {chart_abbr}")
        _render_splits(chart_log, chart_abbr)
        st.markdown(f"### {other_abbr}")
        other_log = ctx.log_b if chart_log is ctx.log_a else ctx.log_a
        _render_splits(other_log, other_abbr)
    with tab_h2h:
        _render_h2h(ctx)
    with tab_proj:
        proj_side_abbr = None
        if kind == "margin" and side_team_id is not None:
            proj_side_abbr = next(
                (t.get("abbreviation") for t in teams
                 if int(t["id"]) == side_team_id), None,
            )
        _render_projection_details(proj, market_kind=kind,
                                    side_abbr=proj_side_abbr)
    with tab_half:
        _render_half_analysis(ctx, halves_loaded)
