"""Match-level research for the CS2 page.

HLTV endpoints used:
  * getResults  — recent completed matches (1 request, 6-h cache)
  * getMatches  — upcoming / live matches (1 request, 15-min cache)
  * getMatch    — full match detail with per-map results (1 request, 30-day cache)
"""

from __future__ import annotations

import datetime
import logging
import math
from dataclasses import dataclass, field

from services.cs2 import cs2_data_provider as provider
from services.cs2.models import CS2MatchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TeamForm — used by the projection engine
# ---------------------------------------------------------------------------


@dataclass
class TeamForm:
    """Aggregated recent form for a team, built from match results.

    Field names (maps_won / maps_lost, team_id) preserved for backward
    compatibility with the projection engine and existing smoke tests.
    """
    team_name: str = ""
    team_id: int | None = None        # compat: old tests pass team_id
    wins: int = 0
    losses: int = 0
    maps_won: int = 0                 # alias: was map_wins in draft
    maps_lost: int = 0                # alias: was map_losses in draft

    @property
    def win_pct(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else float("nan")

    @property
    def map_win_pct(self) -> float:
        total = self.maps_won + self.maps_lost
        return self.maps_won / total if total > 0 else float("nan")

    # Projection engine also reads these names on some paths.
    @property
    def map_wins(self) -> int:
        return self.maps_won

    @property
    def map_losses(self) -> int:
        return self.maps_lost

    @property
    def games(self) -> int:
        """Total games (matches) played. Used by legacy smoke tests."""
        return self.wins + self.losses


def build_team_form(results: list[CS2MatchResult], team_name: str,
                    n: int = 10) -> TeamForm:
    """Compute a TeamForm from recent CS2MatchResult objects."""
    form = TeamForm(team_name=team_name)
    for r in results[:n]:
        if r.score_t1 is None or r.score_t2 is None:
            continue
        t1_name = (r.team1.name or "") if r.team1 else ""
        t1_is_ours = team_name.lower() in t1_name.lower()
        if t1_is_ours:
            if r.score_t1 > r.score_t2:
                form.wins += 1
            else:
                form.losses += 1
            form.maps_won += r.score_t1
            form.maps_lost += r.score_t2
        else:
            if r.score_t2 > r.score_t1:
                form.wins += 1
            else:
                form.losses += 1
            form.maps_won += r.score_t2
            form.maps_lost += r.score_t1
    return form


# ---------------------------------------------------------------------------
# Backward-compat helpers (used by legacy smoke tests)
# ---------------------------------------------------------------------------


def compute_team_form(matches: list[dict], *,
                      team_id: int | None = None,
                      team_name: str | None = None) -> TeamForm:
    """Legacy adapter.  Accepts raw dicts (old PandaScore shape).

    Works by looking at ``winner_id`` / ``results`` keys.
    """
    form = TeamForm(team_id=team_id, team_name=team_name or "")
    tid_set = {team_id} if team_id else set()

    for m in matches:
        status = m.get("status", "")
        if status not in ("finished", "post_game"):
            continue
        winner = m.get("winner_id")
        results = m.get("results") or []

        # Determine if this team won.
        t_score = o_score = None
        for r in results:
            if isinstance(r, dict):
                tid = r.get("team_id")
                sc = r.get("score")
                if team_id and tid == team_id:
                    t_score = sc
                elif team_id and tid != team_id:
                    o_score = sc

        if t_score is not None and o_score is not None:
            if winner == team_id:
                form.wins += 1
            else:
                form.losses += 1
            form.maps_won  += int(t_score)
            form.maps_lost += int(o_score)

    return form


def head_to_head(matches: list[dict], opponent_id: int) -> list[dict]:
    """Legacy: return matches involving opponent_id."""
    out = []
    for m in matches:
        opps = m.get("opponents") or []
        ids = {o.get("id") for o in opps if isinstance(o, dict)}
        if opponent_id in ids:
            out.append(m)
    return out


def map_pool_summary(matches: list[dict], *,
                     team_id: int | None = None) -> dict[str, dict]:
    """Legacy: aggregate map wins/losses from raw match dicts."""
    pool: dict[str, dict] = {}
    for m in matches:
        for g in m.get("games") or []:
            if not isinstance(g, dict) or not g.get("finished"):
                continue
            map_name = g.get("map")
            if not map_name:
                continue
            from services.cs2.constants import normalize_map_name
            map_name = normalize_map_name(str(map_name))
            if map_name not in pool:
                pool[map_name] = {"games": 0, "wins": 0, "losses": 0, "win_pct": 0.0}
            pool[map_name]["games"] += 1
            winner = g.get("winner_id")
            if team_id and winner == team_id:
                pool[map_name]["wins"] += 1
            else:
                pool[map_name]["losses"] += 1

    for v in pool.values():
        total = v["games"]
        v["win_pct"] = v["wins"] / total if total > 0 else 0.0

    return pool

def get_recent_results(limit: int = 20, stars: int = 0) -> list[CS2MatchResult]:
    """Recent completed matches from HLTV results page."""
    try:
        return provider.recent_results(stars=stars, limit=limit)
    except Exception as exc:
        logger.warning("get_recent_results failed: %s", exc)
        return []


def get_upcoming() -> list[CS2MatchResult]:
    """Upcoming and live matches from HLTV matches page."""
    try:
        return provider.upcoming_matches()
    except Exception as exc:
        logger.warning("get_upcoming failed: %s", exc)
        return []


def get_match_detail(match_id: int) -> CS2MatchResult | None:
    """Full match detail including per-map scores."""
    try:
        return provider.match_detail(match_id)
    except Exception as exc:
        logger.warning("get_match_detail failed for id=%s: %s", match_id, exc)
        return None


def format_match_label(r: CS2MatchResult) -> str:
    """Return a short human-readable label for a match result."""
    t1 = (r.team1.name if r.team1 else None) or "TBD"
    t2 = (r.team2.name if r.team2 else None) or "TBD"
    s1 = r.score_t1
    s2 = r.score_t2
    fmt = r.format or ""
    if s1 is not None and s2 is not None:
        return f"{t1} {s1}–{s2} {t2}  ({fmt})"
    return f"{t1} vs {t2}  ({fmt})"


def format_match_date(r: CS2MatchResult) -> str:
    """Return a short date/time string from a match's unix-ms timestamp."""
    if r.date_unix_ms is None:
        return "—"
    try:
        dt = datetime.datetime.utcfromtimestamp(r.date_unix_ms / 1000)
        return dt.strftime("%b %d, %Y")
    except Exception:
        return "—"


def h2h_summary(results: list[CS2MatchResult],
                team1_name: str, team2_name: str) -> dict:
    """Compute head-to-head stats between two teams from a results list."""
    t1_wins = t2_wins = total = 0
    for r in results:
        n1 = (r.team1.name or "").lower()
        n2 = (r.team2.name or "").lower()
        tgt1 = team1_name.lower()
        tgt2 = team2_name.lower()

        is_h2h = (
            (tgt1 in n1 and tgt2 in n2) or (tgt1 in n2 and tgt2 in n1)
        )
        if not is_h2h:
            continue
        total += 1
        if r.score_t1 is None or r.score_t2 is None:
            continue
        if tgt1 in n1:
            if r.score_t1 > r.score_t2:
                t1_wins += 1
            else:
                t2_wins += 1
        else:
            if r.score_t2 > r.score_t1:
                t1_wins += 1
            else:
                t2_wins += 1

    return {
        "team1_name":  team1_name,
        "team2_name":  team2_name,
        "team1_wins":  t1_wins,
        "team2_wins":  t2_wins,
        "total":       total,
        "team1_rate":  t1_wins / total if total > 0 else None,
    }
