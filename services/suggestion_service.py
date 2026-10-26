"""Rule-based Suggested Filters.

The Suggested tab surfaces a short, ordered list of chips relevant to
the currently-selected market. Every rule is transparent and computed
from free nba_api data plus the player's already-loaded game log.

The goal is **not** to mimic any proprietary recommendation engine.
It's to give the user a fast "here are the usual angles" panel so they
don't have to hunt through every tab.

Each `SuggestedChip` is returned with:
    key       — machine key matching a filter-state field
    label     — human label
    status    — "real" (implementable) or "beta" (no-op)
    badge     — optional small text like "rank 5/30"
    color     — "good", "bad", "mid", or None (used by UI)
    reason    — short text shown in the tooltip
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config import SUGGESTED_FILTERS_PER_MARKET

from . import rankings_service

logger = logging.getLogger(__name__)


@dataclass
class SuggestedChip:
    key: str
    label: str
    status: str  # "real" | "beta"
    badge: str | None = None
    color: str | None = None   # "good" | "bad" | "mid" | None
    reason: str = ""


# Labels for chip keys. Kept here (not in config) because they're purely
# presentational and nice to tweak without reloading constants.
_LABEL_BY_KEY: dict[str, str] = {
    "minutes_high":     "Minutes ≥ L10 avg",
    "home_away":        "Match next game loc.",
    "h2h":              "H2H vs current opp",
    "b2b":              "B2B only",
    "opp_pts_rank":     "Opp PTS allowed",
    "opp_reb_rank":     "Opp REB allowed",
    "opp_ast_rank":     "Opp AST allowed",
    "opp_3pm_rank":     "Opp 3PM allowed",
    "opp_stl_rank":     "Opp STL",
    "opp_blk_rank":     "Opp BLK",
    "opp_tov_rank":     "Opp forces TOV",
    "opp_pace_rank":    "Opp Pace",
    "opp_def_rtg_rank": "Opp DefRtg",
    "opp_3pa_allowed":  "Opp 3PA allowed",
    "def_vs_position":  "Def vs Position",
    "def_vs_dpt":       "Def vs DPT",
    "def_vs_csu":       "Def vs C&S",
    "def_vs_pull":      "Def vs Pull-Up",
}


def _color_for_rank(rank: int | None, out_of: int = 30) -> str | None:
    if rank is None:
        return None
    third = max(1, out_of // 3)
    if rank <= third:
        return "good"
    if rank > out_of - third:
        return "bad"
    return "mid"


def _build_rank_chip(key: str, rank_map: dict[int, int],
                     opponent_team_id: int | None) -> SuggestedChip | None:
    """Turn a chip key + opponent into a ranked SuggestedChip."""
    if not opponent_team_id or not rank_map:
        return SuggestedChip(
            key=key,
            label=_LABEL_BY_KEY.get(key, key),
            status="real",
            badge="rank n/a",
            color=None,
            reason="Ranking data unavailable for this context.",
        )
    rank = rank_map.get(int(opponent_team_id))
    color = _color_for_rank(rank)
    badge = f"#{rank}/30" if rank is not None else "rank n/a"
    reason = ("Filter to games where the opposing defence has this "
              "ranking tendency (1 = most favourable for the player).")
    return SuggestedChip(
        key=key,
        label=_LABEL_BY_KEY.get(key, key),
        status="real",
        badge=badge,
        color=color,
        reason=reason,
    )


def build_suggested(
    market_col: str,
    game_log: pd.DataFrame,
    opponent_team_id: int | None,
    season: str,
    season_type: str = "Regular Season",
    *,
    league_id: str = "00",
) -> list[SuggestedChip]:
    """Return an ordered list of SuggestedChip for the given market.

    Safe against missing data — every lookup degrades to a chip with a
    helpful badge rather than being dropped.
    """
    chips: list[SuggestedChip] = []
    keys = SUGGESTED_FILTERS_PER_MARKET.get(market_col, ("minutes_high", "home_away", "h2h"))

    # Pre-fetch rank maps once.
    rank_maps = rankings_service.build_chip_rank_maps(
        season, season_type=season_type, league_id=league_id,
    )

    for key in keys:
        label = _LABEL_BY_KEY.get(key, key)

        if key == "minutes_high":
            chips.append(SuggestedChip(
                key=key, label=label, status="real",
                badge=None, color="mid",
                reason=("Keep games where MIN is at or above the player's "
                        "last-10 average. Helps filter out garbage-time outliers."),
            ))

        elif key == "home_away":
            chips.append(SuggestedChip(
                key=key, label=label, status="real",
                reason=("Narrow to games in the same location as the next "
                        "scheduled game (home vs away)."),
            ))

        elif key == "h2h":
            chips.append(SuggestedChip(
                key=key, label=label, status="real",
                reason="Only games vs. the current / upcoming opponent.",
            ))

        elif key == "b2b":
            chips.append(SuggestedChip(
                key=key, label=label, status="real",
                reason="Only games played on the second night of a back-to-back.",
            ))

        elif key in rankings_service.describe_supported_chips():
            chip = _build_rank_chip(key, rank_maps.get(key, {}), opponent_team_id)
            if chip is not None:
                chips.append(chip)

        else:
            # Beta: Def vs Position / DPT / CSU / Pull / 3PA-allowed etc.
            chips.append(SuggestedChip(
                key=key, label=label, status="beta",
                reason=("Requires play-type or positional defensive data that "
                        "isn't reliably available from free nba_api endpoints."),
            ))

    return chips
