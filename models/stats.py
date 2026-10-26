"""Derived stat columns.

Two categories:

1. **Sum-based** (PRA, PA, RA, PR, Stocks) — driven by ``DERIVED_STATS`` in
   config.  Each derived column = sum of its component columns per row.

2. **Threshold-based** (DD, TD) — driven by ``THRESHOLD_STATS`` in config.
   Each column is 0 or 1 per row.  A row earns 1 when at least
   ``min_categories`` of the listed columns each reach ``threshold``.

Adding these as real columns means every downstream function (rolling,
splits, hit_rate, projection) works identically for all stat types.
"""

from __future__ import annotations

import pandas as pd

from config import DERIVED_STATS, STAT_COLUMNS, THRESHOLD_STATS


def add_derived_stats(
    df: pd.DataFrame,
    *,
    derived_stats: dict[str, tuple[str, ...]] | None = None,
    threshold_stats: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with all sum-based and threshold-based derived
    stat columns appended.  Silently skips any stat whose source columns are
    absent."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    dspec = DERIVED_STATS if derived_stats is None else derived_stats
    out = df.copy()

    # ── Sum-based (PRA, PA, RA, PR, Stocks) ────────────────────────────────
    for name, components in dspec.items():
        if name in out.columns:
            continue  # already present (e.g. from cache)
        missing = [c for c in components if c not in out.columns]
        if missing:
            continue
        out[name] = sum(pd.to_numeric(out[c], errors="coerce") for c in components)

    # ── Threshold-based (DD, TD) ────────────────────────────────────────────
    out = _add_threshold_stats(out, threshold_specs=threshold_stats)

    return out


def _add_threshold_stats(df: pd.DataFrame,
                         threshold_specs: dict[str, dict] | None = None) -> pd.DataFrame:
    """Add 0/1 columns for every entry in THRESHOLD_STATS.

    For each row, counts how many of the specified columns are >= threshold.
    If that count >= min_categories the result is 1, else 0.
    """
    for name, spec in (threshold_specs or THRESHOLD_STATS).items():
        if name in df.columns:
            continue
        cols      = spec["columns"]
        threshold = spec["threshold"]
        min_cats  = spec["min_categories"]

        # Only proceed when at least one source column is present.
        available = [c for c in cols if c in df.columns]
        if not available:
            continue

        # Count how many available columns hit the threshold row-by-row.
        hits = sum(
            (pd.to_numeric(df[c], errors="coerce") >= threshold).astype(int)
            for c in available
        )
        df[name] = (hits >= min_cats).astype(float)

    return df


def resolve_stat_column(
    stat_label: str,
    *,
    stat_columns: dict[str, str] | None = None,
) -> str:
    """Map a UI stat label (e.g. 'Points', 'PRA', '1Q Points') to its
    DataFrame column name."""
    cols = STAT_COLUMNS if stat_columns is None else stat_columns
    if stat_label in cols:
        return cols[stat_label]
    # Also accept passing the raw column name directly.
    if stat_label in cols.values():
        return stat_label
    raise KeyError(f"Unknown stat label: {stat_label!r}")


def ensure_stat_column(df: pd.DataFrame, stat_col: str) -> pd.Series:
    """Return the stat column as a numeric Series, or an empty Series if
    the column is absent."""
    if df is None or df.empty or stat_col not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[stat_col], errors="coerce")
