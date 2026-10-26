"""Thin retry/timeout wrapper around nba_api endpoint calls.

Three mechanisms prevent the "timeout storm" seen when stats.nba.com is slow:

1. Per-endpoint failure cache
   After an endpoint+params combo fails, a cooldown timer (NBA_API_FAILURE_COOLDOWN_S)
   prevents any retry for that combination. Streamlit reruns (triggered by any widget
   interaction) no longer re-hammer a dead endpoint — they fail fast and return stale
   cached data instead.

2. Global circuit breaker
   If >= NBA_API_CB_THRESHOLD distinct endpoint calls fail within NBA_API_CB_WINDOW_S
   seconds, the breaker trips and ALL live requests fail immediately for NBA_API_CB_RESET_S
   seconds. This prevents 15 teammate log fetches from each blocking for 26s when the
   server is clearly unavailable.

3. Per-call rate limiter
   Enforces a minimum gap (NBA_API_MIN_CALL_INTERVAL_S) between consecutive outbound
   calls to stats.nba.com. Prevents burst-sending when loading a full roster's game logs.

These work together: a cold teammate table with 15 players that previously took up to
22 minutes (15 × 90s) now fails in under 2 minutes: first 2-3 players exhaust the
circuit breaker window (60s, 3 failures), then all subsequent calls fail immediately,
and the stale cache fallback returns any previously saved data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from typing import Any, Callable, Type

import pandas as pd

from config import (
    NBA_API_CB_RESET_S,
    NBA_API_CB_THRESHOLD,
    NBA_API_CB_WINDOW_S,
    NBA_API_FAILURE_COOLDOWN_S,
    NBA_API_MAX_RETRIES,
    NBA_API_MIN_CALL_INTERVAL_S,
    NBA_API_RETRY_BACKOFF,
    NBA_API_TIMEOUT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed error
# ---------------------------------------------------------------------------


class NBAApiError(RuntimeError):
    """Raised when an nba_api call ultimately fails after all retries."""


# ---------------------------------------------------------------------------
# Per-endpoint failure cache
# ---------------------------------------------------------------------------

# Maps _endpoint_key(cls, kwargs) → timestamp_of_last_failure.
# Thread-safe via _failure_lock.
_failure_cache: dict[str, float] = {}
_failure_lock = threading.Lock()


def _endpoint_key(endpoint_cls: Type, kwargs: dict) -> str:
    """Stable hash key that identifies a unique (endpoint, params) pair."""
    try:
        kw_str = json.dumps(sorted(kwargs.items()), default=str, sort_keys=True)
    except Exception:
        kw_str = str(sorted(kwargs.items()))
    h = hashlib.md5(kw_str.encode()).hexdigest()[:10]
    return f"{endpoint_cls.__name__}:{h}"


def _in_failure_cooldown(key: str) -> bool:
    with _failure_lock:
        ts = _failure_cache.get(key)
    if ts is None:
        return False
    return (time.monotonic() - ts) < NBA_API_FAILURE_COOLDOWN_S


def _record_failure(key: str) -> None:
    with _failure_lock:
        _failure_cache[key] = time.monotonic()


def _clear_failure(key: str) -> None:
    with _failure_lock:
        _failure_cache.pop(key, None)


def clear_failure_cache() -> None:
    """Remove all failure cooldown entries (e.g. after a manual refresh)."""
    with _failure_lock:
        _failure_cache.clear()


# ---------------------------------------------------------------------------
# Global circuit breaker
# ---------------------------------------------------------------------------
# Tracks timestamps of recent failures; trips when the count >= threshold.
# State: "closed" (normal), "open" (all requests fail fast), "half-open" (one test).

_cb_lock = threading.Lock()
_cb_failure_times: deque[float] = deque()   # timestamps of recent failures
_cb_open_since: float | None = None          # when the breaker tripped


def _cb_record_failure() -> None:
    now = time.monotonic()
    with _cb_lock:
        _cb_failure_times.append(now)
        # Drop events older than the window.
        while _cb_failure_times and (now - _cb_failure_times[0]) > NBA_API_CB_WINDOW_S:
            _cb_failure_times.popleft()
        # Trip the breaker if threshold is reached.
        global _cb_open_since
        if len(_cb_failure_times) >= NBA_API_CB_THRESHOLD and _cb_open_since is None:
            _cb_open_since = now
            logger.warning(
                "nba_api circuit breaker OPEN — %d failures in %.0fs. "
                "All live requests paused for %ds.",
                len(_cb_failure_times), NBA_API_CB_WINDOW_S, NBA_API_CB_RESET_S,
            )


def _cb_is_open() -> bool:
    global _cb_open_since
    with _cb_lock:
        if _cb_open_since is None:
            return False
        elapsed = time.monotonic() - _cb_open_since
        if elapsed >= NBA_API_CB_RESET_S:
            # Reset to closed — allow the next call through as a probe.
            _cb_open_since = None
            _cb_failure_times.clear()
            logger.info("nba_api circuit breaker CLOSED (reset after %.0fs).", elapsed)
            return False
        return True


def reset_circuit_breaker() -> None:
    """Manually reset the circuit breaker (e.g. after a user-triggered refresh)."""
    global _cb_open_since
    with _cb_lock:
        _cb_open_since = None
        _cb_failure_times.clear()
    clear_failure_cache()
    logger.info("nba_api circuit breaker manually reset.")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_last_call_at: float = 0.0


def _rate_limit() -> None:
    """Sleep just enough to keep consecutive calls at least MIN_CALL_INTERVAL_S apart."""
    global _last_call_at
    with _rate_lock:
        now = time.monotonic()
        gap = now - _last_call_at
        if gap < NBA_API_MIN_CALL_INTERVAL_S:
            time.sleep(NBA_API_MIN_CALL_INTERVAL_S - gap)
        _last_call_at = time.monotonic()


# ---------------------------------------------------------------------------
# Main call helper
# ---------------------------------------------------------------------------


def call_endpoint(
    endpoint_cls: Type,
    *,
    timeout: int | None = None,
    max_retries: int | None = None,
    skip_failure_cache: bool = False,
    **kwargs: Any,
):
    """Instantiate an nba_api endpoint with retry/backoff and circuit breaker.

    Returns the endpoint instance on success. Raises `NBAApiError` on final
    failure after exhausting retries (or immediately when the circuit breaker
    or per-endpoint cooldown is active).

    Args:
        endpoint_cls:        The nba_api endpoint class (e.g. PlayerGameLog).
        timeout:             Per-request timeout in seconds. Defaults to NBA_API_TIMEOUT.
        max_retries:         Override the global retry count.
        skip_failure_cache:  If True, ignore the per-endpoint cooldown (use for
                             user-triggered manual refreshes only).
        **kwargs:            Passed directly to the endpoint constructor.
    """
    t = timeout or NBA_API_TIMEOUT
    retries = max_retries if max_retries is not None else NBA_API_MAX_RETRIES
    key = _endpoint_key(endpoint_cls, kwargs)

    # --- Fast-fail checks (no network call at all) ---
    if _cb_is_open():
        raise NBAApiError(
            f"nba_api circuit breaker is OPEN — {endpoint_cls.__name__} skipped. "
            f"stats.nba.com appears unavailable. Will retry in {NBA_API_CB_RESET_S}s."
        )
    if not skip_failure_cache and _in_failure_cooldown(key):
        raise NBAApiError(
            f"nba_api endpoint {endpoint_cls.__name__} is in failure cooldown "
            f"({NBA_API_FAILURE_COOLDOWN_S}s). Returning from cache if available."
        )

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            _rate_limit()
            result = endpoint_cls(timeout=t, **kwargs)
            # Success — clear any previous failure record for this combination.
            _clear_failure(key)
            return result
        except Exception as exc:
            last_exc = exc
            is_last = (attempt >= retries)
            if not is_last:
                sleep_s = min(
                    NBA_API_RETRY_BACKOFF ** attempt + random.uniform(0.0, 0.5),
                    6.0,   # cap backoff at 6s to avoid long queuing
                )
                logger.warning(
                    "nba_api %s failed (attempt %d/%d): %s; retrying in %.1fs",
                    endpoint_cls.__name__, attempt + 1, retries + 1, exc, sleep_s,
                )
                time.sleep(sleep_s)

    # All attempts exhausted — record failure in both caches.
    _record_failure(key)
    _cb_record_failure()
    raise NBAApiError(
        f"nba_api endpoint {endpoint_cls.__name__} failed after {retries + 1} "
        f"attempt(s): {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def safe_dataframe(fn: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """Run a callable that returns a DataFrame; on any error return empty.

    Use this when a missing or failing upstream call should degrade the UI
    rather than bubble an exception.
    """
    try:
        df = fn()
        if df is None:
            return pd.DataFrame()
        return df
    except Exception as exc:
        logger.warning("safe_dataframe fallback: %s", exc)
        return pd.DataFrame()
