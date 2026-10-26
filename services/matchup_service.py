"""Matchup-level helpers built on top of ``team_service``.

Composes team game logs into pair-level context (H2H subset, each team's
recent form, the team-level advanced stats snapshot used for pace and
net-rating math). All pure-functional: every argument is explicit so the
projection service has no hidden state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from . import rankings_service, team_service
from .nba_client import NBAApiError, call_endpoint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pair context dataclass (returned by build_pair_context)
# ---------------------------------------------------------------------------


@dataclass
class PairContext:
    """Everything the projection engine needs to price the seven markets."""

    season: str
    season_type: str
    team_a: dict[str, Any]            # static info (id, abbr, full_name...)
    team_b: dict[str, Any]

    log_a: pd.DataFrame = field(default_factory=pd.DataFrame)
    log_b: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Most recent N games — pre-sliced for the projection service.
    recent_a: pd.DataFrame = field(default_factory=pd.DataFrame)
    recent_b: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Advanced league table (PACE, OFF_RATING, DEF_RATING, NET_RATING).
    # Single row per team — keyed by TEAM_ID.
    advanced: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Home-team guess (either supplied by the user or inferred from
    # upcoming schedule / last common game). Defaults to team_a.
    home_team_id: int | None = None

    # Whether either team log is serving stale cache data.
    is_stale: bool = False


# ---------------------------------------------------------------------------
# Per-team recent-form helpers
# ---------------------------------------------------------------------------


def recent_form(log: pd.DataFrame, n: int = 10) -> dict[str, float]:
    """Return a dict of averages for the most-recent `n` games.

    Keys: ``pts_for``, ``pts_against``, ``margin``, ``total``, ``games``.
    Missing columns/empty log → NaN-ish values (0 games, NaN mean).
    """
    if log is None or log.empty:
        return {"pts_for": float("nan"), "pts_against": float("nan"),
                "margin": float("nan"), "total": float("nan"), "games": 0}
    df = log.head(n)
    out = {"games": int(len(df))}
    for src, dst in (("PTS", "pts_for"), ("PTS_AGAINST", "pts_against"),
                     ("MARGIN", "margin"), ("GAME_TOTAL", "total")):
        val = pd.to_numeric(df.get(src), errors="coerce").mean() if src in df.columns else float("nan")
        out[dst] = float(val) if pd.notna(val) else float("nan")
    return out


def home_away_split(log: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Return {'home': {..}, 'away': {..}} averages.

    Each sub-dict has ``pts_for``, ``pts_against``, ``margin``, ``games``.
    Empty subsets come back with games=0 and NaN means.
    """
    out: dict[str, dict[str, float]] = {
        "home": {"games": 0, "pts_for": float("nan"),
                 "pts_against": float("nan"), "margin": float("nan")},
        "away": {"games": 0, "pts_for": float("nan"),
                 "pts_against": float("nan"), "margin": float("nan")},
    }
    if log is None or log.empty or "IS_HOME" not in log.columns:
        return out
    for tag, mask in (("home", log["IS_HOME"] == True),
                      ("away", log["IS_HOME"] == False)):
        subset = log[mask]
        out[tag]["games"] = int(len(subset))
        for src, dst in (("PTS", "pts_for"), ("PTS_AGAINST", "pts_against"),
                         ("MARGIN", "margin")):
            val = pd.to_numeric(subset.get(src), errors="coerce").mean() if src in subset.columns else float("nan")
            out[tag][dst] = float(val) if pd.notna(val) else float("nan")
    return out


# ---------------------------------------------------------------------------
# H2H helpers
# ---------------------------------------------------------------------------


def h2h_games(log_a: pd.DataFrame, team_b_abbr: str) -> pd.DataFrame:
    """Subset ``log_a`` to games played against team_b.

    Matching is by abbreviation on the parsed OPPONENT column because
    different historical teams can share an ID across relocations but
    tricode-matching within a single season is unambiguous.
    """
    if log_a is None or log_a.empty or not team_b_abbr:
        return pd.DataFrame()
    if "OPPONENT" not in log_a.columns:
        return pd.DataFrame()
    abbr = str(team_b_abbr).upper().strip()
    return log_a[log_a["OPPONENT"].fillna("").astype(str).str.upper() == abbr].copy()


def h2h_summary(log_a: pd.DataFrame, team_b_abbr: str, recent_n: int = 5
                ) -> dict[str, Any]:
    """Compact H2H summary used by the Overview tab.

    Returns: {games, recent_total_avg, recent_margin_avg, recent_rows}.
    ``recent_rows`` is a list of dicts for display (date, matchup, WL,
    PTS, PTS_AGAINST, GAME_TOTAL, MARGIN).
    """
    sub = h2h_games(log_a, team_b_abbr)
    if sub.empty:
        return {"games": 0, "recent_total_avg": float("nan"),
                "recent_margin_avg": float("nan"), "recent_rows": []}
    head = sub.head(recent_n)
    cols = [c for c in ("GAME_DATE", "MATCHUP", "WL", "PTS",
                         "PTS_AGAINST", "GAME_TOTAL", "MARGIN", "PLUS_MINUS")
            if c in head.columns]
    return {
        "games": int(len(sub)),
        "recent_total_avg": float(pd.to_numeric(head.get("GAME_TOTAL"),
                                                errors="coerce").mean())
                              if "GAME_TOTAL" in head.columns else float("nan"),
        "recent_margin_avg": float(pd.to_numeric(head.get("MARGIN"),
                                                 errors="coerce").mean())
                               if "MARGIN" in head.columns else float("nan"),
        "recent_rows": head[cols].to_dict(orient="records") if cols else [],
    }


# ---------------------------------------------------------------------------
# Upcoming matchup (live schedule lookup)
# ---------------------------------------------------------------------------


def find_upcoming_matchup(team_id: int | None,
                          game_date: str | None = None) -> dict[str, Any] | None:
    """Scan ScoreboardV2 for a game involving ``team_id`` on ``game_date``.

    ``game_date`` defaults to today's local date in YYYY-MM-DD form.
    Returns {opponent_team_id, is_home, game_date} or None when no game
    is found. Pure network — schedule_service already handles the next-
    opponent lookup for player-props; this helper is deliberately minimal
    because the Game Markets page lets the user pick teams manually too.
    """
    if not team_id:
        return None
    day = game_date or date.today().strftime("%Y-%m-%d")

    try:
        from nba_api.stats.endpoints import scoreboardv2

        ep = call_endpoint(scoreboardv2.ScoreboardV2, game_date=day)
        frames = ep.get_data_frames()
    except NBAApiError as exc:
        logger.info("find_upcoming_matchup: scoreboard failed (%s) on %s", exc, day)
        return None

    games = frames[0] if frames else pd.DataFrame()
    if games.empty:
        return None
    h_col = "HOME_TEAM_ID" if "HOME_TEAM_ID" in games.columns else None
    v_col = "VISITOR_TEAM_ID" if "VISITOR_TEAM_ID" in games.columns else None
    if not h_col or not v_col:
        return None
    row = games[(games[h_col] == team_id) | (games[v_col] == team_id)]
    if row.empty:
        return None
    g = row.iloc[0]
    is_home = int(g[h_col]) == int(team_id)
    opp_id = int(g[v_col] if is_home else g[h_col])
    return {"opponent_team_id": opp_id, "is_home": is_home, "game_date": day}


# ---------------------------------------------------------------------------
# Pair context builder (the main entry point for the projection service)
# ---------------------------------------------------------------------------


def build_pair_context(team_a_id: int, team_b_id: int,
                       season: str, season_type: str = "Regular Season",
                       *, recent_n: int = 10,
                       home_team_id: int | None = None) -> PairContext:
    """Assemble a PairContext with game logs + advanced table.

    All fetches go through the cached services, so a repeated call for
    the same (a, b, season) is near-instant after first load.
    """
    team_a = team_service.resolve_team(team_a_id) or {"id": team_a_id}
    team_b = team_service.resolve_team(team_b_id) or {"id": team_b_id}

    log_a = team_service.fetch_team_game_log(team_a_id, season, season_type)
    log_b = team_service.fetch_team_game_log(team_b_id, season, season_type)

    stale_a = bool("_stale" in log_a.columns and log_a["_stale"].any()) if not log_a.empty else False
    stale_b = bool("_stale" in log_b.columns and log_b["_stale"].any()) if not log_b.empty else False

    recent_a = log_a.head(recent_n).copy() if not log_a.empty else pd.DataFrame()
    recent_b = log_b.head(recent_n).copy() if not log_b.empty else pd.DataFrame()

    try:
        advanced = rankings_service.fetch_team_advanced(season, season_type=season_type)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("advanced table fetch failed: %s", exc)
        advanced = pd.DataFrame()

    return PairContext(
        season=season,
        season_type=season_type,
        team_a=team_a,
        team_b=team_b,
        log_a=log_a,
        log_b=log_b,
        recent_a=recent_a,
        recent_b=recent_b,
        advanced=advanced,
        home_team_id=home_team_id if home_team_id is not None else team_a_id,
        is_stale=stale_a or stale_b,
    )
