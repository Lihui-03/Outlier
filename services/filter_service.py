"""Sample-filtering pipeline.

Everything here is a **pure** function of a game-log DataFrame and a
`FilterState` snapshot. No Streamlit, no network, no cache — which makes
every step unit-testable in isolation.

Order of operations for `apply_all_filters`:

    1. season         — when "All" we merge multiple seasons upstream;
                        this step is a no-op for now (kept for symmetry
                        and future extension).
    2. regular/playoff — uses SEASON_TYPE column if present.
    3. home/away, win/loss, H2H, B2B, rest, margin — all narrow the sample.
    4. teammate WITH/WITHOUT — intersect with pre-fetched teammate game ids.
    5. opp-rank chips — narrow to games vs. "favourable" opposing teams
                        (top-10 in the chosen stat).
    6. stat ranges    — per-column range filter.
    7. game_count     — applied LAST so the "last 10 games" reflects the
                        already-narrowed sample, not the raw log.

`apply_all_filters` returns *both* the filtered sample and the excluded
sample (defined as rows removed by steps 2-6; the game_count cap does
not contribute to "excluded" because a capped game is still a hit for
the filter, just not shown).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

# The nba_api sometimes spells the column "Game_ID" and sometimes "GAME_ID".
# We always try the friendly spelling first, then fall back to the raw one.
GAME_ID_COLS = ("Game_ID", "GAME_ID")


def _game_id_col(df: pd.DataFrame) -> str | None:
    for c in GAME_ID_COLS:
        if c in df.columns:
            return c
    return None


# ---------------------------------------------------------------------------
# Individual filter functions
# ---------------------------------------------------------------------------


def apply_season_filter(df: pd.DataFrame, selection: str) -> pd.DataFrame:
    """Narrow by season. We expect upstream fetching to already have
    scoped the log to the requested seasons (see data_service loaders),
    so this function is a no-op stub: by the time we reach it, `df` is
    already the right slice. Kept for future multi-season combined logs.
    """
    return df


def apply_regular_playoff_filter(df: pd.DataFrame, mode: str | None) -> pd.DataFrame:
    if not mode or "SEASON_TYPE" not in df.columns:
        return df
    if mode == "regular":
        return df[df["SEASON_TYPE"].astype(str).str.lower().eq("regular season")]
    if mode == "playoffs":
        return df[df["SEASON_TYPE"].astype(str).str.lower().eq("playoffs")]
    return df


def apply_home_away_filter(df: pd.DataFrame, mode: str | None) -> pd.DataFrame:
    if not mode or "IS_HOME" not in df.columns:
        return df
    if mode == "home":
        return df[df["IS_HOME"] == True]
    if mode == "away":
        return df[df["IS_HOME"] == False]
    return df


def apply_win_loss_filter(df: pd.DataFrame, mode: str | None) -> pd.DataFrame:
    if not mode or "IS_WIN" not in df.columns:
        return df
    if mode == "win":
        return df[df["IS_WIN"] == True]
    if mode == "loss":
        return df[df["IS_WIN"] == False]
    return df


def apply_h2h_filter(df: pd.DataFrame, opponent_abbr: str | None) -> pd.DataFrame:
    if not opponent_abbr or "OPPONENT" not in df.columns:
        return df
    return df[df["OPPONENT"].fillna("").str.upper() == opponent_abbr.upper()]


def apply_b2b_filter(df: pd.DataFrame, b2b_only: bool) -> pd.DataFrame:
    """Keep only games played on the second night of a back-to-back.

    We define B2B as REST_DAYS <= 1 (i.e. played the prior day). Rows
    with missing REST_DAYS are excluded (they're the first game of a
    season with no prior-game reference).
    """
    if not b2b_only or "REST_DAYS" not in df.columns:
        return df
    rest = pd.to_numeric(df["REST_DAYS"], errors="coerce")
    return df[(rest.notna()) & (rest <= 1)]


def apply_rest_filter(df: pd.DataFrame, rest_min: int | None,
                      rest_max: int | None) -> pd.DataFrame:
    if "REST_DAYS" not in df.columns:
        return df
    if rest_min is None and rest_max is None:
        return df
    rest = pd.to_numeric(df["REST_DAYS"], errors="coerce")
    mask = rest.notna()
    if rest_min is not None:
        mask &= rest >= rest_min
    if rest_max is not None:
        mask &= rest <= rest_max
    return df[mask]


def apply_margin_filter(df: pd.DataFrame, mode: str | None) -> pd.DataFrame:
    """Filter by absolute point margin. 'close' = |PLUS_MINUS| <= 5,
    'blowout' = |PLUS_MINUS| > 15."""
    if not mode or "PLUS_MINUS" not in df.columns:
        return df
    pm = pd.to_numeric(df["PLUS_MINUS"], errors="coerce").abs()
    if mode == "close":
        return df[pm <= 5]
    if mode == "blowout":
        return df[pm > 15]
    return df


def apply_teammate_filter(df: pd.DataFrame,
                          teammate_game_ids: dict[int, set[str]],
                          modes: dict[int, str]) -> pd.DataFrame:
    """Intersect the game log with each teammate's game-id set.

    `teammate_game_ids` maps teammate_player_id -> set of normalized
    game-id strings (zero-padded). `modes` is {pid: "with"|"without"}.

    Empty game-id sets are treated as 'data unavailable' and skipped so
    the filter never silently drops the whole sample when a teammate
    fetch failed.
    """
    if not modes:
        return df
    col = _game_id_col(df)
    if not col:
        return df

    gids = df[col].astype(str).str.zfill(10)
    mask = pd.Series([True] * len(df), index=df.index)
    for pid, mode in modes.items():
        gid_set = teammate_game_ids.get(pid)
        if not gid_set:
            logger.info("No game-id set for teammate %s; skipping filter", pid)
            continue
        in_set = gids.isin(gid_set)
        if mode == "with":
            mask &= in_set
        elif mode == "without":
            mask &= ~in_set
    return df[mask]


def apply_stat_range_filter(df: pd.DataFrame, col: str,
                            lo: float | None, hi: float | None) -> pd.DataFrame:
    if col not in df.columns or (lo is None and hi is None):
        return df
    values = pd.to_numeric(df[col], errors="coerce")
    mask = values.notna()
    if lo is not None:
        mask &= values >= lo
    if hi is not None:
        mask &= values <= hi
    return df[mask]


def apply_all_stat_ranges(df: pd.DataFrame,
                          ranges: dict[str, tuple[float | None, float | None]]) -> pd.DataFrame:
    for col, (lo, hi) in (ranges or {}).items():
        df = apply_stat_range_filter(df, col, lo, hi)
    return df


def apply_opp_rank_filter(df: pd.DataFrame,
                          active_chips: dict[str, bool],
                          chip_to_rank_map: dict[str, dict[int, int]],
                          favourable_rank_cutoff: int = 10) -> pd.DataFrame:
    """Narrow to games against opponents whose rank (for the chip's stat)
    is in the top `favourable_rank_cutoff` (i.e. easiest matchups).

    `chip_to_rank_map[chip_key]` is a {team_id: rank} dict precomputed by
    `services/rankings_service.py`. Chips not in that map are skipped
    (beta or currently unavailable data).
    """
    if not active_chips:
        return df
    if "OPP_TEAM_ID" not in df.columns:
        # We fall back to matching by abbreviation if opp id isn't present.
        logger.debug("OPP_TEAM_ID missing; opp_rank filter is a no-op")
        return df

    mask = pd.Series([True] * len(df), index=df.index)
    for chip, enabled in active_chips.items():
        if not enabled:
            continue
        ranks = chip_to_rank_map.get(chip)
        if not ranks:
            continue
        good_team_ids = {tid for tid, r in ranks.items() if r <= favourable_rank_cutoff}
        if not good_team_ids:
            continue
        mask &= df["OPP_TEAM_ID"].isin(good_team_ids)
    return df[mask]


def apply_game_count_filter(df: pd.DataFrame, mode: str,
                            custom_n: int = 15) -> pd.DataFrame:
    """Cap to the most-recent N games. df is expected newest-first.

    "Max" returns the full frame. Digit modes (``"30"``, ``"5"``, ...) parse as
    ints for ``head(n)``. ``"Custom"`` uses ``custom_n``. Unknown modes = Max.
    """
    if df is None or df.empty:
        return df
    if not mode or mode == "Max":
        return df
    if mode == "Custom":
        n = max(1, int(custom_n))
    else:
        try:
            n = int(mode)
        except (TypeError, ValueError):
            return df
    return df.head(n)


# ---------------------------------------------------------------------------
# Composite pipeline
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    filtered: pd.DataFrame
    excluded: pd.DataFrame
    # Pre-cap count (after all narrowing filters but before game_count).
    narrowed_total: int


def apply_all_filters(
    df: pd.DataFrame,
    state: Any,
    *,
    opponent_abbr: str | None = None,
    teammate_game_ids: dict[int, set[str]] | None = None,
    opp_rank_maps: dict[str, dict[int, int]] | None = None,
) -> FilterResult:
    """Run the full filter pipeline.

    Returns the filtered + excluded samples plus the pre-cap total so
    the UI can render "12 / 20" with the correct denominator even when
    the game_count mode is "10" or "Custom".
    """
    if df is None or df.empty:
        empty = df if df is not None else pd.DataFrame()
        return FilterResult(filtered=empty, excluded=empty, narrowed_total=0)

    narrowed = df
    narrowed = apply_season_filter(narrowed, getattr(state, "season_selection", None))
    narrowed = apply_regular_playoff_filter(narrowed, getattr(state, "regular_playoff", None))
    narrowed = apply_home_away_filter(narrowed, getattr(state, "home_away", None))
    narrowed = apply_win_loss_filter(narrowed, getattr(state, "win_loss", None))
    if getattr(state, "h2h_only", False):
        narrowed = apply_h2h_filter(narrowed, opponent_abbr)
    narrowed = apply_b2b_filter(narrowed, getattr(state, "b2b_only", False))
    narrowed = apply_rest_filter(
        narrowed,
        getattr(state, "rest_min", None),
        getattr(state, "rest_max", None),
    )
    narrowed = apply_margin_filter(narrowed, getattr(state, "margin_filter", None))
    narrowed = apply_teammate_filter(
        narrowed,
        teammate_game_ids or {},
        getattr(state, "teammate_modes", {}) or {},
    )
    narrowed = apply_opp_rank_filter(
        narrowed,
        getattr(state, "opp_rank_active", {}) or {},
        opp_rank_maps or {},
    )
    narrowed = apply_all_stat_ranges(narrowed, getattr(state, "stat_ranges", {}) or {})

    # Excluded = full log minus the narrowed (pre-cap) sample. We use
    # index difference so we don't accidentally treat repeat rows as
    # duplicates.
    excluded_idx = df.index.difference(narrowed.index)
    excluded = df.loc[excluded_idx]

    # Now cap; excluded is unchanged by this step on purpose.
    capped = apply_game_count_filter(
        narrowed,
        getattr(state, "game_count_mode", "Max"),
        custom_n=getattr(state, "game_count_custom_n", 15),
    )

    return FilterResult(
        filtered=capped,
        excluded=excluded,
        narrowed_total=int(len(narrowed)),
    )
