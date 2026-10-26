"""Date helpers: parsing game dates and computing rest-day buckets."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

import numpy as np
import pandas as pd


_COMPACT_DIGITS_RE = re.compile(r"^\d{8}(?:\.0+)?$")


def _timestamp_from_compact_ymd(n: int) -> pd.Timestamp | None:
    """Decode ``YYYYMMDD`` into a calendar Timestamp, or ``None`` if invalid."""
    if not (19900101 <= n <= 21001231):
        return None
    y, md = divmod(n, 10000)
    mo, da = divmod(md, 100)
    if not (1 <= mo <= 12 and 1 <= da <= 31):
        return None
    try:
        return pd.Timestamp(year=y, month=mo, day=da)
    except (ValueError, OverflowError):
        return None


def coerce_game_datetime(value: Any) -> pd.Timestamp:
    """Best-effort parse for a single ``GAME_DATE`` cell (recovery / charts).

    Handles NBA / cache quirks:

    - **Numeric ``YYYYMMDD``** (int/float/str including ``"...\\.0"``) must decode as a
      calendar day. Pandas' default ``to_datetime(int)`` treats integers as nanoseconds
      since epoch, producing bogus ``1970-`` timestamps unless we intercept first.
    - **``"Apr 29,2026"``** (missing space after comma) otherwise yields NaT /
      year ``0001`` in some parsers; we normalize to **``, ``**.
    """
    if value is None:
        return pd.NaT

    try:
        if pd.isna(value) and not isinstance(value, (str, bytes)):
            return pd.NaT
    except (TypeError, ValueError):
        pass

    # bool would match int subclass checks — never a plausible game date
    if isinstance(value, bool):
        return pd.NaT

    if isinstance(value, pd.Timestamp):
        return value if pd.notna(value) else pd.NaT

    if isinstance(value, float):
        if np.isnan(value):
            return pd.NaT

    # ints / floats that are really calendar compact codes (prefer before pd.to_datetime)
    if isinstance(value, (int, np.integer)):
        decoded = _timestamp_from_compact_ymd(int(value))
        if decoded is not None:
            return decoded

    if isinstance(value, (float, np.floating)):
        r = round(float(value))
        if abs(float(value) - r) < 1e-6:
            decoded = _timestamp_from_compact_ymd(int(r))
            if decoded is not None:
                return decoded

    ts = pd.to_datetime(value, errors="coerce")
    if isinstance(ts, pd.Timestamp) and pd.notna(ts):
        return ts

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return pd.NaT
        if s.lower() in ("nan", "none", "null", "<na>"):
            return pd.NaT
        compact = _COMPACT_DIGITS_RE.match(s)
        if compact:
            head = compact.group(0).split(".")[0]
            decoded = _timestamp_from_compact_ymd(int(head))
            if decoded is not None:
                return decoded
        # "Apr 29,2026" → "Apr 29, 2026"
        normalized = re.sub(r",(\d)", r", \1", s)
        try:
            ts_try = pd.Timestamp(normalized)
            if isinstance(ts_try, pd.Timestamp) and pd.notna(ts_try):
                # Reject phantom ancient years pandas sometimes emits for malformed inputs
                if 1980 <= ts_try.year <= 2100:
                    return ts_try
        except (ValueError, TypeError):
            pass
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
            try:
                return pd.Timestamp(datetime.strptime(normalized[:48], fmt))
            except ValueError:
                continue

    return pd.NaT


def parse_game_date(series: pd.Series) -> pd.Series:
    """Parse ``GAME_DATE`` into tz-naive ``datetime64[ns]``.

    Rows pass through ``coerce_game_datetime`` so SQLite/JSON caching and odd
    nba_api string shapes recover consistently (not only the NaT cells from a
    single vectorized pandas pass).
    """
    if series is None or len(series) == 0:
        return series
    mapped = series.map(coerce_game_datetime)
    out = pd.to_datetime(mapped, errors="coerce")
    # Fix rare float YYYYMMDD that coerce turned into bogus 1970 stamps (extra guard)
    if out.dtype.kind == "M":
        bogus = (
            pd.notna(out)
            & (out.dt.year == 1970)
            & series.notna()
        )
        if bogus.any():
            recovered = series.loc[bogus].map(coerce_game_datetime)
            out.loc[bogus] = pd.to_datetime(recovered, errors="coerce")
    return out


def compute_rest_days(game_dates: pd.Series) -> pd.Series:
    """Return days of rest between consecutive games (newest-first ordering aware).

    The input may be in reverse-chronological order (nba_api's default);
    we sort ascending internally, compute diffs, then restore the original
    index alignment. First game of the season gets NaN.
    """
    if game_dates.empty:
        return pd.Series(dtype="float64", index=game_dates.index)

    parsed = parse_game_date(game_dates)
    ordered = parsed.sort_values()
    diffs = ordered.diff().dt.days - 1  # subtract 1: playing next day = 0 rest
    diffs = diffs.clip(lower=0)
    return diffs.reindex(game_dates.index)


def bucket_rest_days(rest: float | None) -> str:
    """Map a rest-days number into a readable bucket."""
    if rest is None:
        return "Unknown"
    try:
        r = float(rest)
    except (TypeError, ValueError):
        return "Unknown"
    if pd.isna(r):
        return "Unknown"
    if r <= 0:
        return "0 (B2B)"
    if r == 1:
        return "1 day"
    if r == 2:
        return "2 days"
    return "3+ days"


def order_rest_buckets() -> Iterable[str]:
    """Canonical ordering for rest-day buckets (for consistent UX)."""
    return ["0 (B2B)", "1 day", "2 days", "3+ days", "Unknown"]
