"""SQLite-backed persistent cache for nba_api responses."""

from .cache_db import (
    get_cached,
    set_cached,
    clear_namespace,
    clear_all,
    init_db,
)

__all__ = [
    "get_cached",
    "set_cached",
    "clear_namespace",
    "clear_all",
    "init_db",
]
