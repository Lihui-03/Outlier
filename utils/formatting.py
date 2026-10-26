"""Display-formatting helpers.

All UI layers should go through these so numbers render consistently.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd


def fmt_number(value: Any, decimals: int = 1, default: str = "-") -> str:
    """Format a numeric value; gracefully handle NaN/None."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    return f"{f:.{decimals}f}"


def fmt_signed(value: Any, decimals: int = 2, default: str = "-") -> str:
    """Format a number with explicit sign (used for edge)."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.{decimals}f}"


def compact_season_label(season_api: str) -> str:
    """Sidebar/API season labels like ``2025-26`` → compact ``25/26``."""
    raw = str(season_api or "").strip().replace("–", "-")
    parts = raw.split("-", 1)
    if len(parts) == 2 and len(parts[0]) >= 4 and parts[1].isdigit():
        start_yy = parts[0][-2:]
        end_part = parts[1]
        end_yy = end_part[-2:] if len(end_part) >= 2 else end_part.zfill(2)
        return f"{start_yy}/{end_yy}"
    return raw or "—"


def fmt_percent(value: Any, decimals: int = 0, default: str = "-") -> str:
    """Format a 0-1 fraction as a percent string."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    return f"{f * 100:.{decimals}f}%"


def over_under_label(edge: float | None) -> str:
    """Translate a signed edge into an Over / Under / Pass label."""
    if edge is None or (isinstance(edge, float) and math.isnan(edge)):
        return "-"
    if edge > 0:
        return "Over"
    if edge < 0:
        return "Under"
    return "Pass"


# ---------------------------------------------------------------------------
# Spread-market helpers (sportsbook notation).
#
# Convention across the app: positive = selected team wins by that much,
# negative = selected team loses by that much. Posted spread shares the
# same sign:
#   -4.5  → favourite, must win by > 4.5
#   +4.5  → underdog, may lose by up to 4.5 (or win outright)
#
# These helpers let UI and projection explanation text all agree on one
# user-friendly rendering without each call site re-computing the sign
# decomposition.
# ---------------------------------------------------------------------------


def format_team_margin(team_abbr: str | None, signed_margin: Any,
                        *, default: str = "-") -> str:
    """Sportsbook-style display: ``"TOR -12.5"`` / ``"TOR +6.2"``.

    Used for both posted spreads and projected margins — they share the
    convention that the signed value is *from the selected team's
    perspective*.

    ``0`` is rendered as ``"TOR PK"`` (pick'em).
    NaN / None / unparseable values return ``default``.
    """
    try:
        value = float(signed_margin)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    abbr = team_abbr or "?"
    if value == 0:
        return f"{abbr} PK"
    sign = "-" if value < 0 else "+"
    return f"{abbr} {sign}{abs(value):.1f}"


def _is_half_point(magnitude: float) -> bool:
    """True when ``magnitude`` ends in .5 (half-point line)."""
    frac = magnitude - int(magnitude)
    return abs(frac - 0.5) < 1e-9


def spread_interpretation(team_abbr: str | None, signed_spread: Any,
                           *, market_window: str = "game") -> str:
    """Return a plain-English explanation of what must happen to cover.

    Examples (market_window = "game"):
        "TOR -12.5" → "TOR -12.5 covers only if TOR wins by 13 or more."
        "TOR +12.5" → "TOR +12.5 covers if TOR loses by 12 or fewer, or
                       wins outright."
        "TOR -12"   → "TOR -12 covers only if TOR wins by 13 or more
                       (push on exactly 12)."

    ``market_window`` picks the temporal phrase:
        "game" → "" (no qualifier)
        "1h"   → " in the 1st half"
        "2h"   → " in the 2nd half"
    """
    try:
        value = float(signed_spread)
    except (TypeError, ValueError):
        return ""
    if math.isnan(value):
        return ""

    abbr = team_abbr or "the selected team"
    window = {"1h": " in the 1st half",
              "2h": " in the 2nd half"}.get(market_window, "")

    magnitude = abs(value)
    if magnitude <= 0:
        return f"Pick'em — {abbr} must win outright{window} to cover."

    m_int = int(magnitude)
    is_half = _is_half_point(magnitude)

    # Underdog: positive posted line, covers on a close loss or a win.
    if value > 0:
        mag_str = f"{magnitude:.1f}" if is_half else f"{m_int}"
        if is_half:
            max_loss = m_int
            return (f"{abbr} +{mag_str} covers if {abbr} loses by "
                    f"{max_loss} or fewer{window}, or wins outright.")
        max_loss = m_int - 1
        if max_loss < 0:
            return (f"{abbr} +0 covers if {abbr} wins outright{window} "
                    "(push on a tie).")
        return (f"{abbr} +{mag_str} covers if {abbr} loses by "
                f"{max_loss} or fewer{window}, or wins outright "
                f"(push on exactly {m_int}).")

    # Favourite: negative posted line, covers only on a sufficient win.
    mag_str = f"{magnitude:.1f}" if is_half else f"{m_int}"
    min_win = m_int + 1
    if is_half:
        return (f"{abbr} -{mag_str} covers only if {abbr} wins by "
                f"{min_win} or more{window}.")
    return (f"{abbr} -{mag_str} covers only if {abbr} wins by "
            f"{min_win} or more{window} (push on exactly {m_int}).")


# ---------------------------------------------------------------------------
# NBA player-prop Research chart (game-by-game bar chart in ui/graph.py)
# ---------------------------------------------------------------------------

_MONTH_ABBR_EN = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Friendly Research dropdown labels → lowercase noun phrase for tooltips.
_PROP_STAT_PHRASE_SUFFIX: dict[str, str] = {
    "Points": "points",
    "Rebounds": "rebounds",
    "Assists": "assists",
    "3PM": "made threes",
    "Steals": "steals",
    "Blocks": "blocks",
    "Turnovers": "turnovers",
    "Off. Rebounds": "offensive rebounds",
    "Def. Rebounds": "defensive rebounds",
    "FT Made": "free throws made",
    "FT Attempts": "free throw attempts",
    "3P Attempts": "three-point attempts",
    "Personal Fouls": "fouls",
    "PRA": "PTS + REB + AST",
    "PA": "PTS + AST",
    "RA": "REB + AST",
    "PR": "PTS + REB",
    "Stocks (STL+BLK)": "stocks",
    "Double-Double": "double-double",
    "Triple-Double": "triple-double",
    "1Q Points": "1st-quarter points",
    "1Q Rebounds": "1st-quarter rebounds",
    "1Q Assists": "1st-quarter assists",
    "1Q 3PM": "1st-quarter made threes",
    "1H Points": "1st-half points",
    "1H Rebounds": "1st-half rebounds",
    "1H Assists": "1st-half assists",
    "1H 3PM": "1st-half made threes",
}


def format_nba_prop_chart_axis_month(chart_ts: Any) -> str:
    """English month abbreviation for compact axis chips (locale-stable)."""
    ts = pd.Timestamp(chart_ts)
    if pd.isna(ts):
        raise ValueError("chart_ts must not be NaT")
    return _MONTH_ABBR_EN[int(ts.month) - 1]


def format_nba_prop_chart_axis_day(chart_ts: Any) -> str:
    """Two-digit day for compact axis chips."""
    ts = pd.Timestamp(chart_ts)
    if pd.isna(ts):
        raise ValueError("chart_ts must not be NaT")
    return f"{int(ts.day):02d}"


def dedupe_compact_axis_month_day(
    months: list[str],
    days: list[str],
    *,
    iso_dates: list[str],
    opponents_upper: list[str],
) -> tuple[list[str], list[str]]:
    """Suffix duplicate calendar/opponent slots so stacked labels stay distinguishable."""
    seen: dict[tuple[str, str], int] = {}
    mo_out: list[str] = []
    day_out: list[str] = []
    for m, d, iso, opp in zip(months, days, iso_dates, opponents_upper):
        key = (iso, opp)
        c = seen.get(key, 0)
        seen[key] = c + 1
        mo_out.append(m)
        day_out.append(d if c == 0 else f"{d} ({c + 1})")
    return mo_out, day_out


def infer_player_team_abbr_from_row(row: Mapping[str, Any]) -> str:
    """Return the player's team tricode from ``TEAM_ABBREVIATION`` or ``MATCHUP``."""
    ta = row.get("TEAM_ABBREVIATION")
    if ta is not None and str(ta).strip() and str(ta).strip().lower() != "nan":
        return str(ta).strip().upper()
    mu = row.get("MATCHUP")
    if isinstance(mu, str):
        s = mu.strip()
        if " vs." in s:
            return s.split(" vs.", 1)[0].strip().upper()
        if " vs " in s:
            return s.split(" vs ", 1)[0].strip().upper()
        if " @ " in s:
            return s.split(" @ ", 1)[0].strip().upper()
    return ""


def format_nba_matchup_line(
    team_abbr: str,
    opponent_abbr: str,
    is_home: bool | None,
) -> str:
    """Sportsbook matchup text for tooltips only (not on the x-axis)."""
    t = (team_abbr or "").strip().upper() or "—"
    o = (opponent_abbr or "").strip().upper() or "—"
    if is_home is True:
        return f"{t} vs {o}"
    if is_home is False:
        return f"{t} @ {o}"
    return f"{t} · {o}"


def format_nba_prop_stat_phrase(stat_label: str, stat_value: Any) -> str:
    """Human phrase for tooltip ``Stat:`` line (``\"24.0 points\"``)."""
    txt = fmt_number(stat_value, decimals=1)
    key = stat_label.strip()
    suffix = _PROP_STAT_PHRASE_SUFFIX.get(key, key.lower())
    return f"{txt} {suffix}"


def nba_game_id_sort_string(game_id: Any) -> str:
    """Stable string key for sorting rows that share the same game date.

    Not intended for on-screen display — only chronological tie-breaks.
    """
    if game_id is None or (isinstance(game_id, float) and pd.isna(game_id)):
        return "0000000000"
    s = str(game_id).strip()
    if not s or s.lower() == "nan":
        return "0000000000"
    if len(s) >= 2 and s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        s = s[:-2]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return "0000000000"
    tail = digits[-10:] if len(digits) >= 10 else digits
    return tail.zfill(10)


def infer_nba_home_from_row(row: Mapping[str, Any]) -> bool | None:
    """Return ``True`` if home, ``False`` if away, ``None`` if unknown."""
    if "IS_HOME" in row:
        try:
            v = row["IS_HOME"]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)) and not pd.isna(v):
                return bool(int(v))
            if isinstance(v, str):
                lower = v.strip().lower()
                if lower in ("true", "1", "t", "yes"):
                    return True
                if lower in ("false", "0", "f", "no"):
                    return False
        except Exception:
            pass
    mu = row.get("MATCHUP") if hasattr(row, "get") else None
    if isinstance(mu, str):
        if " vs." in mu or " vs " in mu:
            return True
        if " @ " in mu:
            return False
    return None


def format_nba_prop_chart_tick(
    *,
    chart_ts: Any,
    opponent_abbr: str,
    is_home: bool | None,
) -> str:
    """Short x-axis label: ``MM/DD vs TOR`` (home) or ``MM/DD @ DET`` (away).

    Preconditions: ``chart_ts`` must be a real (non-NaT) timestamp. Callers must
    drop rows without parseable ``GAME_DATE`` — never substitute placeholders.
    """
    try:
        ts = pd.Timestamp(chart_ts)
    except (ValueError, TypeError) as exc:
        raise ValueError("chart_ts must be a valid NBA game datetime") from exc
    if pd.isna(ts):
        raise ValueError("chart_ts must not be NaT — exclude this row from the chart")

    opp = (opponent_abbr or "").strip()
    md = ts.strftime("%m/%d")
    conn = "@" if is_home is False else "vs"
    if opp:
        return f"{md} {conn} {opp}"
    return md


def dedupe_chart_tick_labels(labels: list[str]) -> list[str]:
    """Append ``(2)``, ``(3)``, … when the same short label repeats."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for lbl in labels:
        n = seen.get(lbl, 0)
        seen[lbl] = n + 1
        out.append(lbl if n == 0 else f"{lbl} ({n + 1})")
    return out


def nba_prop_chart_hit_over(stat_val: Any, prop_line: float, *, is_half_line: bool) -> bool:
    """Same threshold semantics as ``hit_rate`` / Research chart coloring."""
    try:
        v = float(stat_val)
    except (TypeError, ValueError):
        return False
    if math.isnan(v):
        return False
    return v > prop_line if is_half_line else v >= prop_line


def nba_prop_chart_result_label(stat_val: Any, prop_line: float, *, is_half_line: bool) -> str:
    """Hover/text label: ``Over`` or ``Under`` relative to the posted line."""
    try:
        v = float(stat_val)
    except (TypeError, ValueError):
        return "-"
    if math.isnan(v):
        return "-"
    return "Over" if nba_prop_chart_hit_over(v, prop_line, is_half_line=is_half_line) else "Under"


def format_nba_prop_chart_hover_html(
    *,
    date_display: str,
    opponent: str,
    location: str,
    matchup_display: str,
    stat_phrase: str,
    minutes_display: str,
    prop_line: float,
    result_label: str,
) -> str:
    """Structured hover tooltip for the Research bar chart."""
    line_txt = fmt_number(prop_line, decimals=1)
    parts = [
        f"<b>Date:</b> {date_display}",
        f"<b>Opponent:</b> {opponent}",
        f"<b>Location:</b> {location}",
        f"<b>Matchup:</b> {matchup_display}",
        f"<b>Stat:</b> {stat_phrase}",
        f"<b>Minutes:</b> {minutes_display}",
        f"<b>Line:</b> {line_txt}",
        f"<b>Result:</b> {result_label}",
    ]
    return "<br>".join(parts)
