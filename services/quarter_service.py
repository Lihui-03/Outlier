"""Quarter / first-half stats (1Q, 1H) for Research mode.

Historical note: versions prior to Feb 2026 derived these from
``BoxScoreTraditionalV2`` ``StartPeriod``/``EndPeriod`` slices. That path is
**deprecated upstream** (and returns bogus or empty splits for newer seasons).

The authoritative implementation now lives in :mod:`services.partial_game_service`
and uses ``PlayByPlayV3`` + ``BoxScoreTraditionalV3`` rosters — see module docstrings
there for caveats on assist attribution."""

from __future__ import annotations

from services.partial_game_service import (
    enrich_game_log_partial_stats as enrich_with_quarter_stats,
    partial_derived_stats_cached_for_log as quarter_stats_are_cached,
)

__all__ = ("enrich_with_quarter_stats", "quarter_stats_are_cached")
