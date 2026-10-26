"""HLTV data provider — Python wrapper around the gigobyte/HLTV Node.js bridge.

Anti-ban architecture
---------------------
Every call to ``_call()`` goes through five protection layers:

1. **Cache-first** — disk cache is checked *before* any subprocess call.
   Hot paths (recent results, upcoming matches) have short TTLs (15 min).
   Historical paths (player/team stats) have long TTLs (24 h).

2. **Serialised requests** — ``_lock`` ensures at most one Node.js bridge
   subprocess runs at a time.  Burst requests from Streamlit reruns or
   multiple tabs are safely queued.

3. **Throttle** — ``CS2_HLTV_THROTTLE_SECONDS`` (default 3 s) of sleep
   between consecutive outbound requests.

4. **Circuit breaker / cooldown** — after ``CS2_HLTV_CB_THRESHOLD``
   consecutive failures within ``CS2_HLTV_CB_WINDOW_S`` seconds the
   breaker opens for ``CS2_HLTV_COOLDOWN_SECONDS`` (default 300 s).
   During cooldown every call returns stale-cached data without
   hitting the network.

5. **Max retries** — at most ``CS2_HLTV_MAX_RETRIES`` (default 1) per call;
   no retry storms.

Response shapes
---------------
Raw HLTV JSON is returned as plain Python dicts/lists.  Normalisation into
``services.cs2.models`` dataclasses happens in ``cs2_data_provider.py``.

Stale-cache fallback
--------------------
When a live call fails (Cloudflare block, network error, cooldown) and a
stale payload is present on disk, ``_call()`` returns the stale payload
with the ``"_stale": True`` marker so the UI can surface a banner.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from services.cs2 import cs2_cache_service as _cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (all overridable via environment variables)
# ---------------------------------------------------------------------------

# Path to the Node.js bridge directory.
_BRIDGE_DIR = Path(__file__).parent / "hltv_bridge"

# Seconds to sleep *between* consecutive outbound HLTV requests.
THROTTLE_S: float = float(os.environ.get("CS2_HLTV_THROTTLE_SECONDS", "3"))

# Max retries per call (0 = no retry, only first attempt).
MAX_RETRIES: int = int(os.environ.get("CS2_HLTV_MAX_RETRIES", "1"))

# Circuit-breaker: N consecutive failures in WINDOW_S → cooldown for COOLDOWN_S.
CB_THRESHOLD: int   = int(os.environ.get("CS2_HLTV_CB_THRESHOLD", "3"))
CB_WINDOW_S: float  = float(os.environ.get("CS2_HLTV_CB_WINDOW_S", "120"))
COOLDOWN_S: float   = float(os.environ.get("CS2_HLTV_COOLDOWN_SECONDS", "300"))

# Node process timeout (seconds).
NODE_TIMEOUT: int   = int(os.environ.get("CS2_HLTV_NODE_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Module-level state (process-wide singletons)
# ---------------------------------------------------------------------------

_lock = threading.Lock()              # serialises all HLTV subprocesses
_last_call_ts: float = 0.0            # unix timestamp of last outbound request
# Note: _last_call_ts is only ever read/written inside `with _lock:` via
# _throttle(), so no additional lock is needed.

# Circuit-breaker state.
_cb_failures: list[float] = []        # timestamps of recent failures
_cb_open_until: float = 0.0
_cb_lock = threading.Lock()

# Track last successful call timestamp.
_last_success_ts: float | None = None


# ---------------------------------------------------------------------------
# Circuit-breaker helpers
# ---------------------------------------------------------------------------


def _cb_is_open() -> bool:
    return time.time() < _cb_open_until


def _cb_register_failure() -> None:
    global _cb_open_until
    now = time.time()
    with _cb_lock:
        _cb_failures.append(now)
        # Drop failures outside the window.
        cutoff = now - CB_WINDOW_S
        while _cb_failures and _cb_failures[0] < cutoff:
            _cb_failures.pop(0)
        if len(_cb_failures) >= CB_THRESHOLD:
            _cb_open_until = now + COOLDOWN_S
            logger.warning(
                "HLTV circuit breaker tripped: %d failures in %.0fs — "
                "pausing requests for %.0fs.",
                len(_cb_failures), CB_WINDOW_S, COOLDOWN_S
            )


def _cb_register_success() -> None:
    global _last_success_ts
    with _cb_lock:
        _cb_failures.clear()
    _last_success_ts = time.time()


def reset_circuit_breaker() -> None:
    """Manually reset the circuit breaker and throttle state.
    Called by the sidebar "Refresh" button so users can unblock a
    spurious cooldown without restarting the app."""
    global _cb_open_until, _last_success_ts
    with _cb_lock:
        _cb_failures.clear()
        _cb_open_until = 0.0
    _last_success_ts = None
    logger.info("HLTV circuit breaker manually reset.")


# ---------------------------------------------------------------------------
# Bridge availability check
# ---------------------------------------------------------------------------


def bridge_installed() -> bool:
    """Return True if the Node.js bridge's node_modules directory exists."""
    return (_BRIDGE_DIR / "node_modules").is_dir()


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------


def _throttle() -> None:
    """Enforce minimum gap between consecutive requests.

    Must only be called from inside ``with _lock:``.  _lock already serialises
    all subprocesses, so no additional lock is needed to protect _last_call_ts.
    """
    global _last_call_ts
    wait = THROTTLE_S - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def _call(method: str, args: dict | None = None, *,
          ttl_seconds: int,
          cache_name: str | None = None) -> Any:
    """Core entry point.  Returns a Python object (list or dict) or raises.

    Flow:
        1. Check disk cache — return if fresh.
        2. If circuit breaker is open, return stale or raise HLTVCooldown.
        3. Serialise via lock, throttle, call Node.js bridge.
        4. On success: cache + return.
        5. On failure: register failure, try stale fallback, else raise.
    """
    name = cache_name or f"{method}:{json.dumps(args or {}, sort_keys=True)}"

    # 1. Fresh cache.
    hit = _cache.get_cached(name, ttl_seconds)
    if hit is not None:
        return hit

    # 2. Circuit-breaker fast-fail.
    if _cb_is_open():
        logger.info("HLTV circuit breaker open — serving stale cache for %s", method)
        stale = _cache.get_stale(name)
        if stale is not None:
            _mark_stale(stale)
            return stale
        raise HLTVCooldown(
            f"HLTV requests are paused (cooldown). "
            f"Will resume in ~{max(0, _cb_open_until - time.time()):.0f}s."
        )

    if not bridge_installed():
        stale = _cache.get_stale(name)
        if stale is not None:
            _mark_stale(stale)
            return stale
        raise HLTVNotReady(
            "HLTV bridge is not installed. "
            "Run: cd services/cs2/hltv_bridge && npm install"
        )

    # 3. Live fetch (serialised).
    with _lock:
        # Re-check cache under lock (another thread may have populated it).
        hit = _cache.get_cached(name, ttl_seconds)
        if hit is not None:
            return hit

        _throttle()

        payload = None
        last_exc: Exception | None = None
        for attempt in range(max(1, MAX_RETRIES + 1)):
            if attempt > 0:
                backoff = min(THROTTLE_S * (2 ** attempt), 30)
                logger.info("HLTV retry %d for %s (backoff %.1fs)", attempt, method, backoff)
                time.sleep(backoff)
            try:
                payload = _run_bridge(method, args or {})
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("HLTV call %s attempt %d failed: %s", method, attempt + 1, exc)

        if payload is not None:
            _cb_register_success()
            _cache.set_cached(name, payload, ttl_seconds=ttl_seconds,
                              key_hint=name[:100])
            return payload

        # 4. All attempts failed.
        _cb_register_failure()
        stale = _cache.get_stale(name)
        if stale is not None:
            _mark_stale(stale)
            logger.info("HLTV returning stale cache for %s after failure: %s",
                        method, last_exc)
            return stale

        raise HLTVError(
            f"HLTV call '{method}' failed and no cached data is available: {last_exc}"
        )


def _run_bridge(method: str, args: dict) -> Any:
    """Invoke the Node.js bridge and return parsed JSON payload."""
    script = _BRIDGE_DIR / "index.mjs"
    if not script.exists():
        raise HLTVNotReady(f"Bridge script not found: {script}")

    cmd_json = json.dumps({"method": method, "args": args})
    result = subprocess.run(
        ["node", str(script), cmd_json],
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT,
        cwd=str(_BRIDGE_DIR),
    )
    raw = result.stdout.strip()
    if not raw:
        stderr = result.stderr.strip()[:300]
        raise HLTVError(f"Bridge produced no output (exit={result.returncode}). stderr: {stderr}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HLTVError(f"Bridge returned invalid JSON: {e}. raw={raw[:200]}") from e

    if not data.get("ok"):
        msg = data.get("error", "unknown error")
        if "cloudflare" in msg.lower() or "access denied" in msg.lower():
            raise HLTVCloudflare(
                f"Cloudflare blocked the request for '{method}'. "
                "This is normal for automated requests. "
                "Stale cache will be used if available, otherwise try again later."
            )
        raise HLTVError(f"HLTV method '{method}' failed: {msg}")

    return data.get("data")


def _mark_stale(payload: Any) -> None:
    """Set ``_stale=True`` on a payload so the UI can show a warning banner."""
    if isinstance(payload, dict):
        payload["_stale"] = True
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item["_stale"] = True


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HLTVError(RuntimeError):
    """General HLTV call failure."""


class HLTVCloudflare(HLTVError):
    """Cloudflare bot-protection blocked the request."""


class HLTVCooldown(HLTVError):
    """Circuit breaker is open; requests are paused."""


class HLTVNotReady(HLTVError):
    """Bridge is not installed or script is missing."""


# ---------------------------------------------------------------------------
# High-level endpoint wrappers
# All TTLs are conservative by default.  Long historical TTLs avoid re-scraping
# data that cannot change (finished match stats, historical player stats).
# ---------------------------------------------------------------------------

# TTLs (seconds)
_TTL_LIVE       = 15 * 60          #  15 min — live / upcoming matches
_TTL_RESULTS    = 6 * 60 * 60      #   6 h   — recent results page
_TTL_PLAYER     = 24 * 60 * 60     #  24 h   — player profile / stats
_TTL_TEAM       = 24 * 60 * 60     #  24 h   — team profile
_TTL_TEAM_STATS = 24 * 60 * 60     #  24 h   — team stats (4+ requests expensive)
_TTL_RANKING    = 4 * 60 * 60      #   4 h   — world ranking
_TTL_MATCH      = 30 * 24 * 60 * 60  # 30 days — finished match detail (immutable)


def get_matches() -> list[dict]:
    """Upcoming and live matches from hltv.org/matches/ (1 request)."""
    return _call("getMatches", {}, ttl_seconds=_TTL_LIVE,
                 cache_name="hltv:getMatches") or []


def get_results(stars: int = 0, limit: int = 30,
                team_ids: list[int] | None = None) -> list[dict]:
    """Recent completed results (1 request for first 100 rows).

    ``stars`` filters by match star rating (0 = all).
    We never paginate automatically to avoid multi-request bursts.
    """
    args: dict[str, Any] = {}
    if stars > 0:
        args["stars"] = stars
    if team_ids:
        args["teamIds"] = team_ids
    name = f"hltv:getResults:s{stars}:t{','.join(str(x) for x in (team_ids or []))}"
    raw = _call("getResults", args, ttl_seconds=_TTL_RESULTS, cache_name=name)
    return (raw or [])[:limit]


def get_match(match_id: int) -> dict:
    """Full match detail including per-map results (1 request)."""
    return _call("getMatch", {"id": match_id},
                 ttl_seconds=_TTL_MATCH,
                 cache_name=f"hltv:getMatch:{match_id}") or {}


def get_player_by_name(name: str) -> dict:
    """Look up a player by IGN, returns full player profile (2 requests)."""
    cache_name = f"hltv:getPlayerByName:{name.lower()}"
    return _call("getPlayerByName", {"name": name},
                 ttl_seconds=_TTL_PLAYER,
                 cache_name=cache_name) or {}


def get_player_stats(player_id: int) -> dict:
    """Full player stats from hltv.org/stats/players/…  (3 requests — lazy only)."""
    return _call("getPlayerStats", {"id": player_id},
                 ttl_seconds=_TTL_PLAYER,
                 cache_name=f"hltv:getPlayerStats:{player_id}") or {}


def get_player_ranking(start_date: str | None = None,
                       end_date: str | None = None) -> list[dict]:
    """Top-rated players from hltv.org/stats/players (1 request)."""
    args: dict[str, Any] = {}
    if start_date:
        args["startDate"] = start_date
    if end_date:
        args["endDate"] = end_date
    name = f"hltv:getPlayerRanking:{start_date}:{end_date}"
    return _call("getPlayerRanking", args, ttl_seconds=_TTL_RANKING,
                 cache_name=name) or []


def get_team(team_id: int) -> dict:
    """Team profile page, includes current roster (1 request)."""
    return _call("getTeam", {"id": team_id},
                 ttl_seconds=_TTL_TEAM,
                 cache_name=f"hltv:getTeam:{team_id}") or {}


def get_team_by_name(name: str) -> dict:
    """Look up a team by name (2 requests — search + detail)."""
    cache_name = f"hltv:getTeamByName:{name.lower()}"
    return _call("getTeamByName", {"name": name},
                 ttl_seconds=_TTL_TEAM,
                 cache_name=cache_name) or {}


def get_team_stats(team_id: int) -> dict:
    """Team stats page (4–5 requests — expensive, lazy-load only)."""
    return _call("getTeamStats", {"id": team_id},
                 ttl_seconds=_TTL_TEAM_STATS,
                 cache_name=f"hltv:getTeamStats:{team_id}") or {}


def get_team_ranking() -> list[dict]:
    """Current HLTV world team ranking (1 request)."""
    return _call("getTeamRanking", {},
                 ttl_seconds=_TTL_RANKING,
                 cache_name="hltv:getTeamRanking") or []


# ---------------------------------------------------------------------------
# Status helper (used by UI source-status panel)
# ---------------------------------------------------------------------------


def get_status() -> dict:
    """Return a dict describing the current HLTV provider state."""
    now = time.time()
    cb_open = _cb_is_open()
    if cb_open:
        mode = "cooldown"
    elif _last_success_ts is None:
        stats = _cache.cache_stats()
        mode = "cached" if stats["entry_count"] > 0 else "unknown"
    else:
        mode = "live" if (now - _last_success_ts) < 60 else "cached"

    return {
        "provider":         "hltv",
        "mode":             mode,
        "bridge_installed": bridge_installed(),
        "last_success_ts":  _last_success_ts,
        "cb_open":          cb_open,
        "cb_open_until":    _cb_open_until if cb_open else None,
        "throttle_s":       THROTTLE_S,
        "cache_stats":      _cache.cache_stats(),
        "pandascore_available": False,   # updated by cs2_data_provider
    }

