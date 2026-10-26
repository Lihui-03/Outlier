"""Transparent, rule-based projection engine for CS2 research markets.

Mirrors the design of ``game_market_projection_service`` on the NBA side:
every projection returns a ``CS2Projection`` with an explicit list of
``Factor`` objects and a plain-English ``explanation`` — no black boxes.

Pure module: no Streamlit, no network, no cache. All inputs are already
loaded upstream (team profiles, player samples).

MVP markets:
    * match_winner       — transparent win-probability lean
    * total_maps         — projected maps in the series (vs manual line)
    * total_rounds_map   — projected total rounds in one map
    * player_kills       — projected kills on a single map
    * player_headshots   — projected headshot count on a single map

Markets flagged ``needs_hltv`` in ``constants.CS2_MARKETS`` return a
projection with ``beta=True`` and an explanation noting the enrichment gap.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from services.cs2.cs2_match_service import TeamForm, build_team_form  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Factor:
    """Single adjustment applied to a base projection."""
    name: str          # internal id, e.g. "win_rate_diff"
    factor: float      # multiplicative adjustment (1.0 = neutral)
    note: str          # plain-English one-liner for the UI


@dataclass
class CS2Projection:
    market: str                    # UI market name
    projection: float              # projected value in the same units as the manual line
    manual_line: float | None      # user-entered line; None when not applicable
    edge: float                    # projection - manual_line (NaN when no line)
    lean: str                      # "Over" / "Under" / "Covers" / "No cover" / etc.
    factors: list[Factor] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    beta: bool = False             # True when projection depends on HLTV / thin data
    sample_size: int = 0
    probability: float | None = None  # only set for match_winner / pistol_winner
    confidence: str = "medium"         # "low" / "medium" / "high"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_mean(values: list[float] | None) -> float:
    if not values:
        return float("nan")
    clean = [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return sum(clean) / len(clean) if clean else float("nan")


def _fmt(value: Any, decimals: int = 1) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "-"
    if math.isnan(f):
        return "-"
    return f"{f:.{decimals}f}"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _over_under(edge: float) -> str:
    if math.isnan(edge):
        return "-"
    if edge > 0:
        return "Over"
    if edge < 0:
        return "Under"
    return "Pass"


# ---------------------------------------------------------------------------
# Market: match winner
# ---------------------------------------------------------------------------


def project_match_winner(form_a: TeamForm, form_b: TeamForm,
                          *, team_a_name: str = "A",
                          team_b_name: str = "B",
                          h2h: dict | None = None) -> CS2Projection:
    """Estimate P(team A wins) from transparent factors.

    Inputs:
        form_a / form_b: TeamForm dataclasses (recent match sample)
        h2h: optional h2h_summary dict {"games", "a_wins", "b_wins", ...}

    Base prior: 50%. Adjusted by:
        - match win% differential (±)
        - map win% differential (smaller weight)
        - H2H win% (when >= 3 games, small weight)
        - sample-size tilt toward 50% when either team has < 5 games
    """
    factors: list[Factor] = []

    a_wr = form_a.win_pct
    b_wr = form_b.win_pct
    a_mp = form_a.map_win_pct
    b_mp = form_b.map_win_pct

    prob = 0.5
    explanation: list[str] = []

    def _pct(v: float) -> str:
        return "-" if math.isnan(v) else f"{v * 100:.0f}%"

    # 1. Match win% differential — max swing ±20%.
    if not math.isnan(a_wr) and not math.isnan(b_wr):
        diff = a_wr - b_wr
        swing = _clamp(diff * 0.40, -0.20, 0.20)
        prob += swing
        factors.append(Factor(
            name="match_win_rate_diff",
            factor=1.0 + swing,
            note=f"{team_a_name} match win rate {_pct(a_wr)} vs "
                 f"{team_b_name} {_pct(b_wr)} → {'+' if swing >= 0 else ''}{swing * 100:.0f}%.",
        ))
        explanation.append(
            f"Recent match record: {team_a_name} {form_a.wins}-{form_a.losses} "
            f"({_pct(a_wr)}) vs {team_b_name} {form_b.wins}-{form_b.losses} "
            f"({_pct(b_wr)})."
        )

    # 2. Map win% differential — smaller, ±10%.
    if not math.isnan(a_mp) and not math.isnan(b_mp):
        diff = a_mp - b_mp
        swing = _clamp(diff * 0.25, -0.10, 0.10)
        prob += swing
        factors.append(Factor(
            name="map_win_rate_diff",
            factor=1.0 + swing,
            note=f"Map win rate {_pct(a_mp)} vs {_pct(b_mp)} → "
                 f"{'+' if swing >= 0 else ''}{swing * 100:.0f}%.",
        ))

    # 3. H2H adjustment — only if 3+ games.
    if h2h and h2h.get("games", 0) >= 3:
        total = int(h2h["games"])
        a_wins = int(h2h.get("a_wins") or 0)
        a_h2h = a_wins / total
        swing = _clamp((a_h2h - 0.5) * 0.20, -0.08, 0.08)
        prob += swing
        factors.append(Factor(
            name="h2h_bias",
            factor=1.0 + swing,
            note=f"H2H {a_wins}-{total - a_wins} over {total} meetings → "
                 f"{'+' if swing >= 0 else ''}{swing * 100:.0f}%.",
        ))
        explanation.append(
            f"Head-to-head: {team_a_name} is {a_wins}-{total - a_wins} vs "
            f"{team_b_name} in the last {total} meetings ({_pct(a_h2h)})."
        )

    # 4. Sample-size regularisation: pull toward 50% when either team has
    # fewer than 5 recent games.
    small_sample = min(form_a.games, form_b.games)
    if small_sample < 5:
        pull = 0.5 - prob
        pull *= (1 - small_sample / 5) * 0.5
        prob += pull
        factors.append(Factor(
            name="sample_regularisation",
            factor=1.0,
            note=f"Thin sample ({small_sample} games) → pulled toward 50%.",
        ))

    prob = _clamp(prob, 0.05, 0.95)

    lean = team_a_name if prob >= 0.55 else (team_b_name if prob <= 0.45 else "Toss-up")
    confidence = "low" if small_sample < 5 else ("high" if max(prob, 1 - prob) >= 0.70 else "medium")

    explanation.append(
        f"Projected win probability: {team_a_name} {prob * 100:.1f}% — "
        f"lean **{lean}** ({confidence} confidence)."
    )

    return CS2Projection(
        market="Match Winner",
        projection=prob,
        manual_line=None,
        edge=float("nan"),
        lean=lean,
        factors=factors,
        explanation=explanation,
        probability=prob,
        sample_size=min(form_a.games, form_b.games),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Market: total maps in a series
# ---------------------------------------------------------------------------


def project_total_maps(prob_a: float, *, best_of: int,
                        manual_line: float | None) -> CS2Projection:
    """Project expected maps played in a BOn series from a win probability.

    Uses a simple binomial: expected series length given each side's prob
    of winning a single map. We approximate per-map prob as the match win
    prob (which already folds map win rate in via the match_winner model).

    BO3 support: E[maps] = 2*(p^2 + q^2) + 3*(2pq) where q = 1-p.
    BO5 support: E[maps] computed similarly (4 or 5 maps).
    BO1:         always 1 map — treated as a no-op with beta=True.
    """
    beta = False
    explanation: list[str] = []
    factors: list[Factor] = []

    if best_of == 1:
        beta = True
        projected = 1.0
        explanation.append("BO1 always plays 1 map — market is informational only.")
    else:
        p = _clamp(float(prob_a), 0.05, 0.95)
        q = 1.0 - p

        if best_of == 3:
            p_sweep = p ** 2 + q ** 2
            p_3games = 1 - p_sweep
            projected = 2 * p_sweep + 3 * p_3games
            explanation.append(
                f"BO3 sweep probability ≈ {p_sweep * 100:.0f}% "
                f"(either side winning 2-0); projected series length "
                f"{projected:.2f} maps."
            )
        elif best_of == 5:
            # P(4-0): someone wins 3 in a row then wins a 4th.
            p_40 = p ** 3 * (3 * q) + q ** 3 * (3 * p)  # each side's 3-0 window
            # Using a light approximation: simulate expected length via the
            # pattern [3-0, 3-1, 3-2] with standard negative-binomial math.
            p_3_0 = p ** 3 + q ** 3
            # 3-1 => exactly one loss somewhere in first 3, then win 4th.
            p_3_1 = 3 * (p ** 3 * q + q ** 3 * p)
            p_3_2 = 1 - p_3_0 - p_3_1
            projected = 3 * p_3_0 + 4 * p_3_1 + 5 * p_3_2
            explanation.append(
                f"BO5 expected length ≈ {projected:.2f} maps "
                f"(3-0: {p_3_0 * 100:.0f}%, 3-1: {p_3_1 * 100:.0f}%, "
                f"3-2: {p_3_2 * 100:.0f}%)."
            )
        else:
            beta = True
            projected = float(best_of)
            explanation.append(
                f"BO{best_of} not modelled explicitly — showing {best_of} "
                "as a placeholder (beta)."
            )

        factors.append(Factor(
            name="win_prob_input",
            factor=1.0,
            note=f"Input: projected win probability {p * 100:.0f}% / {q * 100:.0f}%.",
        ))

    edge = float("nan")
    lean = "-"
    if manual_line is not None:
        edge = projected - float(manual_line)
        lean = _over_under(edge)
        explanation.append(
            f"Posted line: {_fmt(manual_line, 1)} — model lean **{lean}** "
            f"(edge {edge:+.2f} maps)."
        )

    return CS2Projection(
        market="Total Maps (series)",
        projection=projected,
        manual_line=manual_line,
        edge=edge,
        lean=lean,
        factors=factors,
        explanation=explanation,
        beta=beta,
        sample_size=0,
        confidence="medium" if not beta else "low",
    )


# ---------------------------------------------------------------------------
# Market: total rounds on a single map
# ---------------------------------------------------------------------------


def project_total_rounds_map(map_totals: dict, selected_map: str,
                              *, manual_line: float | None) -> CS2Projection:
    """Project total rounds played on ``selected_map`` using the cross-team
    sample from ``cs2_map_service.aggregate_map_pool``.

    Falls back to the CS2 regulation-time baseline (24 rounds — 12+12)
    when no sample exists and flags ``beta=True``.
    """
    rec = (map_totals or {}).get(selected_map) or {}
    sample = rec.get("rounds_sample") or 0
    avg = rec.get("avg_total_rounds")

    factors: list[Factor] = []
    explanation: list[str] = []
    beta = False

    if avg is None:
        beta = True
        projected = 24.0
        explanation.append(
            f"No round-level data for {selected_map} in the recent sample — "
            "using the MR12 regulation baseline (24 rounds). Beta."
        )
    else:
        projected = float(avg)
        factors.append(Factor(
            name="map_avg_rounds",
            factor=1.0,
            note=f"{selected_map} avg total rounds {avg} across {sample} recent games.",
        ))
        explanation.append(
            f"Across {sample} recent {selected_map} games the average total "
            f"rounds played is {avg:.1f}."
        )
        if sample < 5:
            beta = True
            explanation.append(
                f"Only {sample} games in the round-level sample — treat as beta."
            )

    edge = float("nan")
    lean = "-"
    if manual_line is not None:
        edge = projected - float(manual_line)
        lean = _over_under(edge)
        explanation.append(
            f"Posted: {manual_line:.1f} rounds — model lean **{lean}** "
            f"(edge {edge:+.2f})."
        )

    return CS2Projection(
        market="Map Total Rounds",
        projection=projected,
        manual_line=manual_line,
        edge=edge,
        lean=lean,
        factors=factors,
        explanation=explanation,
        beta=beta,
        sample_size=sample,
        confidence="low" if beta else "medium",
    )


# ---------------------------------------------------------------------------
# Market: player kills (per map)
# ---------------------------------------------------------------------------


def project_player_kills(player_sample: dict, *, manual_line: float | None,
                           selected_map: str | None = None) -> CS2Projection:
    """Project kills per map. Base = recent-sample average, adjusted for map
    sample if the user selected a specific map."""
    factors: list[Factor] = []
    explanation: list[str] = []

    if not player_sample.get("has_stats"):
        return CS2Projection(
            market="Player Kills (map)",
            projection=float("nan"),
            manual_line=manual_line,
            edge=float("nan"),
            lean="-",
            factors=factors,
            explanation=[
                "No per-map stats available for this player. "
                "Load player stats from HLTV first (click 'Load stats' in the Players tab). "
                "If HLTV is in cooldown, stale-cached data will be used automatically."
            ],
            beta=True,
            sample_size=0,
            confidence="low",
        )

    base = player_sample.get("kills_avg") or 0.0
    explanation.append(
        f"Recent-sample kills per map: {_fmt(base, 2)} "
        f"over {player_sample.get('sample_size', 0)} maps."
    )
    factors.append(Factor(
        name="recent_kills_avg",
        factor=1.0,
        note=f"Base = {_fmt(base, 2)} kills/map across {player_sample['sample_size']} maps.",
    ))

    projection = float(base)

    # Optional map adjustment: if a map is selected and we have >= 2 games on it,
    # blend 60% map-specific with 40% overall baseline.
    if selected_map:
        from services.cs2.cs2_player_service import sample_for_map  # noqa: PLC0415
        map_games = sample_for_map(player_sample, selected_map)
        map_kills = [g["kills"] for g in map_games if g.get("kills") is not None]
        if len(map_kills) >= 2:
            map_avg = _safe_mean(map_kills)
            blended = 0.6 * map_avg + 0.4 * base
            factors.append(Factor(
                name="map_blend",
                factor=blended / base if base else 1.0,
                note=f"{selected_map} sample ({len(map_kills)} maps) avg "
                     f"{_fmt(map_avg, 2)} blended 60/40 with overall {_fmt(base, 2)}.",
            ))
            projection = blended
            explanation.append(
                f"On {selected_map} specifically, {len(map_kills)}-map average "
                f"is {map_avg:.2f} → blended projection {blended:.2f}."
            )
        else:
            explanation.append(
                f"Not enough {selected_map} sample ({len(map_kills)} maps) — "
                "using overall recent average instead."
            )

    edge = float("nan")
    lean = "-"
    if manual_line is not None:
        edge = projection - float(manual_line)
        lean = _over_under(edge)
        explanation.append(
            f"Posted line: {manual_line:.1f} — model lean **{lean}** "
            f"(edge {edge:+.2f} kills)."
        )

    confidence = "high" if player_sample.get("sample_size", 0) >= 6 else (
        "medium" if player_sample.get("sample_size", 0) >= 3 else "low"
    )

    return CS2Projection(
        market="Player Kills (map)",
        projection=projection,
        manual_line=manual_line,
        edge=edge,
        lean=lean,
        factors=factors,
        explanation=explanation,
        beta=player_sample.get("sample_size", 0) < 3,
        sample_size=player_sample.get("sample_size", 0),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Market: player headshots (per map)
# ---------------------------------------------------------------------------


def project_player_headshots(player_sample: dict, *,
                               manual_line: float | None) -> CS2Projection:
    """Projected headshot count = HS% × projected kills.

    Falls through to a beta projection when PandaScore returned no HS data.
    """
    factors: list[Factor] = []
    explanation: list[str] = []

    kills_avg = player_sample.get("kills_avg")
    hs_avg = player_sample.get("headshots_avg")
    hs_pct = player_sample.get("hs_pct_avg")
    n = player_sample.get("sample_size", 0)

    if hs_avg is None or not player_sample.get("has_stats"):
        return CS2Projection(
            market="Player Headshots",
            projection=float("nan"),
            manual_line=manual_line,
            edge=float("nan"),
            lean="-",
            factors=factors,
            explanation=[
                "Headshot stats require player stats to be loaded first. "
                "Click 'Load stats' in the Players tab. "
                "HLTV provides overall HS% which is used to project per-map headshots."
            ],
            beta=True,
            sample_size=n,
            confidence="low",
        )

    projection = float(hs_avg)
    if kills_avg and hs_pct is not None:
        factors.append(Factor(
            name="hs_pct",
            factor=1.0,
            note=f"HS% ≈ {hs_pct * 100:.1f}% of kills.",
        ))
        explanation.append(
            f"Recent headshots per map: {hs_avg:.2f} "
            f"(HS% {hs_pct * 100:.1f}% of {kills_avg:.2f} kills/map)."
        )
    else:
        explanation.append(
            f"Recent headshots per map: {hs_avg:.2f} across {n} maps."
        )

    edge = float("nan")
    lean = "-"
    if manual_line is not None:
        edge = projection - float(manual_line)
        lean = _over_under(edge)
        explanation.append(
            f"Posted line: {manual_line:.1f} — model lean **{lean}** "
            f"(edge {edge:+.2f})."
        )

    confidence = "high" if n >= 6 else ("medium" if n >= 3 else "low")

    return CS2Projection(
        market="Player Headshots",
        projection=projection,
        manual_line=manual_line,
        edge=edge,
        lean=lean,
        factors=factors,
        explanation=explanation,
        beta=n < 3,
        sample_size=n,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Beta/unimplemented markets — honest placeholder output.
# ---------------------------------------------------------------------------


def unimplemented_projection(market_label: str, *,
                               reason: str = "Needs HLTV enrichment.") -> CS2Projection:
    return CS2Projection(
        market=market_label,
        projection=float("nan"),
        manual_line=None,
        edge=float("nan"),
        lean="-",
        factors=[],
        explanation=[
            f"{market_label} is not projected in this build. {reason} "
            "The UI surfaces the card so you can still see the sample, but "
            "no projection is produced until enrichment is wired in."
        ],
        beta=True,
        confidence="low",
    )
