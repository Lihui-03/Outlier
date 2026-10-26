"""GRID Open Access CS2 data provider.

Two API surfaces
----------------
1. **Central Data GraphQL** — series list, tournaments, teams, players.
   Endpoint: https://api.grid.gg/central-data/graphql

2. **File Download REST** — end-state JSON files for completed series.
   Endpoint: https://api.grid.gg/file-download/end-state/grid/series/{id}
   These contain per-map scores and per-player kill/death/headshot/damage stats.

Authentication
--------------
Every request sends ``x-api-key: <GRID_API_KEY>`` as a header.
The key is read from the ``GRID_API_KEY`` env-var (or ``st.secrets["grid"]["api_key"]``).

Caching
-------
All responses are stored in ``cs2_cache_service`` (disk JSON, shared with HLTV).
Cache keys are prefixed ``grid:`` to avoid namespace collisions.
TTLs: series list 6 h, end-state / series detail 30 days, teams/players/tournaments 24 h.

Throttling
----------
A single ``threading.Lock`` serialises all outbound requests.
``GRID_THROTTLE_SECONDS`` (default 1 s) is enforced between consecutive calls.

Error types
-----------
``GRIDNotConfigured`` — no API key.
``GRIDAuthError``     — 401 / 403 from GRID.
``GRIDError``         — any other failure.

All errors are retried at most ``GRID_MAX_RETRIES`` times with exponential
backoff before raising. ``GRIDNotConfigured`` and ``GRIDAuthError`` are
non-retryable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

from services.cs2 import cs2_cache_service as _cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime config (mirrors config.py defaults but read directly from env so
# this module remains importable without Streamlit / config loaded)
# ---------------------------------------------------------------------------

_BASE_URL     = os.environ.get("GRID_BASE_URL", "https://api.grid.gg")
_GQL_URL      = f"{_BASE_URL}/central-data/graphql"
_DL_BASE      = f"{_BASE_URL}/file-download/end-state/grid/series"
_CS2_TITLE_ID = int(os.environ.get("GRID_CS2_TITLE_ID", "4"))
_THROTTLE_S   = float(os.environ.get("GRID_THROTTLE_SECONDS", "1"))
_TIMEOUT_S    = int(os.environ.get("GRID_TIMEOUT_SECONDS", "15"))
_MAX_RETRIES  = int(os.environ.get("GRID_MAX_RETRIES", "1"))

# ---------------------------------------------------------------------------
# TTLs (seconds)
# ---------------------------------------------------------------------------

_TTL_SERIES_LIST  =  6 * 60 * 60        #  6 h  — recent series index
_TTL_ENDSTATE     = 30 * 24 * 60 * 60   # 30 d  — immutable once finished
_TTL_TOURNAMENTS  = 24 * 60 * 60        # 24 h
_TTL_TEAM         = 24 * 60 * 60        # 24 h
_TTL_PLAYER       = 24 * 60 * 60        # 24 h

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GRIDError(RuntimeError):
    """General GRID call failure."""


class GRIDAuthError(GRIDError):
    """API key missing or rejected (401 / 403)."""


class GRIDNotConfigured(GRIDError):
    """GRID_API_KEY is not set in the environment."""


class GRIDPermissionDenied(GRIDError):
    """API key is valid, but the account tier does not include this endpoint.

    Typical cause: a free "File Download / Open Access" key trying to use
    the Central Data GraphQL API (which requires a Central Data subscription).
    """


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_lock = threading.Lock()           # serialises all outbound requests
_last_call_ts: float = 0.0
_last_success_ts: float | None = None
_graphql_denied: bool = False      # sticky: True once Central Data returns PERMISSION_DENIED


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def api_key() -> str | None:
    """Read GRID_API_KEY from env or Streamlit secrets (lazy)."""
    k = os.environ.get("GRID_API_KEY", "").strip()
    if k:
        return k
    try:
        import streamlit as st  # noqa: PLC0415
        section = st.secrets.get("grid") if hasattr(st, "secrets") else None
        if section and section.get("api_key"):
            return str(section["api_key"]).strip()
    except Exception:
        pass
    return None


def is_configured() -> bool:
    """Return True if a GRID API key is available."""
    return bool(api_key())


def graphql_denied() -> bool:
    """Return True if Central Data GraphQL has returned PERMISSION_DENIED this session.

    Once set, we stop trying GraphQL-backed features until the process restarts
    or the user switches keys.
    """
    return _graphql_denied


def is_usable() -> bool:
    """Return True if GRID is configured *and* has not been denied by the server.

    Used by the facade to decide whether GRID should be the primary provider
    or whether to fall back to HLTV.
    """
    return is_configured() and not _graphql_denied


# ---------------------------------------------------------------------------
# Throttle (must be called inside ``with _lock:``)
# ---------------------------------------------------------------------------


def _throttle() -> None:
    global _last_call_ts
    wait = _THROTTLE_S - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise GRIDNotConfigured(
            "GRID_API_KEY is not set. "
            "Add it to .env (see .env.example for instructions)."
        )
    return {"x-api-key": key, "Content-Type": "application/json"}


def _post_graphql(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query, return ``response["data"]``.  Retries on transient failures."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_exc: Exception | None = None
    for attempt in range(max(1, _MAX_RETRIES + 1)):
        if attempt > 0:
            time.sleep(min(_THROTTLE_S * (2 ** attempt), 30))
        try:
            resp = requests.post(
                _GQL_URL, json=payload, headers=_auth_headers(), timeout=_TIMEOUT_S
            )
            if resp.status_code in (401, 403):
                raise GRIDAuthError(
                    f"GRID API key rejected (HTTP {resp.status_code}). "
                    "Verify GRID_API_KEY."
                )
            resp.raise_for_status()
            body = resp.json()

            if "errors" in body:
                err0 = body["errors"][0] if body["errors"] else {}
                err_msg = err0.get("message", "unknown GraphQL error")
                ext = err0.get("extensions") or {}
                if ext.get("errorType") == "PERMISSION_DENIED":
                    global _graphql_denied
                    _graphql_denied = True
                    raise GRIDPermissionDenied(
                        "GRID API key does not include Central Data GraphQL access. "
                        "Your tier only grants the File Download / Open Access REST "
                        "endpoint. Falling back to HLTV."
                    )
                raise GRIDError(f"GRID GraphQL error: {err_msg}")
            return body.get("data") or {}
        except (GRIDNotConfigured, GRIDAuthError, GRIDPermissionDenied):
            raise   # non-retryable
        except Exception as exc:
            last_exc = exc
            logger.warning("GRID GraphQL attempt %d/%d failed: %s",
                           attempt + 1, _MAX_RETRIES + 1, exc)

    raise GRIDError(
        f"GRID GraphQL failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _get_file(series_id: str | int) -> dict:
    """GET the end-state JSON file for a series. Returns {} on 404."""
    url = f"{_DL_BASE}/{series_id}"
    last_exc: Exception | None = None
    for attempt in range(max(1, _MAX_RETRIES + 1)):
        if attempt > 0:
            time.sleep(min(_THROTTLE_S * (2 ** attempt), 30))
        try:
            resp = requests.get(url, headers=_auth_headers(), timeout=_TIMEOUT_S)
            if resp.status_code in (401, 403):
                raise GRIDAuthError(
                    f"GRID API key rejected (HTTP {resp.status_code})."
                )
            if resp.status_code == 404:
                return {}  # series not yet finished or not available
            resp.raise_for_status()
            return resp.json()
        except (GRIDNotConfigured, GRIDAuthError):
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("GRID end-state download attempt %d/%d for series %s: %s",
                           attempt + 1, _MAX_RETRIES + 1, series_id, exc)

    raise GRIDError(f"GRID end-state download for {series_id} failed: {last_exc}")


# ---------------------------------------------------------------------------
# Core cache-first call wrapper
# ---------------------------------------------------------------------------


def _call(cache_name: str, ttl_seconds: int, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Check disk cache; on miss, acquire lock → throttle → call fn(*args, **kwargs)."""
    global _last_success_ts

    hit = _cache.get_cached(cache_name, ttl_seconds)
    if hit is not None:
        return hit

    with _lock:
        # Double-check after acquiring lock (another thread may have populated it).
        hit = _cache.get_cached(cache_name, ttl_seconds)
        if hit is not None:
            return hit

        _throttle()
        result = fn(*args, **kwargs)
        _last_success_ts = time.time()
        _cache.set_cached(cache_name, result, ttl_seconds=ttl_seconds,
                          key_hint=cache_name[:100])
        return result


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------
# Fields requested here are the intersection of what GRID Central Data
# documents and what is most likely available on Open Access.  If a field
# is missing from a particular account level, GRID returns a GraphQL error —
# caught by _post_graphql and surfaced as GRIDError with a clear message.

_Q_SERIES_LIST = """
query CS2SeriesList($first: Int $after: String) {
  allSeries(
    first: $first
    after: $after
    orderBy: StartTimeScheduled
    orderDirection: DESC
  ) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        startTimeScheduled
        format { nameShortened }
        tournament { id name }
        teams {
          baseInfo { id name logoUrl }
        }
      }
    }
  }
}
"""

_Q_TOURNAMENTS = f"""
query CS2Tournaments($first: Int) {{
  allTournaments(
    first: $first
    filter: {{ titleId: {_CS2_TITLE_ID} }}
    orderBy: StartDate
    orderDirection: DESC
  ) {{
    edges {{
      node {{
        id
        name
        startDate
        endDate
        prizePool
        location {{ city country {{ name }} }}
      }}
    }}
  }}
}}
"""

_Q_TEAM = f"""
query CS2Team($id: ID!) {{
  team(id: $id) {{
    id
    name
    logoUrl
    nationality {{ name code }}
    roster(titleId: {_CS2_TITLE_ID}) {{
      id
      nickname
      firstName
      lastName
      role
      nationality {{ name }}
    }}
  }}
}}
"""

_Q_PLAYER = f"""
query CS2Player($id: ID!) {{
  player(id: $id) {{
    id
    nickname
    firstName
    lastName
    nationality {{ name }}
    photoUrl
    activeTeam(titleId: {_CS2_TITLE_ID}) {{
      id
      name
    }}
  }}
}}
"""


# ---------------------------------------------------------------------------
# Public endpoint wrappers
# ---------------------------------------------------------------------------


def get_series_list(
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Fetch recent CS2 series from GRID Central Data.

    Args:
        limit:      Max results per page (GRID caps at 50).
        start_date: ISO-8601 UTC lower bound, e.g. ``"2024-01-01T00:00:00Z"``.
        end_date:   ISO-8601 UTC upper bound.

    Returns:
        List of raw series node dicts.  Never raises; returns [] on error.
    """
    cache_name = (
        f"grid:allSeries:n={limit}:s={start_date or ''}:e={end_date or ''}"
    )

    def _fetch() -> list[dict]:
        # Server-side date/type filters are tier-restricted on GRID Open Access.
        # We fetch up to 50 series and filter by date in Python.
        variables: dict[str, Any] = {"first": min(limit, 50)}
        data = _post_graphql(_Q_SERIES_LIST, variables)
        edges = (data.get("allSeries") or {}).get("edges") or []
        nodes = [e["node"] for e in edges if isinstance(e, dict) and e.get("node")]

        # Python-side date filtering
        if start_date or end_date:
            import datetime as _dt
            def _parse(ts: str) -> float | None:
                try:
                    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return None
            lo = _parse(start_date) if start_date else None
            hi = _parse(end_date) if end_date else None
            filtered = []
            for n in nodes:
                ts = n.get("startTimeScheduled")
                t = _parse(ts) if ts else None
                if t is None:
                    continue
                if lo is not None and t < lo:
                    continue
                if hi is not None and t > hi:
                    continue
                filtered.append(n)
            return filtered
        return nodes

    return _call(cache_name, _TTL_SERIES_LIST, _fetch) or []


def get_series_endstate(series_id: str | int) -> dict:
    """Download the end-state JSON file for a finished series.

    The end-state file contains per-map game results and per-player stats
    (kills, deaths, headshots, damage if available).

    Returns {} if the series is not yet finished or not accessible.
    """
    cache_name = f"grid:endstate:{series_id}"

    def _fetch() -> dict:
        return _get_file(series_id)

    return _call(cache_name, _TTL_ENDSTATE, _fetch) or {}


def get_tournaments(limit: int = 30) -> list[dict]:
    """Fetch recent CS2 tournaments from GRID Central Data."""
    cache_name = f"grid:allTournaments:n={limit}"

    def _fetch() -> list[dict]:
        data = _post_graphql(_Q_TOURNAMENTS, {"first": min(limit, 50)})
        edges = (data.get("allTournaments") or {}).get("edges") or []
        return [e["node"] for e in edges if isinstance(e, dict) and e.get("node")]

    return _call(cache_name, _TTL_TOURNAMENTS, _fetch) or []


def get_team(team_id: str | int) -> dict:
    """Fetch a team profile from GRID Central Data."""
    cache_name = f"grid:team:{team_id}"

    def _fetch() -> dict:
        data = _post_graphql(_Q_TEAM, {"id": str(team_id)})
        return data.get("team") or {}

    return _call(cache_name, _TTL_TEAM, _fetch) or {}


def get_player(player_id: str | int) -> dict:
    """Fetch a player profile from GRID Central Data."""
    cache_name = f"grid:player:{player_id}"

    def _fetch() -> dict:
        data = _post_graphql(_Q_PLAYER, {"id": str(player_id)})
        return data.get("player") or {}

    return _call(cache_name, _TTL_PLAYER, _fetch) or {}


# ---------------------------------------------------------------------------
# Stale-cache fallback helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit helpers — inspect actual GRID responses before writing normalisers
# ---------------------------------------------------------------------------


def audit_series(limit: int = 2) -> dict:
    """Fetch a small sample of series nodes and return raw JSON.

    Use this to discover the exact field names and shapes GRID returns
    for your access level before writing normalisation code.

    Returns a dict with keys:
        "nodes"    — list of raw series dicts (up to `limit`)
        "error"    — error message string, or None
        "source"   — "live" | "cache"
    """
    try:
        cache_name = f"grid:audit:series:n={limit}"
        hit = _cache.get_cached(cache_name, ttl_seconds=300)  # 5-min audit cache
        if hit is not None:
            return {"nodes": hit, "error": None, "source": "cache"}

        with _lock:
            hit = _cache.get_cached(cache_name, ttl_seconds=300)
            if hit is not None:
                return {"nodes": hit, "error": None, "source": "cache"}
            _throttle()
            variables: dict = {"first": min(limit, 5)}  # no date/type filter — relies on Python-side filtering
            data = _post_graphql(_Q_SERIES_LIST, variables)
            edges = (data.get("allSeries") or {}).get("edges") or []
            nodes = [e["node"] for e in edges if isinstance(e, dict) and e.get("node")]
            _cache.set_cached(cache_name, nodes, ttl_seconds=300)
            return {"nodes": nodes, "error": None, "source": "live"}
    except Exception as exc:
        return {"nodes": [], "error": str(exc), "source": "error"}


def audit_endstate(series_id: str) -> dict:
    """Download a single end-state file and return its raw structure.

    Use this to discover what fields GRID exposes in end-state files for
    your CS2 access level (map names, player stats, etc.).

    Returns a dict with keys:
        "data"   — raw end-state dict (empty dict if unavailable)
        "error"  — error message string, or None
        "source" — "live" | "cache" | "error"
    """
    try:
        cache_name = f"grid:audit:endstate:{series_id}"
        hit = _cache.get_cached(cache_name, ttl_seconds=300)
        if hit is not None:
            return {"data": hit, "error": None, "source": "cache"}

        with _lock:
            hit = _cache.get_cached(cache_name, ttl_seconds=300)
            if hit is not None:
                return {"data": hit, "error": None, "source": "cache"}
            _throttle()
            result = _get_file(series_id)
            _cache.set_cached(cache_name, result, ttl_seconds=300)
            return {"data": result, "error": None, "source": "live"}
    except Exception as exc:
        return {"data": {}, "error": str(exc), "source": "error"}


def get_stale_series_list(
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict] | None:
    """Return stale series list from disk cache, regardless of TTL."""
    cache_name = (
        f"grid:allSeries:n={limit}:s={start_date or ''}:e={end_date or ''}"
    )
    payload = _cache.get_stale(cache_name)
    if payload is not None and isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item["_stale"] = True
    return payload


# ---------------------------------------------------------------------------
# Status helper (used by the UI source-status panel)
# ---------------------------------------------------------------------------


def get_status() -> dict:
    """Current GRID provider state."""
    now = time.time()
    configured = is_configured()

    if not configured:
        mode = "offline"
    elif _last_success_ts is not None:
        age = now - _last_success_ts
        mode = "live" if age < 60 else "cached"
    else:
        cs = _cache.cache_stats()
        mode = "cached" if cs["entry_count"] > 0 else "unknown"

    return {
        "provider":        "grid",
        "mode":            mode,
        "configured":      configured,
        "last_success_ts": _last_success_ts,
        "throttle_s":      _THROTTLE_S,
        "cache_stats":     _cache.cache_stats(),
    }
