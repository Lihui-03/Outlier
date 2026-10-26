"""Logging configuration.

Called once from app.py so logs go to stderr with a consistent format.
"""

from __future__ import annotations

import logging
import os


def configure_logging(level: str | None = None) -> None:
    """Configure root logger. Idempotent."""
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if root.handlers:
        # Already configured (e.g. by Streamlit's reloader). Just set level.
        root.setLevel(numeric_level)
        return

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # nba_api is noisy at DEBUG; keep it at WARN unless the user explicitly
    # cranked up logging.
    if numeric_level > logging.DEBUG:
        logging.getLogger("nba_api").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
