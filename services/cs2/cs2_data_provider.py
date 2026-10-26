"""Unified CS2 data facade.

Provider priority (resolved at runtime each call):
    1. HLTV (gigobyte/hltv Node.js bridge) — primary data source.
       Scraping-based; protected by the cache/throttle/circuit-breaker
       stack in ``hltv_provider.py``.
    2. Mock — fallback when HLTV is unavailable or
       ``CS2_USE_MOCK_FALLBACK=true``.
    3. GRID Open Access — experimental future placeholder; only consulted
       when ``CS2_PROVIDER=grid`` is set explicitly.  Preserved so the
       project can flip back to GRID later without code churn.

Downstream services and the UI never import grid_provider, hltv_provider,
or mock_provider directly — all data flows through this facade.

Stale data handling:
    Payloads returned from a disk-cache fallback carry ``_stale=True`` on
    the raw dict or list items.  Normalised dataclasses propagate this via
    the ``._stale`` field.  The UI surfaces ``cs2_stale_banner()``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from services.cs2 import hltv_provider as _hltv
from services.cs2 import mock_provider as _mock
from services.cs2 import grid_provider as _grid
from services.cs2.constants import normalize_map_name
from services.cs2.models import (
    CS2MapResult,
    CS2MatchResult,
    CS2Player,
    CS2PlayerMapEntry,
    CS2PlayerStats,
    CS2SourceStatus,
    CS2Team,
    CS2TeamMapStat,
    CS2TeamStats,
    CS2Tournament,
)

logger = logging.getLogger(__name__)

_USE_MOCK: bool = os.environ.get("CS2_USE_MOCK_FALLBACK", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Provider selection helpers
# ---------------------------------------------------------------------------


def _active_provider() -> str:
    """Resolve which provider is active this call.

    HLTV is primary by default.  GRID is consulted only when
    ``CS2_PROVIDER=grid`` is set explicitly AND the key is usable
    (i.e. has Central Data permission); otherwise the app transparently
    falls back to HLTV, then to mock.

    Returns one of: "hltv", "mock", "grid".
    """
    if _USE_MOCK:
        return "mock"

    requested = os.environ.get("CS2_PROVIDER", "hltv").lower()

    # Experimental GRID path — only taken when explicitly opted-in.
    if requested == "grid":
        if _grid.is_usable():
            return "grid"
        if _grid.is_configured() and _grid.graphql_denied():
            logger.info(
                "GRID key lacks Central Data GraphQL permission; "
                "falling back to HLTV for this session"
            )
        else:
            logger.debug(
                "CS2_PROVIDER=grid but GRID_API_KEY not set; falling back to HLTV"
            )

    # Default / fallback path: HLTV, then mock.
    if _hltv.bridge_installed():
        return "hltv"
    logger.debug("HLTV bridge not installed; falling back to mock")

    return "mock"


def _use_mock() -> bool:
    return _active_provider() == "mock"


def _use_hltv_enrichment() -> bool:
    """True when HLTV enrichment is enabled and the bridge is ready."""
    try:
        from config import CS2_ENABLE_HLTV_ENRICHMENT  # noqa: PLC0415
        if not CS2_ENABLE_HLTV_ENRICHMENT:
            return False
    except Exception:
        if os.environ.get("CS2_ENABLE_HLTV_ENRICHMENT", "false").lower() != "true":
            return False
    return _hltv.bridge_installed()


# ---------------------------------------------------------------------------
# Normalisation helpers (HLTV JSON → internal models)
# ---------------------------------------------------------------------------


def _norm_team_from_hltv(raw: dict | None) -> CS2Team | None:
    """Normalise HLTV team dict (can come from result, match, or getTeam)."""
    if not raw:
        return None
    t = CS2Team(
        id=raw.get("id"),
        name=raw.get("name"),
        logo_url=raw.get("logo") or raw.get("image_url"),
        country=_country_str(raw.get("country")),
        ranking=raw.get("ranking"),
        _stale=bool(raw.get("_stale", False)),
        _source=raw.get("_source", "hltv"),
    )
    for p in raw.get("players") or []:
        if isinstance(p, dict):
            t.players.append(CS2Player(
                id=p.get("id"),
                ign=p.get("ign") or p.get("name"),
                name=p.get("name"),
                _source=raw.get("_source", "hltv"),
            ))
    return t


def _country_str(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("name") or raw.get("code")
    return None


def _norm_match_result(raw: dict | None) -> CS2MatchResult | None:
    """Normalise a result entry from getResults()."""
    if not raw:
        return None
    res = raw.get("result") or {}
    stale = bool(raw.get("_stale", False))
    src = raw.get("_source", "hltv")

    # Team fields differ between getResults (no id) and getMatches (has id).
    t1_raw = raw.get("team1") or {}
    t2_raw = raw.get("team2") or {}

    return CS2MatchResult(
        id=raw.get("id"),
        date_unix_ms=raw.get("date"),
        team1=CS2Team(id=t1_raw.get("id"), name=t1_raw.get("name"),
                      logo_url=t1_raw.get("logo"), _source=src),
        team2=CS2Team(id=t2_raw.get("id"), name=t2_raw.get("name"),
                      logo_url=t2_raw.get("logo"), _source=src),
        score_t1=res.get("team1"),
        score_t2=res.get("team2"),
        format=raw.get("format"),
        event_name=(raw.get("event") or {}).get("name") if isinstance(raw.get("event"), dict)
                    else raw.get("event"),
        event_id=(raw.get("event") or {}).get("id") if isinstance(raw.get("event"), dict) else None,
        stars=int(raw.get("stars", 0)),
        is_live=bool(raw.get("live", False)),
        _stale=stale,
        _source=src,
    )


def _norm_match_detail(raw: dict | None) -> CS2MatchResult | None:
    """Normalise a detailed match from getMatch() — includes per-map results."""
    if not raw:
        return None
    m = _norm_match_result(raw)
    if not m:
        return None

    # Per-map breakdown in getMatch response.
    for g in raw.get("maps") or raw.get("games") or []:
        if not isinstance(g, dict):
            continue
        map_res = CS2MapResult(
            map_name=normalize_map_name(g.get("mapName") or g.get("map")),
            score_t1=(g.get("result") or {}).get("team1") or g.get("score_t1"),
            score_t2=(g.get("result") or {}).get("team2") or g.get("score_t2"),
            stats_id=g.get("statsId") or g.get("stats_id"),
        )
        m.maps.append(map_res)

    return m


def _norm_player_stats(raw: dict | None, player_id: int) -> CS2PlayerStats | None:
    """Normalise getPlayerStats() response."""
    if not raw:
        return None
    ov = raw.get("overviewStatistics") or {}
    ind = raw.get("individualStatistics") or {}
    stale = bool(raw.get("_stale", False))
    src = raw.get("_source", "hltv")

    recent: list[CS2PlayerMapEntry] = []
    for m in raw.get("matches") or []:
        if not isinstance(m, dict):
            continue
        recent.append(CS2PlayerMapEntry(
            date_unix_ms=m.get("date"),
            map_name=normalize_map_name(str(m.get("map", ""))),
            team1_name=(m.get("team1") or {}).get("name"),
            team2_name=(m.get("team2") or {}).get("name"),
            kills=m.get("kills"),
            deaths=m.get("deaths"),
            rating=m.get("rating"),
            map_stats_id=m.get("mapStatsId"),
        ))

    rating = ov.get("rating2") or ov.get("rating1")

    return CS2PlayerStats(
        player_id=player_id,
        ign=raw.get("ign"),
        total_kills=ov.get("kills"),
        total_deaths=ov.get("deaths"),
        maps_played=ov.get("mapsPlayed"),
        rounds_played=ov.get("roundsPlayed"),
        kd_ratio=ov.get("kdRatio"),
        kills_per_round=ov.get("killsPerRound"),
        deaths_per_round=ov.get("deathsPerRound"),
        hs_pct=ov.get("headshots"),    # HLTV returns headshot % as 0-100
        adr=ov.get("damagePerRound"),
        rating=rating,
        opening_kills=ind.get("openingKills"),
        opening_deaths=ind.get("openingDeaths"),
        opening_kill_ratio=ind.get("openingKillRatio"),
        recent_maps=recent,
        _stale=stale,
        _source=src,
    )


def _norm_team_stats(raw: dict | None, team_id: int) -> CS2TeamStats | None:
    """Normalise getTeamStats() response."""
    if not raw:
        return None
    ov = raw.get("overview") or {}
    stale = bool(raw.get("_stale", False))
    src = raw.get("_source", "hltv")

    map_stats: list[CS2TeamMapStat] = []
    for ms in raw.get("maps") or []:
        if not isinstance(ms, dict):
            continue
        stat = CS2TeamMapStat(
            map_name=normalize_map_name(str(ms.get("map", ""))),
            wins=int(ms.get("wins", 0)),
            losses=int(ms.get("losses", 0)),
        )
        stat.win_rate = ms.get("winRate") or None
        if stat.win_rate is None:
            stat.compute_win_rate()
        map_stats.append(stat)

    wins = ov.get("wins")
    losses = ov.get("losses")
    total = (wins or 0) + (losses or 0)
    win_rate = ov.get("winRate") or (wins / total if total > 0 else None)

    return CS2TeamStats(
        team_id=team_id,
        team_name=raw.get("name"),
        maps_played=ov.get("mapsPlayed"),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        kills_per_round=ov.get("killsPerRound"),
        deaths_per_round=ov.get("deathsPerRound"),
        map_stats=map_stats,
        _stale=stale,
        _source=src,
    )


# ---------------------------------------------------------------------------
# GRID normalisation helpers
# ---------------------------------------------------------------------------


def _norm_match_from_grid(node: dict | None) -> CS2MatchResult | None:
    """Normalise a GRID Central Data series node into CS2MatchResult."""
    if not node:
        return None
    stale = bool(node.get("_stale", False))

    # Parse scheduled start time (ISO-8601 → epoch ms)
    date_ms: int | None = None
    ts = node.get("startTimeActual") or node.get("startTimeScheduled")
    if ts:
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            date_ms = int(dt.timestamp() * 1000)
        except Exception:
            pass

    # Teams — GRID uses teams[].baseInfo for identity and teams[].score / .won
    teams_raw: list[dict] = node.get("teams") or []
    t1_raw = teams_raw[0] if len(teams_raw) > 0 else {}
    t2_raw = teams_raw[1] if len(teams_raw) > 1 else {}

    def _grid_team(tr: dict) -> CS2Team:
        bi = tr.get("baseInfo") or {}
        return CS2Team(
            id=bi.get("id"),
            name=bi.get("name"),
            logo_url=bi.get("logoUrl"),
            _source="grid",
        )

    t1 = _grid_team(t1_raw) if t1_raw else CS2Team(_source="grid")
    t2 = _grid_team(t2_raw) if t2_raw else CS2Team(_source="grid")

    s1 = t1_raw.get("score")
    s2 = t2_raw.get("score")

    # Series format: GRID returns format.nameShortened (e.g. "Bo3")
    fmt_node = node.get("format") or {}
    fmt = (fmt_node.get("nameShortened") or "").lower() or None

    tournament_node = node.get("tournament") or {}

    return CS2MatchResult(
        id=node.get("id"),
        date_unix_ms=date_ms,
        team1=t1,
        team2=t2,
        score_t1=int(s1) if s1 is not None else None,
        score_t2=int(s2) if s2 is not None else None,
        format=fmt,
        event_name=tournament_node.get("name"),
        event_id=tournament_node.get("id"),
        is_live=False,   # GRID allSeries doesn't flag live separately
        _stale=stale,
        _source="grid",
    )


# NOTE: GRID end-state normalisers are deferred until we audit actual
# response shapes from the API.  See grid_provider.audit_series() to
# inspect real responses before writing format-specific parsers.


def _norm_team_from_grid(raw: dict | None) -> CS2Team | None:
    """Normalise a GRID Central Data team node."""
    if not raw:
        return None
    nationality_node = raw.get("nationality") or {}
    country = nationality_node.get("name") or nationality_node.get("code")

    team = CS2Team(
        id=raw.get("id"),
        name=raw.get("name"),
        logo_url=raw.get("logoUrl"),
        country=country,
        _stale=bool(raw.get("_stale", False)),
        _source="grid",
    )
    for p in raw.get("roster") or []:
        if not isinstance(p, dict):
            continue
        nat = (p.get("nationality") or {}).get("name")
        team.players.append(CS2Player(
            id=p.get("id"),
            ign=p.get("nickname"),
            name=f"{p.get('firstName', '') or ''} {p.get('lastName', '') or ''}".strip() or None,
            nationality=nat,
            role=p.get("role"),
            _source="grid",
        ))
    return team


def _norm_player_from_grid(raw: dict | None) -> CS2Player | None:
    """Normalise a GRID Central Data player node."""
    if not raw:
        return None
    nat_node = raw.get("nationality") or {}
    team_node = raw.get("activeTeam") or {}
    full_name = (
        f"{raw.get('firstName', '') or ''} {raw.get('lastName', '') or ''}".strip() or None
    )
    return CS2Player(
        id=raw.get("id"),
        ign=raw.get("nickname"),
        name=full_name,
        nationality=(nat_node.get("name") or nat_node.get("code")),
        image_url=raw.get("photoUrl"),
        team=CS2Team(id=team_node.get("id"), name=team_node.get("name"))
              if team_node else None,
        _stale=bool(raw.get("_stale", False)),
        _source="grid",
    )


def _norm_tournament_from_grid(raw: dict | None) -> CS2Tournament | None:
    """Normalise a GRID Central Data tournament node."""
    if not raw:
        return None
    loc = raw.get("location") or {}
    country_node = loc.get("country") or {}
    return CS2Tournament(
        id=raw.get("id"),
        name=raw.get("name"),
        start_date=raw.get("startDate"),
        end_date=raw.get("endDate"),
        prize_pool=str(raw.get("prizePool")) if raw.get("prizePool") is not None else None,
        city=loc.get("city"),
        country=country_node.get("name"),
        _stale=bool(raw.get("_stale", False)),
        _source="grid",
    )


# ---------------------------------------------------------------------------
# Public facade — matches / results
# ---------------------------------------------------------------------------


def upcoming_matches() -> list[CS2MatchResult]:
    """Upcoming and live matches (cached 15 min)."""
    provider = _active_provider()
    if provider == "mock":
        raw = _mock.get_matches()
        return [m for r in raw if (m := _norm_match_result(r)) is not None]
    if provider == "grid":
        import datetime
        now = datetime.datetime.utcnow()
        future = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            raw = _grid.get_series_list(
                limit=30,
                start_date=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_date=future,
            )
            return [m for r in raw if (m := _norm_match_from_grid(r)) is not None]
        except _grid.GRIDPermissionDenied:
            # Key lacks Central Data GraphQL; transparently fall through to HLTV.
            logger.info("GRID PERMISSION_DENIED on upcoming_matches — using HLTV fallback")
            # fall through
    # hltv (primary or fallback)
    if _hltv.bridge_installed():
        raw = _hltv.get_matches()
        return [m for r in raw if (m := _norm_match_result(r)) is not None]
    return []


def recent_results(stars: int = 0, limit: int = 30,
                   team_ids: list[int] | None = None) -> list[CS2MatchResult]:
    """Recent completed results (cached 6 h)."""
    provider = _active_provider()
    if provider == "mock":
        raw = _mock.get_results(stars=stars, limit=limit, team_ids=team_ids)
        return [m for r in raw if (m := _norm_match_result(r)) is not None]
    if provider == "grid":
        import datetime
        end = datetime.datetime.utcnow()
        start = (end - datetime.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            raw = _grid.get_series_list(
                limit=limit,
                start_date=start,
                end_date=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            return [m for r in raw if (m := _norm_match_from_grid(r)) is not None]
        except _grid.GRIDPermissionDenied:
            logger.info("GRID PERMISSION_DENIED on recent_results — using HLTV fallback")
            # fall through
    # hltv (primary or fallback)
    if _hltv.bridge_installed():
        raw = _hltv.get_results(stars=stars, limit=limit, team_ids=team_ids)
        return [m for r in raw if (m := _norm_match_result(r)) is not None]
    return []


def match_detail(match_id: Any) -> CS2MatchResult | None:
    """Full match detail including per-map results (30-day cache).

    GRID path: returns basic series metadata only (no end-state enrichment
    yet — pending format audit of actual GRID end-state files).
    """
    provider = _active_provider()
    if provider == "mock":
        return None
    if provider == "grid":
        try:
            series_nodes = _grid.get_series_list(limit=50)
            node = next(
                (n for n in series_nodes if str(n.get("id")) == str(match_id)), None
            )
            if node:
                return _norm_match_from_grid(node)
        except _grid.GRIDPermissionDenied:
            logger.info("GRID PERMISSION_DENIED on match_detail — using HLTV fallback")
            # fall through
    # hltv (primary or fallback)
    if _hltv.bridge_installed():
        raw = _hltv.get_match(match_id)
        return _norm_match_detail(raw)
    return None


def tournaments(limit: int = 30) -> list[CS2Tournament]:
    """Recent CS2 tournaments (GRID only, cached 24 h).

    The allTournaments query shape is pending audit of real GRID responses.
    Returns raw nodes normalised defensively; missing fields show as None.
    """
    provider = _active_provider()
    if provider != "grid":
        return []
    try:
        raw = _grid.get_tournaments(limit=limit)
        return [t for r in raw if (t := _norm_tournament_from_grid(r)) is not None]
    except Exception as exc:
        logger.warning("GRID tournaments query failed (query may need schema audit): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public facade — players
# ---------------------------------------------------------------------------


def search_player(name: str) -> CS2Player | None:
    """Look up a player profile by name / IGN."""
    provider = _active_provider()
    if provider == "mock":
        p = _mock.get_player_stats(0)
        return CS2Player(id=p.get("id"), ign=p.get("ign"), name=p.get("name"),
                         _source="mock")
    if provider == "grid":
        # GRID Central Data does not have a "search by name" endpoint.
        # We try HLTV enrichment first (if available) since it has free-text search.
        if _use_hltv_enrichment():
            try:
                raw = _hltv.get_player_by_name(name)
                if raw:
                    return _norm_player_from_hltv_search(raw)
            except Exception as exc:
                logger.debug("HLTV player search failed: %s", exc)
        # No match — return None (UI will ask user to use GRID player ID).
        return None
    # hltv primary
    raw = _hltv.get_player_by_name(name)
    if not raw:
        return None
    return _norm_player_from_hltv_search(raw)


def _norm_player_from_hltv_search(raw: dict) -> CS2Player:
    team_raw = raw.get("team") or {}
    return CS2Player(
        id=raw.get("id"),
        ign=raw.get("ign"),
        name=raw.get("name"),
        nationality=_country_str(raw.get("country")),
        age=raw.get("age"),
        team=CS2Team(id=team_raw.get("id"), name=team_raw.get("name"))
              if team_raw else None,
        image_url=raw.get("image"),
        _stale=bool(raw.get("_stale", False)),
        _source="hltv",
    )


def player_by_grid_id(player_id: str | int) -> CS2Player | None:
    """Fetch a player profile by their GRID player ID."""
    provider = _active_provider()
    if provider == "mock":
        return None
    if provider == "grid":
        raw = _grid.get_player(player_id)
        return _norm_player_from_grid(raw) if raw else None
    return None


def player_stats(player_id: Any) -> CS2PlayerStats | None:
    """Full player stats. Call lazily on user request.

    GRID path: per-player stats come from end-state files whose format is
    not yet audited.  When GRID is primary, this falls through to HLTV
    enrichment (if enabled) or returns None.
    HLTV path: uses getPlayerStats (24-h cache).
    """
    provider = _active_provider()
    if provider == "mock":
        raw = _mock.get_player_stats(player_id)
        return _norm_player_stats(raw, player_id)

    if provider == "grid":
        # Player stats from GRID end-state files: deferred until format audited.
        # Use HLTV enrichment path if available.
        if _use_hltv_enrichment():
            try:
                raw = _hltv.get_player_stats(int(player_id))
                return _norm_player_stats(raw, player_id)
            except Exception as exc:
                logger.debug("HLTV player stats (enrichment) failed: %s", exc)
        # No player stats available yet from GRID.
        return None

    # hltv primary
    raw = _hltv.get_player_stats(player_id)
    return _norm_player_stats(raw, player_id)


# ---------------------------------------------------------------------------
# Public facade — teams
# ---------------------------------------------------------------------------


def search_team(name: str) -> CS2Team | None:
    """Look up a team profile by name."""
    provider = _active_provider()
    if provider == "mock":
        return _norm_team_from_hltv(_mock.get_team(0))
    if provider == "grid":
        # GRID doesn't have free-text team search; use HLTV enrichment if available.
        if _use_hltv_enrichment():
            try:
                raw = _hltv.get_team_by_name(name)
                if raw:
                    team = _norm_team_from_hltv(raw)
                    if team:
                        team._source = "hltv"
                    return team
            except Exception as exc:
                logger.debug("HLTV team search failed: %s", exc)
        return None
    raw = _hltv.get_team_by_name(name)
    return _norm_team_from_hltv(raw) if raw else None


def team_by_grid_id(team_id: str | int) -> CS2Team | None:
    """Fetch a team profile by their GRID team ID."""
    provider = _active_provider()
    if provider in ("mock", "hltv"):
        return None
    raw = _grid.get_team(team_id)
    return _norm_team_from_grid(raw) if raw else None


def team_detail(team_id: int) -> CS2Team | None:
    """Team roster and profile (HLTV ID; 24-h cache)."""
    provider = _active_provider()
    if provider == "mock":
        return _norm_team_from_hltv(_mock.get_team(team_id))
    if provider == "grid" and _use_hltv_enrichment():
        raw = _hltv.get_team(team_id)
        return _norm_team_from_hltv(raw) if raw else None
    if provider == "hltv":
        raw = _hltv.get_team(team_id)
        return _norm_team_from_hltv(raw) if raw else None
    return None


def team_stats(team_id: int) -> CS2TeamStats | None:
    """Aggregate team stats (lazy).

    GRID Open Access does not expose per-map win rates; falls back to HLTV
    enrichment when enabled, otherwise returns None.
    """
    provider = _active_provider()
    if provider == "mock":
        raw = _mock.get_team_stats(team_id)
        return _norm_team_stats(raw, team_id)
    if provider == "grid" and _use_hltv_enrichment():
        raw = _hltv.get_team_stats(team_id)
        return _norm_team_stats(raw, team_id)
    if provider == "hltv":
        raw = _hltv.get_team_stats(team_id)
        return _norm_team_stats(raw, team_id)
    # GRID without HLTV enrichment cannot supply team stats.
    return None


def team_results(team_id: int, limit: int = 20) -> list[CS2MatchResult]:
    """Recent results for a specific team."""
    provider = _active_provider()
    if provider == "grid":
        # GRID doesn't filter by team ID in allSeries (needs separate query).
        # Return from full recent list and filter client-side.
        all_results = recent_results(limit=100)
        filtered = [
            r for r in all_results
            if (r.team1 and r.team1.id is not None and str(r.team1.id) == str(team_id))
            or (r.team2 and r.team2.id is not None and str(r.team2.id) == str(team_id))
        ]
        return filtered[:limit]
    return recent_results(limit=limit, team_ids=[team_id])


def team_ranking(top_n: int = 30) -> list[dict]:
    """Current world team ranking.

    Only available from HLTV enrichment; not exposed by GRID Open Access.
    """
    if _use_mock():
        return _mock.get_team_ranking()[:top_n]
    if _use_hltv_enrichment() or _active_provider() == "hltv":
        raw = _hltv.get_team_ranking()
        return (raw or [])[:top_n]
    return []  # Not available from GRID Open Access


# ---------------------------------------------------------------------------
# Provider status
# ---------------------------------------------------------------------------


def source_status() -> CS2SourceStatus:
    """Current state of the CS2 provider stack for the UI status panel."""
    from services.cs2 import cs2_cache_service as _cache_svc  # noqa: PLC0415
    provider = _active_provider()
    cache_dir = _cache_svc.cache_stats().get("cache_dir", "")
    enrichment = _use_hltv_enrichment()
    hltv_bridge = _hltv.bridge_installed()

    if provider == "mock":
        return CS2SourceStatus(
            provider="mock",
            mode="offline",
            grid_configured=_grid.is_configured(),
            hltv_bridge_installed=hltv_bridge,
            hltv_enabled=enrichment,
            cache_dir=cache_dir,
        )

    if provider == "grid":
        grid_st = _grid.get_status()
        cs = grid_st.get("cache_stats", {})
        hltv_st = _hltv.get_status() if enrichment else {}
        return CS2SourceStatus(
            provider="grid",
            mode=grid_st.get("mode", "unknown"),
            grid_configured=True,
            grid_last_success_ts=grid_st.get("last_success_ts"),
            last_success_ts=grid_st.get("last_success_ts"),
            hltv_enabled=enrichment,
            hltv_bridge_installed=hltv_bridge,
            hltv_cb_open=bool(hltv_st.get("cb_open", False)),
            hltv_cb_open_until=hltv_st.get("cb_open_until"),
            hltv_last_success_ts=hltv_st.get("last_success_ts"),
            throttle_s=grid_st.get("throttle_s", 1.0),
            cache_dir=cs.get("cache_dir", cache_dir),
        )

    # hltv primary (or GRID fell back to HLTV this session)
    hltv_st = _hltv.get_status()
    cs = hltv_st.get("cache_stats", {})
    return CS2SourceStatus(
        provider="hltv",
        mode=hltv_st.get("mode", "unknown"),
        grid_configured=_grid.is_configured(),
        grid_permission_denied=_grid.graphql_denied(),
        hltv_enabled=True,
        hltv_bridge_installed=hltv_bridge,
        hltv_cb_open=bool(hltv_st.get("cb_open", False)),
        hltv_cb_open_until=hltv_st.get("cb_open_until"),
        hltv_last_success_ts=hltv_st.get("last_success_ts"),
        last_success_ts=hltv_st.get("last_success_ts"),
        throttle_s=hltv_st.get("throttle_s", 3.0),
        cache_dir=cs.get("cache_dir", cache_dir),
    )


def reset_provider() -> None:
    """Reset circuit breakers and clear in-memory state (sidebar Refresh button)."""
    try:
        _hltv.reset_circuit_breaker()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backward-compat shims (retained for smoke-test suite)
# ---------------------------------------------------------------------------
# The old PandaScore-era normalizers are kept here so tests that
# import them directly from cs2_data_provider don't break.


def _opponents(match: dict) -> list[dict]:
    out = []
    for entry in match.get("opponents") or []:
        team = entry.get("opponent") if isinstance(entry, dict) else None
        if isinstance(team, dict):
            out.append(team)
    return out


def normalize_match(match: dict | None) -> dict:
    """Compat shim: normalise a PandaScore-style match dict."""
    if not match:
        return {}
    opponents = _opponents(match)
    winner_id = match.get("winner_id")
    return {
        "id":             match.get("id"),
        "status":         match.get("status"),
        "begin_at":       match.get("begin_at") or match.get("scheduled_at"),
        "end_at":         match.get("end_at"),
        "name":           match.get("name"),
        "number_of_games": match.get("number_of_games"),
        "match_type":     match.get("match_type"),
        "tournament":     (match.get("tournament") or {}).get("name"),
        "serie":          (match.get("serie") or {}).get("full_name"),
        "league":         (match.get("league") or {}).get("name"),
        "opponents": [
            {"id": t.get("id"), "name": t.get("name"),
             "acronym": t.get("acronym") or (t.get("name") or "")[:4].upper(),
             "image_url": t.get("image_url")}
            for t in opponents
        ],
        "winner_id":  winner_id,
        "results":    match.get("results") or [],
        "games":      _normalize_games(match.get("games") or []),
        "_stale":     bool(match.get("_stale", False)),
    }


def _normalize_games(games: list[dict]) -> list[dict]:
    out = []
    for g in games or []:
        if not isinstance(g, dict):
            continue
        out.append({
            "id":        g.get("id"),
            "position":  g.get("position"),
            "status":    g.get("status"),
            "map":       normalize_map_name((g.get("map") or {}).get("name")
                                             if isinstance(g.get("map"), dict)
                                             else g.get("map")),
            "winner_id": (g.get("winner") or {}).get("id")
                         if isinstance(g.get("winner"), dict)
                         else g.get("winner_id"),
            "finished":  bool(g.get("finished", g.get("status") == "finished")),
            "rounds":    g.get("rounds") or [],
            "players":   g.get("players") or [],
            "stats":     g.get("stats") or {},
            "results":   g.get("results") or [],
        })
    return out


def normalize_team(team: dict | None) -> dict:
    if not team:
        return {}
    return {
        "id":        team.get("id"),
        "name":      team.get("name"),
        "acronym":   team.get("acronym") or (team.get("name") or "")[:4].upper(),
        "image_url": team.get("image_url"),
        "location":  team.get("location"),
        "players":   [
            {"id": p.get("id"), "name": p.get("name"),
             "first_name": p.get("first_name"), "last_name": p.get("last_name"),
             "slug": p.get("slug"), "nationality": p.get("nationality"),
             "role": p.get("role")}
            for p in (team.get("players") or [])
            if isinstance(p, dict)
        ],
        "_stale": bool(team.get("_stale", False)),
    }


def normalize_player(player: dict | None) -> dict:
    if not player:
        return {}
    current_team = player.get("current_team") or {}
    return {
        "id":          player.get("id"),
        "name":        player.get("name"),
        "first_name":  player.get("first_name"),
        "last_name":   player.get("last_name"),
        "slug":        player.get("slug"),
        "nationality": player.get("nationality"),
        "role":        player.get("role"),
        "team":        {"id": current_team.get("id"),
                        "name": current_team.get("name"),
                        "acronym": current_team.get("acronym")}
                       if current_team else None,
        "_stale": bool(player.get("_stale", False)),
    }
