"""Transparent projection engine for game-level markets.

All math is explicit and expressed as a list of ``Factor`` steps with
plain-English notes, so the Projection Details tab can display the
breakdown exactly as it was computed. Nothing here touches Streamlit,
the network, or the cache.

Markets supported
-----------------
Full Game Total     — real
Team Total          — real
Full Game Spread    — real
1st Half Total      — real when per-game line scores loaded, else beta heuristic
2nd Half Total      — real when per-game line scores loaded, else beta heuristic
1st Half Spread     — real when per-game line scores loaded, else beta heuristic
2nd Half Spread     — real when per-game line scores loaded, else beta heuristic

Free-data compromise: without a per-game period breakdown the half
markets fall back to fixed heuristic shares (``HALF_SCORING_SHARE`` /
``HALF_SPREAD_SHARE`` in config). Those projections are flagged
``beta=True`` so the UI can label them clearly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from config import (
    GAME_MARKET_RECENT_N,
    HALF_SCORING_SHARE,
    HALF_SPREAD_SHARE,
    HOME_COURT_ADV,
)
from services.matchup_service import PairContext, recent_form
from utils.formatting import (
    fmt_number,
    fmt_signed,
    format_team_margin,
    spread_interpretation,
)

logger = logging.getLogger(__name__)


def _fmt(value: float, decimals: int = 1) -> str:
    """Shorthand: NaN-safe formatted number for explanation strings."""
    return fmt_number(value, decimals=decimals)


def _fmts(value: float, decimals: int = 1) -> str:
    """Shorthand: NaN-safe signed number for explanation strings."""
    return fmt_signed(value, decimals=decimals)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Factor:
    """One transparent line in the projection explanation."""

    name: str
    value: float
    note: str


@dataclass
class GameMarketProjection:
    """Result shape shared across every market.

    ``kind`` is "total" or "margin" so the UI knows how to phrase the lean.
    ``projection`` is the model's number for the chosen market/side.
    ``edge = projection - manual_line`` for totals; for spreads, edge is
    computed with the sportsbook convention ``-line`` (see compute_edge).
    ``factors`` is a list of Factor rows for the breakdown panel.
    ``beta`` is True when we used heuristic-only math.
    """

    market: str
    kind: str                               # "total" | "margin"
    projection: float
    edge: float
    lean: str                               # Over/Under/Cover/No-cover/Pass
    manual_line: float
    factors: list[Factor] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    beta: bool = False
    sample_n: int = 0
    side_team_id: int | None = None         # for Team Total / Spread


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_mean(series: pd.Series | None) -> float:
    if series is None:
        return float("nan")
    val = pd.to_numeric(series, errors="coerce").mean()
    return float(val) if pd.notna(val) else float("nan")


def _team_advanced_row(advanced: pd.DataFrame, team_id: int) -> dict[str, float]:
    """Return {PACE, DEF_RATING, OFF_RATING, NET_RATING} for a team.

    Missing columns or teams produce NaN for each metric.
    """
    if advanced is None or advanced.empty or "TEAM_ID" not in advanced.columns:
        return {}
    row = advanced[advanced["TEAM_ID"] == team_id]
    if row.empty:
        return {}
    out: dict[str, float] = {}
    for k in ("PACE", "DEF_RATING", "OFF_RATING", "NET_RATING"):
        if k in row.columns:
            v = pd.to_numeric(row.iloc[0].get(k), errors="coerce")
            out[k] = float(v) if pd.notna(v) else float("nan")
    return out


def _league_mean(advanced: pd.DataFrame, col: str) -> float:
    if advanced is None or advanced.empty or col not in advanced.columns:
        return float("nan")
    v = pd.to_numeric(advanced[col], errors="coerce").mean()
    return float(v) if pd.notna(v) else float("nan")


def _lean_label_total(edge: float) -> str:
    if math.isnan(edge):
        return "-"
    if edge > 0.5:
        return "Over"
    if edge < -0.5:
        return "Under"
    return "Pass"


def _lean_label_margin(projected_margin: float, manual_spread: float,
                       covers_if_margin_gt: float) -> str:
    """Return cover/no-cover wording for a spread market.

    ``manual_spread`` is the user's posted spread for the selected team
    (e.g. -4.5 means they are the favourite). A team "covers" when their
    projected margin beats the cover threshold (``covers_if_margin_gt``).
    """
    if math.isnan(projected_margin):
        return "-"
    slack = projected_margin - covers_if_margin_gt
    if slack > 0.25:
        return "Covers"
    if slack < -0.25:
        return "No cover"
    return "Pass"


# ---------------------------------------------------------------------------
# Full Game Total
# ---------------------------------------------------------------------------


def project_full_game_total(ctx: PairContext, manual_line: float,
                            *, recent_n: int = GAME_MARKET_RECENT_N
                            ) -> GameMarketProjection:
    """Symmetric projection: each team's scoring × pace adjustment.

    Formulas:
        proj_A = 0.5 × (A_off_ppg + B_def_ppg)
        proj_B = 0.5 × (B_off_ppg + A_def_ppg)
        pace_factor = (A_pace + B_pace) / (2 × league_pace)      # clamped
        projection = (proj_A + proj_B) × pace_factor

    Recent-form baseline uses each team's last ``recent_n`` games rather
    than the season average so hot/cold runs carry through.
    """
    rf_a = recent_form(ctx.log_a, n=recent_n)
    rf_b = recent_form(ctx.log_b, n=recent_n)

    a_off = rf_a["pts_for"]
    a_def = rf_a["pts_against"]
    b_off = rf_b["pts_for"]
    b_def = rf_b["pts_against"]

    factors: list[Factor] = []
    factors.append(Factor("team_a_offense",
                          float(a_off) if pd.notna(a_off) else float("nan"),
                          f"{ctx.team_a.get('abbreviation','A')} L{recent_n} PPG scored"))
    factors.append(Factor("team_b_defense",
                          float(b_def) if pd.notna(b_def) else float("nan"),
                          f"{ctx.team_b.get('abbreviation','B')} L{recent_n} PPG allowed"))
    factors.append(Factor("team_b_offense",
                          float(b_off) if pd.notna(b_off) else float("nan"),
                          f"{ctx.team_b.get('abbreviation','B')} L{recent_n} PPG scored"))
    factors.append(Factor("team_a_defense",
                          float(a_def) if pd.notna(a_def) else float("nan"),
                          f"{ctx.team_a.get('abbreviation','A')} L{recent_n} PPG allowed"))

    # Safest fallback when a team has no games at all in the window.
    def _avg(x: float, y: float) -> float:
        vals = [v for v in (x, y) if not math.isnan(v)]
        return float(sum(vals) / len(vals)) if vals else float("nan")

    proj_a = 0.5 * (a_off + b_def) if not math.isnan(a_off) and not math.isnan(b_def) else _avg(a_off, b_def)
    proj_b = 0.5 * (b_off + a_def) if not math.isnan(b_off) and not math.isnan(a_def) else _avg(b_off, a_def)
    base_total = proj_a + proj_b if not math.isnan(proj_a) and not math.isnan(proj_b) else float("nan")

    factors.append(Factor("proj_team_a", proj_a,
                          f"{ctx.team_a.get('abbreviation','A')} projected points"))
    factors.append(Factor("proj_team_b", proj_b,
                          f"{ctx.team_b.get('abbreviation','B')} projected points"))

    # Pace adjustment (clamped to ±10 %).
    a_adv = _team_advanced_row(ctx.advanced, int(ctx.team_a.get("id") or 0))
    b_adv = _team_advanced_row(ctx.advanced, int(ctx.team_b.get("id") or 0))
    pace_a = a_adv.get("PACE", float("nan"))
    pace_b = b_adv.get("PACE", float("nan"))
    lg_pace = _league_mean(ctx.advanced, "PACE")
    pace_factor = 1.0
    if not math.isnan(pace_a) and not math.isnan(pace_b) and not math.isnan(lg_pace) and lg_pace > 0:
        raw = (pace_a + pace_b) / (2.0 * lg_pace)
        pace_factor = max(0.90, min(1.10, raw))
    if not math.isnan(pace_a):
        pace_note = (f"({_fmt(pace_a)}+{_fmt(pace_b)}) / "
                     f"(2×{_fmt(lg_pace)}); clamped ±10%")
    else:
        pace_note = "pace data unavailable, factor held at 1.000"
    factors.append(Factor("pace_factor", pace_factor, pace_note))

    projection = base_total * pace_factor if not math.isnan(base_total) else float("nan")
    edge = projection - manual_line if not math.isnan(projection) else float("nan")

    pace_direction = (
        "above" if pace_factor > 1 else "below" if pace_factor < 1 else "at"
    )
    explanation = [
        f"Base: {ctx.team_a.get('abbreviation','A')} and "
        f"{ctx.team_b.get('abbreviation','B')} each project to about "
        f"{_fmt(proj_a)} and {_fmt(proj_b)} points respectively, using each "
        f"team's recent offensive output vs the opponent's recent points allowed.",
        f"Pace adjustment: ×{_fmt(pace_factor, 3)} ({pace_direction} league avg).",
        f"Combined projection: {_fmt(projection)}. Manual line is "
        f"{_fmt(manual_line)}, so the model leans {_lean_label_total(edge)} "
        f"by {_fmts(edge)}.",
    ]

    return GameMarketProjection(
        market="Full Game Total",
        kind="total",
        projection=projection,
        edge=edge,
        lean=_lean_label_total(edge),
        manual_line=manual_line,
        factors=factors,
        explanation=explanation,
        beta=False,
        sample_n=int(min(rf_a.get("games", 0), rf_b.get("games", 0))),
    )


# ---------------------------------------------------------------------------
# Team Total
# ---------------------------------------------------------------------------


def project_team_total(ctx: PairContext, manual_line: float, *,
                       side_team_id: int,
                       recent_n: int = GAME_MARKET_RECENT_N
                       ) -> GameMarketProjection:
    """Same symmetric formula as total but returns only the selected team's half.

    Pace factor is still applied because scoring volume scales with
    possessions for either side.
    """
    # Orient so A = the selected side, B = the other team.
    if side_team_id == int(ctx.team_a.get("id") or 0):
        off_log, def_log = ctx.log_a, ctx.log_b
        sel_info, opp_info = ctx.team_a, ctx.team_b
    else:
        off_log, def_log = ctx.log_b, ctx.log_a
        sel_info, opp_info = ctx.team_b, ctx.team_a

    rf_off = recent_form(off_log, n=recent_n)
    rf_def = recent_form(def_log, n=recent_n)

    factors: list[Factor] = [
        Factor("sel_team_offense", rf_off["pts_for"],
               f"{sel_info.get('abbreviation','?')} L{recent_n} PPG scored"),
        Factor("opp_team_defense", rf_def["pts_against"],
               f"{opp_info.get('abbreviation','?')} L{recent_n} PPG allowed"),
    ]
    base = float("nan")
    if not math.isnan(rf_off["pts_for"]) and not math.isnan(rf_def["pts_against"]):
        base = 0.5 * (rf_off["pts_for"] + rf_def["pts_against"])

    a_adv = _team_advanced_row(ctx.advanced, int(ctx.team_a.get("id") or 0))
    b_adv = _team_advanced_row(ctx.advanced, int(ctx.team_b.get("id") or 0))
    pace_a = a_adv.get("PACE", float("nan"))
    pace_b = b_adv.get("PACE", float("nan"))
    lg_pace = _league_mean(ctx.advanced, "PACE")
    pace_factor = 1.0
    if not math.isnan(pace_a) and not math.isnan(pace_b) and not math.isnan(lg_pace) and lg_pace > 0:
        pace_factor = max(0.90, min(1.10, (pace_a + pace_b) / (2.0 * lg_pace)))
    factors.append(Factor("pace_factor", pace_factor,
                          "joint pace factor — same as full-game total"))

    projection = base * pace_factor if not math.isnan(base) else float("nan")
    edge = projection - manual_line if not math.isnan(projection) else float("nan")

    explanation = [
        f"{sel_info.get('abbreviation','?')} projects to {_fmt(projection)} "
        f"points — a blend of their recent scoring "
        f"({_fmt(rf_off['pts_for'])} PPG) and "
        f"{opp_info.get('abbreviation','?')}'s recent defence "
        f"({_fmt(rf_def['pts_against'])} PPG allowed).",
        f"Pace adjustment ×{_fmt(pace_factor, 3)} applied.",
        f"Manual team total line {_fmt(manual_line)}. Edge {_fmts(edge)}, "
        f"lean {_lean_label_total(edge)}.",
    ]

    return GameMarketProjection(
        market="Team Total",
        kind="total",
        projection=projection,
        edge=edge,
        lean=_lean_label_total(edge),
        manual_line=manual_line,
        factors=factors,
        explanation=explanation,
        beta=False,
        sample_n=int(min(rf_off.get("games", 0), rf_def.get("games", 0))),
        side_team_id=side_team_id,
    )


# ---------------------------------------------------------------------------
# Full Game Spread
# ---------------------------------------------------------------------------


def project_full_game_spread(ctx: PairContext, manual_spread: float, *,
                             side_team_id: int,
                             recent_n: int = GAME_MARKET_RECENT_N
                             ) -> GameMarketProjection:
    """Projected margin for the selected team (positive = they win by that).

    Base: NET_RATING difference from the advanced table (team vs league
    opponents), scaled to per-game points using combined pace.
    Plus: home-court adjustment if the selected team is home.
    Plus: recent-form delta (each team's MARGIN last N vs their season
    baseline NET_RATING), weighted 0.3 of the delta per team.

    Cover rule (sportsbook convention):
    - A favourite posted at ``-4.5`` covers when projected margin > 4.5.
    - A dog posted at ``+3.0`` covers when projected margin > -3.0
      (i.e. any loss smaller than 3 still covers).
    So "covers_if_margin_gt" = -manual_spread.
    """
    sel_id = int(side_team_id)
    opp_id = int(ctx.team_b.get("id") or 0) if sel_id == int(ctx.team_a.get("id") or 0) \
             else int(ctx.team_a.get("id") or 0)
    sel_info = ctx.team_a if sel_id == int(ctx.team_a.get("id") or 0) else ctx.team_b
    opp_info = ctx.team_b if sel_id == int(ctx.team_a.get("id") or 0) else ctx.team_a
    sel_log = ctx.log_a if sel_id == int(ctx.team_a.get("id") or 0) else ctx.log_b
    opp_log = ctx.log_b if sel_id == int(ctx.team_a.get("id") or 0) else ctx.log_a

    sel_adv = _team_advanced_row(ctx.advanced, sel_id)
    opp_adv = _team_advanced_row(ctx.advanced, opp_id)

    sel_net = sel_adv.get("NET_RATING", float("nan"))
    opp_net = opp_adv.get("NET_RATING", float("nan"))

    # Compute recent form once and reuse everywhere below. Previously
    # both the NET_RATING-missing fallback and the form-delta block
    # called recent_form, repeating the same pandas reduction.
    rf_sel = recent_form(sel_log, n=recent_n)
    rf_opp = recent_form(opp_log, n=recent_n)

    # Convert NET_RATING difference (per-100) to per-game margin using a
    # typical ~100 possessions/game. No explicit pace factor here because
    # net rating already bakes in each team's rate. Fallback to recent
    # actual margin difference when the advanced table is unavailable.
    if not math.isnan(sel_net) and not math.isnan(opp_net):
        base_margin = sel_net - opp_net
    elif not math.isnan(rf_sel["margin"]) and not math.isnan(rf_opp["margin"]):
        base_margin = rf_sel["margin"] - rf_opp["margin"]
    else:
        base_margin = float("nan")

    # Recent-form delta: each team's last-N margin vs their NET_RATING
    # baseline, weighted 30 %. Zero when NET_RATING is missing.
    form_adj = 0.0
    if not math.isnan(rf_sel["margin"]) and not math.isnan(sel_net):
        form_adj += 0.3 * (rf_sel["margin"] - sel_net)
    if not math.isnan(rf_opp["margin"]) and not math.isnan(opp_net):
        form_adj -= 0.3 * (rf_opp["margin"] - opp_net)

    # Home-court adjustment.
    hc_adj = 0.0
    is_home = (ctx.home_team_id is not None and int(ctx.home_team_id) == sel_id)
    if is_home:
        hc_adj = HOME_COURT_ADV
    elif ctx.home_team_id is not None:
        hc_adj = -HOME_COURT_ADV

    projected_margin = (base_margin + form_adj + hc_adj) \
                       if not math.isnan(base_margin) else float("nan")

    factors = [
        Factor("sel_net_rating", sel_net,
               f"{sel_info.get('abbreviation','?')} season NET_RATING"),
        Factor("opp_net_rating", opp_net,
               f"{opp_info.get('abbreviation','?')} season NET_RATING"),
        Factor("base_margin", base_margin,
               f"{sel_info.get('abbreviation','?')} minus "
               f"{opp_info.get('abbreviation','?')} net rating"),
        Factor("recent_form_adj", form_adj,
               "0.3 × (each team's last-N margin − its own NET_RATING)"),
        Factor("home_court_adj", hc_adj,
               f"+{HOME_COURT_ADV} if home, −{HOME_COURT_ADV} if road, "
               "0 if neutral"),
    ]

    # Cover convention: a team posted at -4.5 covers when they win by > 4.5.
    covers_if_gt = -manual_spread
    edge = (projected_margin - covers_if_gt) \
           if not math.isnan(projected_margin) else float("nan")
    lean = _lean_label_margin(projected_margin, manual_spread, covers_if_gt)

    sel_abbr = sel_info.get("abbreviation", "?")
    opp_abbr = opp_info.get("abbreviation", "?")
    posted_fmt = format_team_margin(sel_abbr, manual_spread)
    proj_fmt = format_team_margin(sel_abbr, projected_margin)
    cover_desc = spread_interpretation(sel_abbr, manual_spread,
                                        market_window="game")
    explanation = [
        f"{sel_abbr} projected margin vs {opp_abbr}: {proj_fmt} "
        f"({_fmts(projected_margin)} points).",
        f"Posted: {posted_fmt}. {cover_desc}",
        f"Model vs market: edge {_fmts(edge)} → {lean}.",
    ]

    return GameMarketProjection(
        market="Full Game Spread",
        kind="margin",
        projection=projected_margin,
        edge=edge,
        lean=lean,
        manual_line=manual_spread,
        factors=factors,
        explanation=explanation,
        beta=False,
        sample_n=int(rf_sel.get("games", 0)),
        side_team_id=sel_id,
    )


# ---------------------------------------------------------------------------
# Half totals & spreads
# ---------------------------------------------------------------------------


def _real_half_total(log_a: pd.DataFrame, log_b: pd.DataFrame,
                     half: str, recent_n: int) -> tuple[float, int]:
    """Return (avg_half_total, games_sampled) using enriched line-score data.

    Averages ``TOTAL_1H``/``TOTAL_2H`` across the most recent games where
    both logs have that column populated (from linescore_service).
    Returns (nan, 0) when real data is not available.
    """
    col = "TOTAL_1H" if half == "1H" else "TOTAL_2H"
    vals: list[float] = []
    if log_a is not None and col in log_a.columns:
        vals.extend(pd.to_numeric(log_a.head(recent_n)[col],
                                  errors="coerce").dropna().tolist())
    if log_b is not None and col in log_b.columns:
        vals.extend(pd.to_numeric(log_b.head(recent_n)[col],
                                  errors="coerce").dropna().tolist())
    if not vals:
        return (float("nan"), 0)
    return (float(sum(vals) / len(vals)), len(vals))


def _real_half_margin(sel_log: pd.DataFrame, half: str,
                      recent_n: int) -> tuple[float, int]:
    col = "MARGIN_1H" if half == "1H" else "MARGIN_2H"
    if sel_log is None or sel_log.empty or col not in sel_log.columns:
        return (float("nan"), 0)
    vals = pd.to_numeric(sel_log.head(recent_n)[col], errors="coerce").dropna()
    if vals.empty:
        return (float("nan"), 0)
    return (float(vals.mean()), int(len(vals)))


def project_half_total(ctx: PairContext, manual_line: float, *, half: str,
                       recent_n: int = GAME_MARKET_RECENT_N
                       ) -> GameMarketProjection:
    """Project 1st- or 2nd-half combined total.

    Uses real half data when ``linescore_service.enrich_team_log_with_halves``
    has populated ``TOTAL_1H`` / ``TOTAL_2H``. Otherwise falls back to a
    fixed share of the projected full-game total (flagged ``beta=True``).
    """
    assert half in ("1H", "2H"), "half must be '1H' or '2H'"
    market_name = "1st Half Total" if half == "1H" else "2nd Half Total"

    real_avg, n = _real_half_total(ctx.log_a, ctx.log_b, half, recent_n)
    if not math.isnan(real_avg):
        # Real path: blend each team's recent half totals. No pace factor
        # because it is already baked into per-game half scoring.
        projection = real_avg
        edge = projection - manual_line
        factors = [Factor(f"half_total_avg_{half.lower()}", projection,
                          f"blend of both teams' last {n} games' {half} "
                          "combined totals")]
        explanation = [
            f"{market_name}: average combined {half} scoring in recent games "
            f"is {_fmt(projection)} across {n} sampled halves.",
            f"Manual line {_fmt(manual_line)}. Edge {_fmts(edge)} → "
            f"{_lean_label_total(edge)}.",
        ]
        return GameMarketProjection(
            market=market_name, kind="total",
            projection=projection, edge=edge,
            lean=_lean_label_total(edge),
            manual_line=manual_line, factors=factors,
            explanation=explanation, beta=False, sample_n=n,
        )

    # Heuristic fallback: fixed share of the projected full-game total.
    full = project_full_game_total(ctx, manual_line=0.0, recent_n=recent_n)
    share = HALF_SCORING_SHARE.get(half, 0.5)
    projection = full.projection * share if not math.isnan(full.projection) else float("nan")
    edge = projection - manual_line if not math.isnan(projection) else float("nan")

    factors = [
        Factor("full_game_total", full.projection,
               "from the main full-game-total projection"),
        Factor(f"half_share_{half.lower()}", share,
               f"historical share of full-game points in {half} (league avg)"),
    ]
    explanation = [
        f"{market_name} (beta heuristic): {_fmt(full.projection)} × "
        f"{_fmt(share, 2)} = {_fmt(projection)}.",
        "Load real line-score data for this pair to replace this estimate "
        "with per-game 1H/2H averages.",
        f"Manual line {_fmt(manual_line)}. Edge {_fmts(edge)} → "
        f"{_lean_label_total(edge)}.",
    ]
    return GameMarketProjection(
        market=market_name, kind="total",
        projection=projection, edge=edge,
        lean=_lean_label_total(edge),
        manual_line=manual_line, factors=factors,
        explanation=explanation, beta=True, sample_n=full.sample_n,
    )


def project_half_spread(ctx: PairContext, manual_spread: float, *,
                        half: str, side_team_id: int,
                        recent_n: int = GAME_MARKET_RECENT_N
                        ) -> GameMarketProjection:
    """Project 1st- or 2nd-half spread for the selected team.

    Real path: average ``MARGIN_1H``/``MARGIN_2H`` across the selected
    team's last N games. Heuristic fallback: fixed share of projected
    full-game spread (flagged beta).
    """
    assert half in ("1H", "2H")
    market_name = "1st Half Spread" if half == "1H" else "2nd Half Spread"
    sel_id = int(side_team_id)

    sel_log = ctx.log_a if sel_id == int(ctx.team_a.get("id") or 0) else ctx.log_b
    sel_info = ctx.team_a if sel_id == int(ctx.team_a.get("id") or 0) else ctx.team_b
    opp_info = ctx.team_b if sel_id == int(ctx.team_a.get("id") or 0) else ctx.team_a

    real_avg, n = _real_half_margin(sel_log, half, recent_n)
    covers_if_gt = -manual_spread
    sel_abbr = sel_info.get("abbreviation", "?")
    window = "1h" if half == "1H" else "2h"
    half_text = "1st half" if half == "1H" else "2nd half"
    posted_fmt = format_team_margin(sel_abbr, manual_spread)

    if not math.isnan(real_avg):
        projection = real_avg
        edge = projection - covers_if_gt
        factors = [Factor(f"half_margin_avg_{half.lower()}", projection,
                          f"{sel_abbr} average "
                          f"{half} margin over last {n} games")]
        proj_fmt = format_team_margin(sel_abbr, projection)
        cover_desc = spread_interpretation(sel_abbr, manual_spread,
                                            market_window=window)
        explanation = [
            f"{sel_abbr} has averaged {proj_fmt} in the {half_text} "
            f"over the last {n} games.",
            f"Posted: {posted_fmt}. {cover_desc}",
            f"Model vs market: edge {_fmts(edge)} → "
            f"{_lean_label_margin(projection, manual_spread, covers_if_gt)}.",
        ]
        return GameMarketProjection(
            market=market_name, kind="margin",
            projection=projection, edge=edge,
            lean=_lean_label_margin(projection, manual_spread, covers_if_gt),
            manual_line=manual_spread, factors=factors,
            explanation=explanation, beta=False, sample_n=n,
            side_team_id=sel_id,
        )

    # Heuristic fallback: fraction of projected full-game spread.
    full = project_full_game_spread(ctx, manual_spread=0.0, side_team_id=sel_id,
                                    recent_n=recent_n)
    share = HALF_SPREAD_SHARE.get(half, 0.5)
    projection = full.projection * share if not math.isnan(full.projection) else float("nan")
    edge = projection - covers_if_gt if not math.isnan(projection) else float("nan")

    factors = [
        Factor("full_game_spread", full.projection,
               f"from the main full-game-spread projection (side={sel_abbr})"),
        Factor(f"half_share_{half.lower()}", share,
               f"historical share of full-game margin in {half} (league avg)"),
    ]
    proj_fmt = format_team_margin(sel_abbr, projection)
    cover_desc = spread_interpretation(sel_abbr, manual_spread,
                                        market_window=window)
    explanation = [
        f"{market_name} (beta heuristic): full-game projected margin "
        f"{_fmts(full.projection)} × {_fmt(share, 2)} = {proj_fmt} "
        f"for the {half_text}.",
        "Load real line-score data for this pair to replace this with "
        "per-game 1H/2H margin averages.",
        f"Posted: {posted_fmt}. {cover_desc}",
        f"Model vs market: edge {_fmts(edge)} → "
        f"{_lean_label_margin(projection, manual_spread, covers_if_gt)}.",
    ]
    return GameMarketProjection(
        market=market_name, kind="margin",
        projection=projection, edge=edge,
        lean=_lean_label_margin(projection, manual_spread, covers_if_gt),
        manual_line=manual_spread, factors=factors,
        explanation=explanation, beta=True, sample_n=full.sample_n,
        side_team_id=sel_id,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def project(market: str, ctx: PairContext, manual_line: float, *,
            side_team_id: int | None = None,
            recent_n: int = GAME_MARKET_RECENT_N) -> GameMarketProjection:
    """Single entry point for the UI — dispatches to the right formula."""
    if market == "Full Game Total":
        return project_full_game_total(ctx, manual_line, recent_n=recent_n)
    if market == "Team Total":
        sid = side_team_id if side_team_id is not None else int(ctx.team_a.get("id") or 0)
        return project_team_total(ctx, manual_line,
                                  side_team_id=sid, recent_n=recent_n)
    if market == "Full Game Spread":
        sid = side_team_id if side_team_id is not None else int(ctx.team_a.get("id") or 0)
        return project_full_game_spread(ctx, manual_line,
                                        side_team_id=sid, recent_n=recent_n)
    if market in ("1st Half Total", "2nd Half Total"):
        half = "1H" if market.startswith("1st") else "2H"
        return project_half_total(ctx, manual_line, half=half, recent_n=recent_n)
    if market in ("1st Half Spread", "2nd Half Spread"):
        half = "1H" if market.startswith("1st") else "2H"
        sid = side_team_id if side_team_id is not None else int(ctx.team_a.get("id") or 0)
        return project_half_spread(ctx, manual_line, half=half,
                                   side_team_id=sid, recent_n=recent_n)
    raise ValueError(f"Unknown game market: {market}")
