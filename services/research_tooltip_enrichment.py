"""Research bar-chart: tooltip-only columns (final margin, Q1 fouls, player tracking).

Uses the same cached services as the rest of the app:

* ``linescore_service.final_scoring_margin_for_team`` — **team** final margin from
  BoxScore LineScore (not player ``PLUS_MINUS``).
* ``partial_game_service`` PlayByPlayV3 — foul counts in period 1.
* ``playertrack_service.player_track_stats_for_player`` — ``BoxScorePlayerTrackV3``
  for rebound chances (**REB**) and passes / optional potential assists (**AST**),
  one fetch per unique game id when enabled via ``TOOLTIP_ENRICH_REBOUND_CHANCES``
  (see ``config`` — governs all optional player-track tooltip fields).

Runs once per **unique game id** in the plotted sample so hover formatting stays cheap.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import TOOLTIP_ENRICH_REBOUND_CHANCES
from services.gamelog_service import team_id_from_abbr
from services.linescore_service import final_scoring_margin_for_team
from services.partial_game_service import (
    SCOPE_1Q_PERIODS,
    count_player_fouls_in_periods,
    fetch_playbyplay_v3_dataframe,
)
from services.playertrack_service import (
    normalize_game_date_iso,
    player_track_stats_for_player,
    potential_assists_for_player_on_date,
    potential_assists_is_cached,
)
from utils.formatting import infer_player_team_abbr_from_row

logger = logging.getLogger(__name__)

TOOLTIP_TEAM_MARGIN = "TOOLTIP_TEAM_MARGIN"
TOOLTIP_Q1_PF = "TOOLTIP_Q1_PF"
TOOLTIP_REB_CHANCES_TOTAL = "TOOLTIP_REB_CHANCES_TOTAL"
TOOLTIP_PASSES_MADE = "TOOLTIP_PASSES_MADE"
TOOLTIP_POTENTIAL_AST = "TOOLTIP_POTENTIAL_AST"
TOOLTIP_DNP_FG3M = "TOOLTIP_DNP_FG3M"


def _game_id_z10(raw: Any) -> str:
    s = "".join(ch for ch in str(raw) if ch.isdigit())
    if not s:
        return ""
    tail = s[-10:] if len(s) >= 10 else s
    return tail.zfill(10)


def _teammate_played_game(game_ids: Any, gid: str) -> bool:
    """True when ``gid`` appears in a teammate's ``GAME_IDS`` set/list."""
    if game_ids is None:
        return False
    try:
        if pd.isna(game_ids):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(game_ids, set):
        ids = {str(x).zfill(10) for x in game_ids}
    elif isinstance(game_ids, (list, tuple)):
        ids = {str(x).zfill(10) for x in game_ids}
    else:
        ids = {str(game_ids).zfill(10)}
    return gid in ids


def _stat_avg_from_teammate_row(stat_avgs: Any, stat_col: str) -> float | None:
    """Read one season-average stat from a teammate ``STAT_AVGS`` cell."""
    if stat_avgs is None:
        return None
    try:
        if pd.isna(stat_avgs):
            return None
    except (TypeError, ValueError):
        pass
    avgs = stat_avgs
    if isinstance(avgs, str):
        try:
            import json

            avgs = json.loads(avgs)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if not isinstance(avgs, dict):
        return None
    raw = avgs.get(stat_col)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    return v


def dnp_teammates_for_game(
    teammate_df: pd.DataFrame | None,
    game_id: str,
    *,
    stat_col: str = "FG3M",
    max_rows: int = 5,
) -> list[dict[str, Any]]:
    """Teammates who did not play ``game_id``, with season avg for ``stat_col``."""
    if teammate_df is None or teammate_df.empty or not game_id:
        return []
    gid = _game_id_z10(game_id)
    if len(gid) != 10:
        return []

    entries: list[dict[str, Any]] = []
    for _, tm in teammate_df.iterrows():
        if _teammate_played_game(tm.get("GAME_IDS"), gid):
            continue
        name = str(tm.get("PLAYER") or "").strip()
        if not name:
            continue
        avg = _stat_avg_from_teammate_row(tm.get("STAT_AVGS"), stat_col)
        entries.append({"name": name, "avg": avg})

    def _sort_key(item: dict[str, Any]) -> float:
        v = item.get("avg")
        try:
            return float(v) if v is not None else -1.0
        except (TypeError, ValueError):
            return -1.0

    entries.sort(key=_sort_key, reverse=True)
    return entries[: max(0, int(max_rows))]


def enrich_research_chart_tooltip_columns(
    df: pd.DataFrame,
    *,
    player_id: int,
    league_id: str = "00",
    stat_col: str | None = None,
    teammate_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Add tooltip columns when APIs succeed.

    ``TOOLTIP_REB_CHANCES_TOTAL`` is filled when ``stat_col == \"REB\"``.
    ``TOOLTIP_PASSES_MADE`` / ``TOOLTIP_POTENTIAL_AST`` when ``stat_col == \"AST\"``
    (same env gate as rebound tracking).
    """
    if df is None or df.empty:
        return df

    game_id_col = "Game_ID" if "Game_ID" in df.columns else "GAME_ID"
    if game_id_col not in df.columns:
        return df

    out = df.copy()

    out[TOOLTIP_TEAM_MARGIN] = pd.NA
    out[TOOLTIP_Q1_PF] = pd.NA
    want_reb_chances = (
        TOOLTIP_ENRICH_REBOUND_CHANCES
        and stat_col is not None
        and str(stat_col).strip().upper() == "REB"
    )
    want_ast_track = (
        TOOLTIP_ENRICH_REBOUND_CHANCES
        and stat_col is not None
        and str(stat_col).strip().upper() == "AST"
    )
    if want_reb_chances:
        out[TOOLTIP_REB_CHANCES_TOTAL] = pd.NA
    want_fg3_dnp = (
        stat_col is not None
        and str(stat_col).strip().upper() == "FG3M"
        and teammate_df is not None
        and not teammate_df.empty
    )
    if want_ast_track:
        out[TOOLTIP_PASSES_MADE] = pd.NA
        out[TOOLTIP_POTENTIAL_AST] = pd.NA
    if want_fg3_dnp:
        out[TOOLTIP_DNP_FG3M] = pd.NA

    if player_id <= 0:
        return out

    gids_series = out[game_id_col].map(_game_id_z10)
    # Avoid .str accessor edge cases when GAME_ID is null or non-string-backed.
    gid_ok = gids_series.map(lambda x: isinstance(x, str) and len(x) == 10)
    gids = pd.unique(gids_series[gid_ok])

    def _gid_iso_date(gid: str) -> str:
        mask = gids_series == gid
        if not bool(mask.any()):
            return ""
        sample = out.loc[mask].iloc[0]
        iso = normalize_game_date_iso(sample.get("GAME_DATE") or sample.get("game_date"))
        return iso or ""

    # Newest games first so the visible chart tail gets potential-assist data first.
    gids_sorted = sorted(gids, key=_gid_iso_date, reverse=True)

    # Limit live (uncached) passing-day fetches per render (one API call per date).
    _PA_LIVE_BUDGET = 20
    pa_live_remaining = [_PA_LIVE_BUDGET]
    pa_dates_fetched: set[str] = set()

    for gid in gids_sorted:
        if not gid:
            continue
        try:
            _enrich_tooltip_for_game(
                out=out,
                gid=gid,
                game_id_col=game_id_col,
                player_id=int(player_id),
                league_id=league_id,
                want_reb_chances=want_reb_chances,
                want_ast_track=want_ast_track,
                want_fg3_dnp=want_fg3_dnp,
                teammate_df=teammate_df,
                pa_live_remaining=pa_live_remaining,
                pa_dates_fetched=pa_dates_fetched,
            )
        except Exception as exc:
            logger.warning("tooltip enrichment failed for game %s: %s", gid, exc)

    return out


def _enrich_tooltip_for_game(
    *,
    out: pd.DataFrame,
    gid: str,
    game_id_col: str,
    player_id: int,
    league_id: str,
    want_reb_chances: bool,
    want_ast_track: bool,
    want_fg3_dnp: bool = False,
    teammate_df: pd.DataFrame | None = None,
    pa_live_remaining: list[int] | None = None,
    pa_dates_fetched: set[str] | None = None,
) -> None:
    """Fill tooltip columns for rows matching ``gid`` (mutates ``out``)."""
    mask = out[game_id_col].map(_game_id_z10) == gid
    if not bool(mask.any()):
        return
    sample = out.loc[mask].iloc[0]
    abbr = infer_player_team_abbr_from_row(sample)
    tid = team_id_from_abbr(abbr, league_id)

    margin: int | None = None
    if tid is not None:
        margin = final_scoring_margin_for_team(gid, int(tid))
    if margin is not None:
        out.loc[mask, TOOLTIP_TEAM_MARGIN] = int(margin)

    try:
        pbp = fetch_playbyplay_v3_dataframe(gid)
        q1_pf = count_player_fouls_in_periods(
            pbp if pbp is not None else pd.DataFrame(),
            player_id=int(player_id),
            periods=SCOPE_1Q_PERIODS,
        )
        out.loc[mask, TOOLTIP_Q1_PF] = float(q1_pf)
    except Exception as exc:  # pragma: no cover - network / parse
        logger.debug("tooltip Q1 PF skip game=%s: %s", gid, exc)

    if want_reb_chances or want_ast_track:
        try:
            stats = player_track_stats_for_player(gid, int(player_id))
            if want_reb_chances and stats.get("rebound_chances_total") is not None:
                out.loc[mask, TOOLTIP_REB_CHANCES_TOTAL] = float(
                    stats["rebound_chances_total"],
                )
            if want_ast_track:
                if stats.get("passes") is not None:
                    out.loc[mask, TOOLTIP_PASSES_MADE] = float(stats["passes"])
        except Exception as exc:  # pragma: no cover
            logger.debug("tooltip player track skip game=%s: %s", gid, exc)

    if want_ast_track:
        try:
            iso_date = normalize_game_date_iso(
                sample.get("GAME_DATE") or sample.get("game_date"),
            )
            if iso_date:
                already_cached = potential_assists_is_cached(
                    int(player_id),
                    iso_date,
                    game_id=gid,
                    league_id=league_id,
                )
                day_already_fetched = (
                    pa_dates_fetched is not None and iso_date in pa_dates_fetched
                )
                can_fetch_live = pa_live_remaining and pa_live_remaining[0] > 0
                if already_cached or day_already_fetched or can_fetch_live:
                    if (
                        not already_cached
                        and not day_already_fetched
                        and can_fetch_live
                        and pa_live_remaining
                    ):
                        pa_live_remaining[0] -= 1
                        if pa_dates_fetched is not None:
                            pa_dates_fetched.add(iso_date)
                    pa = potential_assists_for_player_on_date(
                        player_id=int(player_id),
                        game_date=iso_date,
                        game_id=gid,
                        league_id=league_id,
                    )
                    if pa is not None:
                        out.loc[mask, TOOLTIP_POTENTIAL_AST] = float(pa)
        except Exception as exc:  # pragma: no cover
            logger.debug("tooltip potential_ast skip game=%s: %s", gid, exc)

    if want_fg3_dnp and teammate_df is not None:
        try:
            dnp = dnp_teammates_for_game(teammate_df, gid, stat_col="FG3M")
            if dnp:
                for idx in out.index[mask]:
                    out.at[idx, TOOLTIP_DNP_FG3M] = dnp
        except Exception as exc:  # pragma: no cover
            logger.debug("tooltip DNP FG3M skip game=%s: %s", gid, exc)
