"""Per-game line score — lazy 1H/2H team scoring.

The free ``TeamGameLog`` endpoint gives per-game totals but no period
breakdown. To compute real 1st-half and 2nd-half totals/spreads we pull
``BoxScoreSummaryV2`` per game and read the ``LineScore`` frame, which
contains ``PTS_QTR1..PTS_QTR4`` for both teams.

Because that's one API call per game (~82 per team per season) this
service is opt-in. Call ``enrich_team_log_with_halves`` only when the
user actually selects a 1H/2H market. Results are cached permanently
(historical final line scores never change).

If the network is down or a single game fetch fails, we skip that row
rather than aborting the whole enrichment. The resulting DataFrame
returns the same row count as the input with NaN in the half columns
for any game we couldn't fetch.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from config import CACHE_TTL_LINESCORE
from database.cache_db import get_cached, set_cached

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_NAMESPACE = "linescore"


def _cache_key(game_id: str) -> str:
    return f"linescore:{game_id}"


def fetch_line_score(game_id: str) -> pd.DataFrame | None:
    """Return the LineScore DataFrame for a single game, or None on failure.

    Columns we care about: ``TEAM_ID``, ``PTS_QTR1``..``PTS_QTR4``, ``PTS_OT1+``.
    Cached permanently — regular-season games don't change once final.
    """
    if not game_id:
        return None
    key = _cache_key(str(game_id))

    cached = get_cached(_NAMESPACE, key, ttl_seconds=CACHE_TTL_LINESCORE)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        from nba_api.stats.endpoints import boxscoresummaryv2

        ep = call_endpoint(boxscoresummaryv2.BoxScoreSummaryV2, game_id=str(game_id))
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        logger.info("BoxScoreSummaryV2(%s) skipped: %s", game_id, exc)
        return None

    # BoxScoreSummaryV2 returns several frames; LineScore is consistently
    # the one with PTS_QTR1..4 columns.
    line_df = None
    for f in frames or []:
        if isinstance(f, pd.DataFrame) and "PTS_QTR1" in f.columns:
            line_df = f
            break
    if line_df is None or line_df.empty:
        return None

    try:
        set_cached(_NAMESPACE, key, line_df.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not cache line score for %s: %s", game_id, exc)
    return line_df


def _zfill_gid(game_id: Any) -> str:
    """Normalize game id to 10-digit string for ``BoxScoreSummaryV2``."""
    s = "".join(ch for ch in str(game_id) if ch.isdigit())
    if not s:
        t = str(game_id).strip()
        return t.zfill(10) if t else ""
    tail = s[-10:] if len(s) >= 10 else s
    return tail.zfill(10)


def final_scoring_margin_for_team(game_id: Any, team_id: int | None) -> int | None:
    """Final scoring margin for ``team_id``: team PTS minus opponent PTS (game result).

    Pulled from cached ``LineScore`` — **not** the player's on-court ``PLUS_MINUS``
    from the player game log. Returns ``None`` if line score is unavailable.
    """
    if team_id is None:
        return None
    gid = _zfill_gid(game_id)
    if not gid:
        return None
    df = fetch_line_score(gid)
    if df is None or df.empty or "TEAM_ID" not in df.columns or "PTS" not in df.columns:
        return None
    tid = int(team_id)
    try:
        mine = df.loc[df["TEAM_ID"] == tid, "PTS"]
        other = df.loc[df["TEAM_ID"] != tid, "PTS"]
        if mine.empty or other.empty:
            return None
        my_pts = float(mine.iloc[0])
        op_pts = float(other.iloc[0])
    except (TypeError, ValueError, IndexError):
        return None
    if pd.isna(my_pts) or pd.isna(op_pts):
        return None
    return int(round(my_pts - op_pts))


def _half_splits_for_team(line_df: pd.DataFrame, team_id: int
                          ) -> tuple[float | None, float | None]:
    """Return (pts_1h, pts_2h) for a given team_id out of the LineScore.

    OT points are folded into 2H because from a market perspective the
    2nd-half total/spread is typically defined as "everything after
    halftime" including any overtime.
    """
    if line_df is None or line_df.empty or "TEAM_ID" not in line_df.columns:
        return (None, None)
    row = line_df[line_df["TEAM_ID"] == team_id]
    if row.empty:
        return (None, None)
    r = row.iloc[0]

    def _num(col: str) -> float:
        if col not in r.index:
            return 0.0
        v = r.get(col)
        try:
            return float(v) if pd.notna(v) else 0.0
        except (TypeError, ValueError):
            return 0.0

    pts_1h = _num("PTS_QTR1") + _num("PTS_QTR2")
    pts_2h = _num("PTS_QTR3") + _num("PTS_QTR4")
    # Any overtime columns present — names vary a little by season.
    for c in line_df.columns:
        if c.startswith("PTS_OT"):
            pts_2h += _num(c)
    return (pts_1h, pts_2h)


def enrich_team_log_with_halves(team_log: pd.DataFrame, team_id: int,
                                progress_callback: Callable[[int, int], None] | None = None,
                                max_games: int = 40) -> pd.DataFrame:
    """Add ``PTS_1H``, ``PTS_2H``, ``OPP_PTS_1H``, ``OPP_PTS_2H`` columns.

    Only the most-recent ``max_games`` games are fetched to keep the cold
    path snappy (halves are used for projection trends, not exhaustive
    season reports). Games that fail to fetch leave NaN in those cells
    and the rest of the row is untouched.

    The per-game call is cached permanently so a second call for the same
    team + season is effectively free.
    """
    if team_log is None or team_log.empty or "GAME_ID" not in team_log.columns:
        return team_log

    df = team_log.copy()
    # Initialise columns (float so NaNs are first-class).
    for c in ("PTS_1H", "PTS_2H", "OPP_PTS_1H", "OPP_PTS_2H",
              "TOTAL_1H", "TOTAL_2H", "MARGIN_1H", "MARGIN_2H"):
        if c not in df.columns:
            df[c] = pd.NA

    # Fetch for the newest N games only. The DF is already newest-first
    # from team_service._normalize.
    subset = df.head(max_games)
    total = len(subset)

    for i, (idx, row) in enumerate(subset.iterrows(), start=1):
        gid = str(row.get("GAME_ID", "") or "")
        if not gid:
            if progress_callback:
                progress_callback(i, total)
            continue

        line_df = fetch_line_score(gid)
        if line_df is None:
            if progress_callback:
                progress_callback(i, total)
            continue

        team_1h, team_2h = _half_splits_for_team(line_df, int(team_id))
        # Opponent team id: 2 rows in line_df, grab the other.
        opp_ids = [int(t) for t in line_df["TEAM_ID"].tolist()
                   if int(t) != int(team_id)]
        opp_id = opp_ids[0] if opp_ids else None
        if opp_id is not None:
            opp_1h, opp_2h = _half_splits_for_team(line_df, opp_id)
        else:
            opp_1h = opp_2h = None

        if team_1h is not None:
            df.at[idx, "PTS_1H"] = team_1h
        if team_2h is not None:
            df.at[idx, "PTS_2H"] = team_2h
        if opp_1h is not None:
            df.at[idx, "OPP_PTS_1H"] = opp_1h
        if opp_2h is not None:
            df.at[idx, "OPP_PTS_2H"] = opp_2h
        if team_1h is not None and opp_1h is not None:
            df.at[idx, "TOTAL_1H"] = team_1h + opp_1h
            df.at[idx, "MARGIN_1H"] = team_1h - opp_1h
        if team_2h is not None and opp_2h is not None:
            df.at[idx, "TOTAL_2H"] = team_2h + opp_2h
            df.at[idx, "MARGIN_2H"] = team_2h - opp_2h

        if progress_callback:
            progress_callback(i, total)

    # Coerce for downstream math.
    for c in ("PTS_1H", "PTS_2H", "OPP_PTS_1H", "OPP_PTS_2H",
              "TOTAL_1H", "TOTAL_2H", "MARGIN_1H", "MARGIN_2H"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    return df


def halves_are_cached(team_log: pd.DataFrame, max_games: int = 40) -> bool:
    """True iff every recent game already has a cached line score.

    A cheap way to skip the progress-bar UI when the user has already
    loaded this team's halves in a previous session.
    """
    if team_log is None or team_log.empty or "GAME_ID" not in team_log.columns:
        return True
    for gid in team_log.head(max_games)["GAME_ID"].astype(str):
        if get_cached(_NAMESPACE, _cache_key(gid),
                      ttl_seconds=CACHE_TTL_LINESCORE) is None:
            return False
    return True
