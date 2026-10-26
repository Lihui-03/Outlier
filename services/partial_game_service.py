"""True 1Q / first-half player stats derived from NBA **PlayByPlayV3** events.

Older code used ``BoxScoreTraditionalV2`` with ``StartPeriod``/``EndPeriod``.
That endpoint is deprecated and **no longer publishes reliable period slices**
for newer seasons — and ``PlayByPlayV2`` is empty against the modern API [#591].

This module therefore:

    * Fetches full-game Play-by-Play **V3** (free stats.nba.com).
    * Filters rows by regulation quarter (period 1 = 1Q; periods {1,2} = first half).
    * Aggregates **PTS**, **FG3M**, **REB**, **AST** per player using structured
      fields wherever possible.

Assists credit the passer **only via the standardized trailing parenthetical**
in ``description``: ``(...) (LastName N AST)``.  The API does **not** expose an
assist player id alongside the shooter; ``familyName`` from
``BoxScoreTraditionalV3`` (full game roster) resolves the passer on the scorer's team.

Caches
──────
* ``pbp_v3`` namespace — serialized PlayByPlay dataframe per ``game_id``.
* ``box_roster_v3`` namespace — roster rows (player + team + minutes) per ``game_id``.

Both are effectively permanent (``ttl_seconds=None``) because historical games
do not change.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

import pandas as pd

from database.cache_db import get_cached, set_cached

from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)

_PBP_NAMESPACE = "pbp_v3"
_ROSTER_NAMESPACE = "box_roster_v3"

_AST_INNER_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<count>\d+)\s+AST$",
)


def assist_name_last_from_description(description: str) -> str | None:
    """Return the passer label from the final ``(... N AST)`` segment."""
    s = description.strip()
    stack: list[int] = []
    last_match: str | None = None
    for idx, ch in enumerate(s):
        if ch == "(":
            stack.append(idx + 1)
        elif ch == ")":
            if not stack:
                continue
            start = stack.pop()
            inner = s[start:idx].strip()
            tm = _AST_INNER_RE.match(inner)
            if tm:
                last_match = tm.group("name").strip()
    return last_match


# ---------------------------------------------------------------------------
# Public scope names (for tests / debugging)
# ---------------------------------------------------------------------------

SCOPE_1Q_PERIODS: frozenset[int] = frozenset({1})
SCOPE_1H_PERIODS: frozenset[int] = frozenset({1, 2})

# Output column mapping: base stat -> gamelog column
SCOPE_OUTPUT_COLS: tuple[tuple[frozenset[int], dict[str, str]], ...] = (
    (
        SCOPE_1Q_PERIODS,
        {"PTS": "Q1_PTS", "REB": "Q1_REB", "AST": "Q1_AST", "FG3M": "Q1_FG3M"},
    ),
    (
        SCOPE_1H_PERIODS,
        {"PTS": "H1_PTS", "REB": "H1_REB", "AST": "H1_AST", "FG3M": "H1_FG3M"},
    ),
)


def _game_id_zfill(game_id: str | int) -> str:
    s = "".join(ch for ch in str(game_id) if ch.isdigit())
    if not s:
        return str(game_id).zfill(10)
    tail = s[-10:] if len(s) >= 10 else s
    return tail.zfill(10)


def _pbp_cache_key(game_id_z: str) -> str:
    return game_id_z


def _roster_cache_key(game_id_z: str) -> str:
    return game_id_z


def _collapse_family_key(name: str) -> str:
    """Normalize 'Porter Jr.' / 'Smith' for robust roster ↔ assist-tag matching."""
    return "".join(name.upper().split())


def _played_from_minutes(minutes_str: Any) -> bool:
    if minutes_str is None or (isinstance(minutes_str, float) and pd.isna(minutes_str)):
        return False
    u = str(minutes_str).strip().upper()
    if not u:
        return False
    if "DNP" in u or "DND" in u or "NWT" in u or "INACTIVE" in u:
        return False
    return True


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return default
        return int(x)
    except (TypeError, ValueError):
        return default


def fetch_playbyplay_v3_dataframe(
    game_id: str | int,
    *,
    used_network: list[bool] | None = None,
) -> pd.DataFrame:
    """Return full-game PlayByPlay rows for ``game_id`` (cached)."""
    gid = _game_id_zfill(game_id)
    key = _pbp_cache_key(gid)
    cached = get_cached(_PBP_NAMESPACE, key, ttl_seconds=None)
    if cached is not None:
        if used_network is not None:
            used_network.append(False)
        logger.debug("partial_game PBP cache HIT game=%s", gid)
        return pd.DataFrame(cached)

    if used_network is not None:
        used_network.append(True)
    logger.info("partial_game PBP cache MISS game=%s — fetching PlayByPlayV3", gid)

    from nba_api.stats.endpoints import playbyplayv3

    try:
        ep = call_endpoint(
            playbyplayv3.PlayByPlayV3,
            game_id=gid,
        )
        df = ep.play_by_play.get_data_frame()
    except NBAApiError as exc:
        logger.warning("PlayByPlayV3 failed for %s: %s", gid, exc)
        return pd.DataFrame()

    if df is None or df.empty:
        logger.warning("PlayByPlayV3 returned empty frame for %s", gid)
        return pd.DataFrame()

    # JSON-serializable cache payload
    try:
        set_cached(_PBP_NAMESPACE, key, df.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not cache PBP for %s: %s", gid, exc)

    return df


def fetch_boxscore_roster_dataframe(
    game_id: str | int,
    *,
    used_network: list[bool] | None = None,
) -> pd.DataFrame:
    """Return PlayerStats rows (team + person + familyName + minutes) — cached."""
    gid = _game_id_zfill(game_id)
    key = _roster_cache_key(gid)
    cached = get_cached(_ROSTER_NAMESPACE, key, ttl_seconds=None)
    if cached is not None:
        if used_network is not None:
            used_network.append(False)
        logger.debug("partial_game roster cache HIT game=%s", gid)
        return pd.DataFrame(cached)

    if used_network is not None:
        used_network.append(True)
    logger.info("partial_game roster cache MISS game=%s — fetching BoxScoreTraditionalV3", gid)

    from nba_api.stats.endpoints import boxscoretraditionalv3

    try:
        ep = call_endpoint(
            boxscoretraditionalv3.BoxScoreTraditionalV3,
            game_id=gid,
        )
        df = ep.player_stats.get_data_frame()
    except NBAApiError as exc:
        logger.warning("BoxScoreTraditionalV3 roster failed for %s: %s", gid, exc)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    keep_cols = [c for c in ("teamId", "personId", "familyName", "nameI", "minutes")
                 if c in df.columns]
    slim = df[keep_cols].copy()

    try:
        set_cached(_ROSTER_NAMESPACE, key, slim.to_dict(orient="records"))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not cache roster for %s: %s", gid, exc)

    return slim


def resolve_assist_person_id(
    team_id: int,
    assist_name_raw: str,
    roster_df: pd.DataFrame,
) -> int | None:
    """Map assist text from PBP description to ``personId`` using roster family names."""
    if roster_df is None or roster_df.empty or not assist_name_raw.strip():
        return None
    want = _collapse_family_key(assist_name_raw.strip())
    if not want:
        return None

    sub = roster_df[roster_df["teamId"] == int(team_id)]
    if sub.empty:
        return None

    hits: list[int] = []
    for _, rr in sub.iterrows():
        fam = str(rr.get("familyName") or "").strip()
        if _collapse_family_key(fam) == want:
            hits.append(_safe_int(rr.get("personId")))

    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        logger.debug(
            "partial_game ambiguous assist tag %r on team %s — skipping credit",
            assist_name_raw, team_id,
        )
    return None


def aggregate_player_period_stats(
    pbp_df: pd.DataFrame,
    roster_df: pd.DataFrame,
    *,
    player_id: int,
    periods: frozenset[int],
    played_this_game: bool,
) -> dict[str, float] | None:
    """Aggregate PTS / REB / AST / FG3M for ``player_id`` within ``periods``.

    Returns ``None`` when the player did not dress (DNP) so callers can write NaN.
    """
    if not played_this_game:
        return None
    if pbp_df is None or pbp_df.empty:
        return {"PTS": 0.0, "REB": 0.0, "AST": 0.0, "FG3M": 0.0}

    sub = pbp_df[pbp_df["period"].map(_safe_int).isin(set(periods))].copy()
    if sub.empty:
        return {"PTS": 0.0, "REB": 0.0, "AST": 0.0, "FG3M": 0.0}

    pid = int(player_id)
    pts = 0.0
    reb = 0.0
    ast = 0.0
    fg3m = 0.0

    for _, row in sub.iterrows():
        act = str(row.get("actionType") or "").strip()
        person = _safe_int(row.get("personId"))
        team_id = _safe_int(row.get("teamId"))

        if act == "Made Shot":
            desc = str(row.get("description") or "").strip()
            if person == pid:
                sv = float(_safe_int(row.get("shotValue")))
                pts += sv
                if sv == 3.0:
                    fg3m += 1.0
            # Assists (passer is encoded in description, not as a player id).
            if person != 0:
                ast_nm = assist_name_last_from_description(desc)
                if ast_nm:
                    apid = resolve_assist_person_id(team_id, ast_nm, roster_df)
                    if apid == pid:
                        ast += 1.0

        elif act == "Free Throw" and person == pid:
            desc = str(row.get("description") or "")
            if desc.upper().startswith("MISS"):
                continue
            pts += 1.0

        elif act == "Rebound" and person == pid:
            reb += 1.0

    return {"PTS": pts, "REB": reb, "AST": ast, "FG3M": fg3m}


def count_player_fouls_in_periods(
    pbp_df: pd.DataFrame,
    *,
    player_id: int,
    periods: frozenset[int],
) -> int:
    """Count ``Foul`` actions credited to ``player_id`` within ``periods`` (e.g. 1Q).

    Uses the same PlayByPlayV3 feed as quarter PTS/REB — **personal/team fouls**
    appear as ``actionType`` ``Foul`` with ``personId`` as the fouler.
    """
    if pbp_df is None or pbp_df.empty:
        return 0
    sub = pbp_df[pbp_df["period"].map(_safe_int).isin(set(periods))]
    pid = int(player_id)
    n = 0
    for _, row in sub.iterrows():
        act = str(row.get("actionType") or "").strip().lower()
        if act != "foul":
            continue
        if _safe_int(row.get("personId")) != pid:
            continue
        n += 1
    return n


def _player_dressed(roster_df: pd.DataFrame, player_id: int) -> bool:
    if roster_df is None or roster_df.empty:
        return False
    row = roster_df[roster_df["personId"] == int(player_id)]
    if row.empty:
        return False
    return _played_from_minutes(row.iloc[0].get("minutes"))


def build_partial_game_row(
    game_id: str | int,
    player_id: int,
    periods: frozenset[int],
    *,
    pbp_df: pd.DataFrame | None = None,
    roster_df: pd.DataFrame | None = None,
) -> dict[str, float] | None:
    """Convenience: load/cache PBP + roster and aggregate one scope."""
    gid = _game_id_zfill(game_id)
    pbp = pbp_df if pbp_df is not None else fetch_playbyplay_v3_dataframe(gid)
    ros = roster_df if roster_df is not None else fetch_boxscore_roster_dataframe(gid)
    dressed = _player_dressed(ros, player_id)
    return aggregate_player_period_stats(
        pbp, ros, player_id=player_id, periods=periods, played_this_game=dressed,
    )


def partial_derived_stats_cached_for_log(game_log: pd.DataFrame) -> bool:
    """True when PBP + roster caches exist for every game in the log."""
    if game_log is None or game_log.empty:
        return True
    game_id_col = "Game_ID" if "Game_ID" in game_log.columns else "GAME_ID"
    if game_id_col not in game_log.columns:
        return False
    for raw_gid in game_log[game_id_col].dropna().unique().tolist():
        gid = _game_id_zfill(raw_gid)
        if get_cached(_PBP_NAMESPACE, _pbp_cache_key(gid), ttl_seconds=None) is None:
            return False
        if get_cached(_ROSTER_NAMESPACE, _roster_cache_key(gid), ttl_seconds=None) is None:
            return False
    return True


def enrich_game_log_partial_stats(
    game_log: pd.DataFrame,
    player_id: int,
    progress_callback: Callable[..., Any] | None = None,
) -> pd.DataFrame:
    """Add / refresh Q1_* and H1_* columns using PlayByPlayV3 derivation."""
    if game_log is None or game_log.empty:
        return game_log

    game_id_col = "Game_ID" if "Game_ID" in game_log.columns else "GAME_ID"
    if game_id_col not in game_log.columns:
        logger.warning("partial_game_service: missing %s — skipping enrichment", game_id_col)
        return game_log

    df = game_log.copy()

    # Ensure all partial columns exist
    for _, out_map in SCOPE_OUTPUT_COLS:
        for out_col in out_map.values():
            if out_col not in df.columns:
                df[out_col] = float("nan")

    game_ids = df[game_id_col].dropna().unique().tolist()
    total_games = len(game_ids)

    pbp_hits = 0
    pbp_misses = 0
    roster_hits = 0
    roster_misses = 0
    games_written = 0

    for i_g, raw_gid in enumerate(game_ids):
        gid = _game_id_zfill(raw_gid)
        mask = df[game_id_col].map(_game_id_zfill) == gid

        net_track: list[bool] = []
        pbp = fetch_playbyplay_v3_dataframe(gid, used_network=net_track)
        if net_track and not net_track[-1]:
            pbp_hits += 1
        elif net_track:
            pbp_misses += 1

        net_track_ro: list[bool] = []
        roster = fetch_boxscore_roster_dataframe(gid, used_network=net_track_ro)
        if net_track_ro and not net_track_ro[-1]:
            roster_hits += 1
        elif net_track_ro:
            roster_misses += 1

        if progress_callback:
            try:
                progress_callback(i_g + 1, total_games)
            except Exception:  # pragma: no cover
                pass

        dressed = _player_dressed(roster, player_id)

        game_row_count = int(mask.sum())
        wrote_values = False

        for periods, out_map in SCOPE_OUTPUT_COLS:
            stats = aggregate_player_period_stats(
                pbp, roster, player_id=player_id,
                periods=periods, played_this_game=dressed,
            )

            if stats is None:
                for out_col in out_map.values():
                    df.loc[mask, out_col] = float("nan")
                continue

            for base, out_col in out_map.items():
                df.loc[mask, out_col] = stats.get(base, float("nan"))
                wrote_values = True

        if wrote_values and game_row_count:
            games_written += game_row_count

    logger.info(
        "partial_game enrich done player=%s games=%s pbp_hit=%s pbp_miss=%s "
        "roster_hit=%s roster_miss=%s game_rows_touched=%s",
        player_id, len(game_ids), pbp_hits, pbp_misses,
        roster_hits, roster_misses, games_written,
    )

    # Optional QA: compare ONE random game slice against boxed period totals.
    _maybe_validate_sample(df, player_id, game_id_col)

    # Guard: duplicate game IDs are rare — log if the mask hits >1 row
    dup_games = df[game_id_col].astype(str).value_counts()
    if (dup_games > 1).any():
        bad = dup_games[dup_games > 1].head(5).index.tolist()
        logger.warning(
            "partial_game enrich: duplicated Game_ID rows detected sample=%s", bad)

    return df


def _maybe_validate_sample(df: pd.DataFrame, player_id: int, game_id_col: str) -> None:
    if os.environ.get("PARTIAL_GAME_VALIDATE", "").lower() not in ("1", "true", "yes"):
        return
    # Single-row sanity check (expensive — dev only)
    try:
        gid = _game_id_zfill(df.iloc[0][game_id_col])
        derived = aggregate_player_period_stats(
            fetch_playbyplay_v3_dataframe(gid),
            fetch_boxscore_roster_dataframe(gid),
            player_id=player_id,
            periods=SCOPE_1Q_PERIODS,
            played_this_game=_player_dressed(
                fetch_boxscore_roster_dataframe(gid), player_id),
        )
        boxed = _box_period_stats_for_comparison(gid, player_id, start_p=1, end_p=1)
        logger.warning(
            "PARTIAL_GAME_VALIDATE derived=%s boxed=%s (game=%s pid=%s)",
            derived, boxed, gid, player_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("PARTIAL_GAME_VALIDATE failed: %s", exc)


def _box_period_stats_for_comparison(
    game_id_z: str, player_id: int, *, start_p: int, end_p: int,
) -> dict[str, Any] | None:
    """Debug-only boxed period totals (may fail on old games)."""
    from nba_api.stats.endpoints import boxscoretraditionalv3

    try:
        ep = call_endpoint(
            boxscoretraditionalv3.BoxScoreTraditionalV3,
            game_id=game_id_z,
            start_period=str(start_p),
            end_period=str(end_p),
        )
        pdf = ep.player_stats.get_data_frame()
    except NBAApiError:
        return None
    if pdf is None or pdf.empty:
        return None
    row = pdf[pdf["personId"] == int(player_id)]
    if row.empty:
        return None
    r = row.iloc[0]
    return {
        "PTS": r.get("points"),
        "REB": r.get("reboundsTotal"),
        "AST": r.get("assists"),
        "FG3M": r.get("threePointersMade"),
    }
