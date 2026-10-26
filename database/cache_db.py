"""SQLite-backed persistent cache.

The cache stores JSON payloads keyed by (namespace, cache_key). We use
it to avoid re-hitting the free stats.nba.com endpoints, which are
rate-limited and slow. Payloads are expected to be JSON-serializable
(DataFrames should be converted via `df.to_dict(orient="records")`
before caching).

This module deliberately knows nothing about nba_api; callers decide
how to shape the payload.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _ensure_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if needed. Safe to call repeatedly."""
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect() as conn:
        conn.executescript(schema_sql)


def get_cached(namespace: str, cache_key: str, ttl_seconds: int | None) -> Any | None:
    """Return the cached payload if present and fresh, else None.

    Args:
        namespace: logical bucket (e.g. 'gamelog').
        cache_key: unique key within namespace.
        ttl_seconds: if None, never expires. Otherwise entries older than
            this many seconds are ignored (and treated as a miss).
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, created_at FROM cache_entries "
            "WHERE namespace = ? AND cache_key = ?",
            (namespace, cache_key),
        ).fetchone()

    if row is None:
        return None

    if ttl_seconds is not None:
        age = time.time() - row["created_at"]
        if age > ttl_seconds:
            logger.debug("Cache expired: %s / %s (age=%.0fs)", namespace, cache_key, age)
            return None

    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        logger.warning("Corrupt cache payload for %s/%s; ignoring", namespace, cache_key)
        return None


def set_cached(namespace: str, cache_key: str, payload: Any) -> None:
    """Write (or overwrite) a cache entry."""
    init_db()
    try:
        serialized = json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        logger.error("Refusing to cache unserializable payload for %s/%s: %s",
                     namespace, cache_key, exc)
        return

    with _connect() as conn:
        conn.execute(
            "INSERT INTO cache_entries (namespace, cache_key, payload, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, cache_key) DO UPDATE SET "
            "payload = excluded.payload, created_at = excluded.created_at",
            (namespace, cache_key, serialized, int(time.time())),
        )


def clear_namespace(namespace: str, cache_key_prefix: str | None = None) -> int:
    """Delete all entries in a namespace (optionally with a key prefix).

    Returns the number of rows deleted.
    """
    init_db()
    with _connect() as conn:
        if cache_key_prefix is None:
            cur = conn.execute(
                "DELETE FROM cache_entries WHERE namespace = ?", (namespace,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND cache_key LIKE ?",
                (namespace, f"{cache_key_prefix}%"),
            )
        return cur.rowcount or 0


def get_cached_stale(namespace: str, cache_key: str) -> Any | None:
    """Return any cached payload for the key, ignoring TTL.

    Used as a stale-data fallback when a live fetch fails but the SQLite
    cache has old data that is better than returning nothing. Callers
    should log a warning so the user knows they are seeing stale data.
    """
    return get_cached(namespace, cache_key, ttl_seconds=None)


def get_cache_age(namespace: str, cache_key: str) -> float | None:
    """Return age in seconds of a cached entry, or None if not found."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM cache_entries "
            "WHERE namespace = ? AND cache_key = ?",
            (namespace, cache_key),
        ).fetchone()
    if row is None:
        return None
    return time.time() - row["created_at"]


def clear_all() -> int:
    """Nuke the cache entirely. Returns rows deleted."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM cache_entries")
        return cur.rowcount or 0
