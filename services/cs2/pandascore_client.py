"""Thin HTTP client for the PandaScore REST API (CS2 / CSGO).

Mirrors the resilience model of ``services.nba_client`` on a smaller scale:

1. **Per-request throttle** — ``PANDASCORE_MIN_CALL_INTERVAL_S`` between
   consecutive outbound requests prevents bursts on batch operations.

2. **Per-endpoint failure cache** — after an endpoint+params combo fails,
   a cooldown (``PANDASCORE_FAILURE_COOLDOWN_S``) blocks retries so the UI
   rerunning doesn't re-hammer a dead endpoint.

3. **Global circuit breaker** — ``PANDASCORE_CB_THRESHOLD`` failures in
   ``PANDASCORE_CB_WINDOW_S`` seconds trips the breaker and blocks all live
   requests for ``PANDASCORE_CB_RESET_S`` seconds.

4. **SQLite cache + stale fallback** — every successful JSON response is
   written to ``database.cache_db`` under the ``cs2_pandascore`` namespace.
   If a live fetch fails but SQLite still has an older payload, we return
   that stale payload marked with ``_stale=True`` so the UI can surface a
   warning.

Pure requests.get under the hood — no third-party SDK needed. PandaScore
auth is done via ``Authorization: Bearer <token>`` header.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from typing import Any

import requests

from config import (
    PANDASCORE_BASE_URL,
    PANDASCORE_CB_RESET_S,
    PANDASCORE_CB_THRESHOLD,
    PANDASCORE_CB_WINDOW_S,
    PANDASCORE_FAILURE_COOLDOWN_S,
    PANDASCORE_MAX_RETRIES,
    PANDASCORE_MIN_CALL_INTERVAL_S,
    PANDASCORE_RETRY_BACKOFF,
    PANDASCORE_TIMEOUT,
    pandascore_token,
)
from database import cache_db
from services.cs2.constants import CACHE_NAMESPACE

logger = logging.getLogger(__name__)


class PandaScoreError(RuntimeError):
    """Raised when a PandaScore call ultimately fails (no cache fallback)."""


class PandaScoreNotConfigured(PandaScoreError):
    """No ``PANDASCORE_TOKEN`` is set. The UI shows a setup banner."""


# ---------------------------------------------------------------------------
# Failure cache  (endpoint+params combo → cooldown-until timestamp)
# ---------------------------------------------------------------------------

_failure_cache: dict[str, float] = {}
_failure_cache_lock = threading.Lock()


def _failure_key(path: str, params: dict | None) -> str:
    blob = json.dumps({"path": path, "params": params or {}}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _record_failure(key: str) -> None:
    with _failure_cache_lock:
        _failure_cache[key] = time.time() + PANDASCORE_FAILURE_COOLDOWN_S


def _in_cooldown(key: str) -> bool:
    with _failure_cache_lock:
        until = _failure_cache.get(key)
        if until is None:
            return False
        if time.time() < until:
            return True
        _failure_cache.pop(key, None)
        return False


# ---------------------------------------------------------------------------
# Circuit breaker  (global)
# ---------------------------------------------------------------------------

_cb_failures: deque[float] = deque(maxlen=PANDASCORE_CB_THRESHOLD * 4)
_cb_open_until: float = 0.0
_cb_lock = threading.Lock()


def _cb_is_open() -> bool:
    return time.time() < _cb_open_until


def _cb_register_failure() -> None:
    global _cb_open_until
    now = time.time()
    with _cb_lock:
        _cb_failures.append(now)
        while _cb_failures and (now - _cb_failures[0]) > PANDASCORE_CB_WINDOW_S:
            _cb_failures.popleft()
        if len(_cb_failures) >= PANDASCORE_CB_THRESHOLD:
            _cb_open_until = now + PANDASCORE_CB_RESET_S
            logger.warning("PandaScore circuit breaker tripped for %ss",
                           PANDASCORE_CB_RESET_S)


def reset_circuit_breaker() -> None:
    """Manually reset breaker + per-endpoint failure cache (used by the
    sidebar "Refresh data" button)."""
    global _cb_open_until
    with _cb_lock:
        _cb_failures.clear()
        _cb_open_until = 0.0
    with _failure_cache_lock:
        _failure_cache.clear()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_last_call_ts: float = 0.0
_rate_lock = threading.Lock()


def _throttle() -> None:
    global _last_call_ts
    with _rate_lock:
        now = time.time()
        wait = PANDASCORE_MIN_CALL_INTERVAL_S - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def build_cache_key(path: str, params: dict | None) -> str:
    """Public for unit tests. Returns a stable sha1 key for a request."""
    blob = json.dumps({"path": path, "params": params or {}}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def request(path: str, *, params: dict | None = None,
             ttl_seconds: int | None, allow_stale: bool = True) -> Any:
    """GET ``path`` from PandaScore and return the parsed JSON payload.

    Args:
        path: the endpoint path beginning with ``/`` (e.g. ``"/csgo/matches/upcoming"``).
        params: query params as a plain dict. Lists are serialised as
            comma-separated values per PandaScore convention.
        ttl_seconds: cache TTL. ``None`` means "cache forever" (use for
            immutable historical data such as finished match detail).
        allow_stale: if True, fall back to any cached payload on fetch
            failure regardless of TTL, setting ``_stale=True`` on returned
            dicts so the UI can show a banner.

    Raises:
        PandaScoreNotConfigured: no API token configured.
        PandaScoreError: live fetch failed and no cached fallback available.
    """
    token = pandascore_token()
    if not token:
        raise PandaScoreNotConfigured(
            "PANDASCORE_TOKEN is not set. See README → CS2 setup."
        )

    cache_key = build_cache_key(path, params)

    # 1. Fresh cache hit.
    hit = cache_db.get_cached(CACHE_NAMESPACE, cache_key, ttl_seconds=ttl_seconds)
    if hit is not None:
        return hit

    # 2. Fast-fail paths.
    if _cb_is_open():
        logger.info("PandaScore circuit breaker open; using stale cache if present")
        stale = _stale_or_raise(cache_key, allow_stale, "circuit-breaker open")
        return stale

    if _in_cooldown(cache_key):
        logger.info("PandaScore endpoint %s in cooldown; using stale cache", path)
        stale = _stale_or_raise(cache_key, allow_stale, "endpoint cooldown")
        return stale

    # 3. Live fetch with retries.
    url = f"{PANDASCORE_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    query = _flatten_params(params)

    attempts = max(1, PANDASCORE_MAX_RETRIES + 1)
    last_exc: Exception | None = None
    for i in range(attempts):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, params=query,
                                timeout=PANDASCORE_TIMEOUT)
        except Exception as exc:
            last_exc = exc
            logger.warning("PandaScore call %s network error (attempt %d/%d): %s",
                           path, i + 1, attempts, exc)
            if i < attempts - 1:
                time.sleep(PANDASCORE_RETRY_BACKOFF ** i
                           + random.uniform(0, 0.25))
            continue

        # Non-retryable auth failure: bad or expired token. Don't burn
        # retries and don't trip the circuit breaker — just tell the user.
        if resp.status_code in (401, 403):
            raise PandaScoreNotConfigured(
                f"PandaScore returned {resp.status_code} — check that your "
                "PANDASCORE_TOKEN is valid and has CS2 access."
            )
        # Rate-limited: honour Retry-After then loop. Still a recoverable
        # failure, so it flows through the retry path.
        if resp.status_code == 429:
            wait = _parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning("PandaScore rate limited (429); sleeping %.1fs", wait)
            last_exc = requests.HTTPError(f"429 rate limited at {url}")
            time.sleep(wait)
            if i < attempts - 1:
                continue
            break
        try:
            resp.raise_for_status()
            payload = resp.json()
            cache_db.set_cached(CACHE_NAMESPACE, cache_key, payload)
            return payload
        except Exception as exc:
            last_exc = exc
            logger.warning("PandaScore call %s failed (attempt %d/%d): %s",
                           path, i + 1, attempts, exc)
            if i < attempts - 1:
                time.sleep(PANDASCORE_RETRY_BACKOFF ** i
                           + random.uniform(0, 0.25))

    # 4. All retries failed — record for cooldowns.
    _record_failure(cache_key)
    _cb_register_failure()

    stale = _stale_or_raise(cache_key, allow_stale,
                             f"live fetch failed: {last_exc}")
    return stale


def _stale_or_raise(cache_key: str, allow_stale: bool, reason: str) -> Any:
    if allow_stale:
        stale = cache_db.get_cached_stale(CACHE_NAMESPACE, cache_key)
        if stale is not None:
            _mark_stale(stale)
            logger.info("Returning stale PandaScore payload (%s)", reason)
            return stale
    raise PandaScoreError(f"No data available and fetch failed: {reason}")


def _mark_stale(payload: Any) -> None:
    """Annotate a stale payload so the UI can surface a warning.

    ``dict`` → set ``_stale=True`` on the payload itself.
    ``list`` → set ``_stale=True`` on every element that is a dict, so
    ``any(item.get("_stale") for item in payload)`` works upstream.
    """
    if isinstance(payload, dict):
        payload.setdefault("_stale", True)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item.setdefault("_stale", True)


def _parse_retry_after(value: Any) -> float:
    """Parse a Retry-After header (seconds) defensively. Clamps to 0.5..30s."""
    try:
        return max(0.5, min(30.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _flatten_params(params: dict | None) -> dict | None:
    """PandaScore uses comma-separated values for array-style filters."""
    if not params:
        return params
    out: dict = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = v
    return out
