"""Disk-based JSON cache for CS2 / HLTV data.

Why a separate disk cache (not the shared SQLite cache)?
-------------------------------------------------------
HLTV data is consumed by a Node.js subprocess; the disk cache is a simple
directory of JSON files that both layers can read/write without coordinating
on a SQLite connection.  Long TTLs (hours / days) mean the cache directory
stays small.

Cache layout:
    <CS2_CACHE_DIR>/          (default: data/cs2_hltv_cache/)
        <sha1-key>.json       raw JSON payload
        <sha1-key>.meta.json  {"created_at": <unix>, "ttl": <seconds>, "key_hint": "..."}

Stale-cache semantics:
    * get_cached(key, ttl) → payload if within TTL, else None
    * get_stale(key)       → most-recent payload regardless of TTL (fallback)
    * set_cached(key, data) → writes both files atomically via temp+rename

Thread safety: file operations are protected by a per-key threading.Lock so
concurrent Streamlit reruns do not corrupt cache files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_cache_dir: Path | None = None      # set by init()
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def init(cache_dir: str | Path) -> None:
    """Call once at startup to configure the cache directory."""
    global _cache_dir
    _cache_dir = Path(cache_dir)
    _cache_dir.mkdir(parents=True, exist_ok=True)


def _dir() -> Path:
    """Return the cache directory, initialising with a default if needed."""
    if _cache_dir is None:
        # Lazy default: <workspace>/data/cs2_hltv_cache/
        base = Path(__file__).resolve().parents[2] / "data" / "cs2_hltv_cache"
        init(base)
    return _cache_dir  # type: ignore[return-value]


def _lock_for(key: str) -> threading.Lock:
    with _locks_meta:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cache_key(name: str) -> str:
    """Return a stable sha1 hex string for an arbitrary cache name."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()


def _payload_path(key: str) -> Path:
    return _dir() / f"{key}.json"


def _meta_path(key: str) -> Path:
    return _dir() / f"{key}.meta.json"


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON to path atomically via a temp file + rename."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_cached(name: str, ttl_seconds: int | None) -> Any | None:
    """Return the cached payload if it is within TTL, otherwise None.

    Args:
        name:        A human-readable cache key (e.g. "getResults:stars=1").
        ttl_seconds: Maximum age in seconds.  None = never expire (permanent).
    """
    key = _cache_key(name)
    with _lock_for(key):
        meta_p = _meta_path(key)
        if not meta_p.exists():
            return None
        try:
            meta = json.loads(meta_p.read_text("utf-8"))
        except Exception:
            return None

        if ttl_seconds is not None:
            age = time.time() - meta.get("created_at", 0)
            if age > ttl_seconds:
                return None

        payload_p = _payload_path(key)
        if not payload_p.exists():
            return None
        try:
            return json.loads(payload_p.read_text("utf-8"))
        except Exception:
            return None


def get_stale(name: str) -> Any | None:
    """Return any cached payload regardless of TTL (stale-cache fallback)."""
    key = _cache_key(name)
    with _lock_for(key):
        payload_p = _payload_path(key)
        if not payload_p.exists():
            return None
        try:
            return json.loads(payload_p.read_text("utf-8"))
        except Exception:
            return None


def cache_age_seconds(name: str) -> float | None:
    """Return how old the cache entry is in seconds, or None if absent."""
    key = _cache_key(name)
    meta_p = _meta_path(key)
    if not meta_p.exists():
        return None
    try:
        meta = json.loads(meta_p.read_text("utf-8"))
        return time.time() - meta.get("created_at", 0)
    except Exception:
        return None


def set_cached(name: str, payload: Any, ttl_seconds: int | None = None,
               key_hint: str | None = None) -> None:
    """Write payload to disk cache.

    Args:
        name:        Cache key (same string passed to get_cached).
        payload:     JSON-serialisable value.
        ttl_seconds: For reference only (the TTL used in future get_cached calls).
        key_hint:    Optional human-readable description stored in meta (debug aid).
    """
    key = _cache_key(name)
    with _lock_for(key):
        meta = {
            "created_at": time.time(),
            "ttl":        ttl_seconds,
            "key_hint":   key_hint or name[:120],
        }
        try:
            _atomic_write(_meta_path(key), meta)
            _atomic_write(_payload_path(key), payload)
        except Exception as exc:
            logger.warning("cs2_cache: write failed for %r: %s", name, exc)


def invalidate(name: str) -> None:
    """Remove a cache entry (both payload and meta)."""
    key = _cache_key(name)
    with _lock_for(key):
        for p in (_payload_path(key), _meta_path(key)):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def clear_all() -> int:
    """Delete all cache files. Returns count of files removed."""
    removed = 0
    for p in _dir().glob("*.json"):
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    return removed


def cache_stats() -> dict:
    """Return a summary of the current cache directory."""
    d = _dir()
    entries = list(d.glob("*.meta.json"))
    total_bytes = sum(
        p.stat().st_size for p in d.glob("*.json") if p.exists()
    )
    return {
        "entry_count": len(entries),
        "total_kb": round(total_bytes / 1024, 1),
        "cache_dir": str(d),
    }
