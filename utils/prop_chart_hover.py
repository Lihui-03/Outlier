"""Player prop Research bar-chart hover text for Plotly (``ui/graph.py``).

Plotly.js hover labels **do not render arbitrary HTML**. Only a small subset is
interpreted (e.g. ``<b>``, ``<i>``, ``<br>``, ``<sup>``). Tags like
``<table>``, ``<div>``, ``<span style=…>`` are shown as **literal text** — the
usual cause of “raw HTML spilled across the chart”.

We therefore:

- Build a **structured payload** per bar from the game row
  (points / rebounds / assists / future markets).
- **Render** Plotly-safe hover strings (e.g. ``render_points_plotly_hover``,
  ``render_rebounds_plotly_hover``) — only supported tags plus escaped plain text.

``row`` must be the same **Series**/**mapping** as the plotted bar’s row.
"""

from __future__ import annotations

import html
import math
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from utils.formatting import (
    fmt_number,
    fmt_signed,
    format_nba_prop_chart_hover_html,
    format_nba_prop_stat_phrase,
    nba_prop_chart_result_label,
)

from services.research_tooltip_enrichment import (
    TOOLTIP_DNP_FG3M,
    TOOLTIP_PASSES_MADE,
    TOOLTIP_POTENTIAL_AST,
    TOOLTIP_Q1_PF,
    TOOLTIP_REB_CHANCES_TOTAL,
    TOOLTIP_TEAM_MARGIN,
)

# Prefer tooltip enrichment column, then any column merged onto the game log.
REB_CHANCES_TOTAL_CANDIDATES: tuple[str, ...] = (
    TOOLTIP_REB_CHANCES_TOTAL,
    "REB_CHANCES_TOTAL",
    "REBOUND_CHANCES_TOTAL",
    "REB_CHANCES",
)

PASSES_MADE_CANDIDATES: tuple[str, ...] = (
    TOOLTIP_PASSES_MADE,
    "PASSES_MADE",
    "PASSES",
)

POTENTIAL_AST_CANDIDATES: tuple[str, ...] = (
    TOOLTIP_POTENTIAL_AST,
    "POTENTIAL_AST",
    "POTENTIAL_ASSISTS",
)

RichHoverBuilder = Callable[..., str]


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


# Non-breaking space: pads do not collapse in HTML; use for column alignment.
_NBSP = "\u00a0"
_MIDDOT = "\u00b7"  # · (vs line separator)


def _two_col_stat_row(
    label: str,
    value: str,
    *,
    label_col_w: int,
    value_col_w: int,
) -> str:
    """Label flush left; value right-aligned in a column of width ``value_col_w``."""
    lab = label.strip()
    val_raw = str(value)
    val_esc = _esc(val_raw)
    pad_after_label = max(0, label_col_w - len(lab))
    pad_before_value = max(0, value_col_w - len(val_raw))
    gap = _NBSP * 3
    return f"{lab}{_NBSP * pad_after_label}{gap}{_NBSP * pad_before_value}{val_esc}"


def _section_rule(*, width: int = 22) -> str:
    """Subtle horizontal rule (Plotly-safe, monospace-friendly)."""
    return "─" * width


def _row_float(row: Mapping[str, Any], key: str) -> float | None:
    if key not in row:
        return None
    v = row.get(key)
    if v is None:
        return None
    if isinstance(v, (float, int)) and pd.isna(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _wl_token_from_row(row: Mapping[str, Any]) -> str | None:
    if "WL" not in row:
        return None
    raw = row.get("WL")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    h = s[0]
    return h if h in ("W", "L") else None


def _minutes_prime(min_val: Any) -> str:
    if min_val is None or (isinstance(min_val, float) and pd.isna(min_val)):
        return "—"
    try:
        f = float(min_val)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(f):
        return "—"
    return f"{int(math.floor(f))}"


def _minutes_prime_tick(min_val: Any) -> str:
    """Minutes with prime suffix (e.g. ``36'``) for 3PM-style tooltips."""
    base = _minutes_prime(min_val)
    return "—" if base == "—" else f"{base}'"


def _abbr_player_name(full: str) -> str:
    """``M. MCBRIDE`` / ``K. MCCULLAR JR.`` style from a roster full name."""
    parts = [p for p in str(full or "").strip().split() if p]
    if not parts:
        return "—"
    suffix_tokens: list[str] = []
    while len(parts) >= 2 and parts[-1].upper().replace(".", "") in (
        "JR", "SR", "II", "III", "IV", "V",
    ):
        suffix_tokens.insert(0, parts.pop().upper())
    if len(parts) >= 2:
        base = f"{parts[0][0]}. {parts[-1].upper()}"
        if suffix_tokens:
            return f"{base} {' '.join(suffix_tokens)}"
        return base
    if parts:
        return parts[0].upper()
    return "—"


def _dnp_fg3m_entries_from_row(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse enriched DNP teammate list from a chart row."""
    if TOOLTIP_DNP_FG3M not in row:
        return []
    raw = row.get(TOOLTIP_DNP_FG3M)
    if raw is None:
        return []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [e for e in parsed if isinstance(e, dict)]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    try:
        if pd.isna(raw):
            return []
    except (TypeError, ValueError):
        pass
    return []


def _date_vs_opponent_line(
    chart_ts: Any,
    opponent_abbr: str,
    *,
    location: str = "",
) -> str:
    """Header date line: ``02/24/26 @ CLE`` when away, ``vs`` when home."""
    try:
        ts = pd.Timestamp(chart_ts)
    except (TypeError, ValueError):
        ts = pd.NaT
    if pd.isna(ts):
        date_s = "Unknown"
    else:
        date_s = ts.strftime("%m/%d/%y")

    opp = (opponent_abbr or "").strip().upper()
    if not opp:
        return date_s
    loc = (location or "").strip().lower()
    if loc == "away":
        return f"{date_s} @ {opp}"
    if loc == "home":
        return f"{date_s} vs {opp}"
    return f"{date_s} vs {opp}"


def _makes_att_pct(
    row: Mapping[str, Any],
    made_key: str,
    att_key: str,
    pct_key: str | None,
) -> str | None:
    ma = _row_float(row, made_key)
    aa = _row_float(row, att_key)
    if ma is None and aa is None:
        return None
    m = int(round(ma)) if ma is not None else None
    a = int(round(aa)) if aa is not None else None
    if m is None or a is None:
        return f"{m if m is not None else '—'}/{a if a is not None else '—'}"
    pct: float | None = None
    if pct_key and pct_key in row:
        p_raw = _row_float(row, pct_key)
        if p_raw is not None:
            pct = p_raw * 100.0 if p_raw <= 1.5 else p_raw
    if pct is None and a > 0:
        pct = 100.0 * m / a
    if pct is None:
        return f"{m}/{a}"
    return f"{m}/{a}({pct:.0f}%)"


def _optional_int_row(row: Mapping[str, Any], key: str) -> int | None:
    """Integer field from a row; ``None`` if missing or NaN."""
    if key not in row:
        return None
    v = row.get(key)
    if v is None or pd.isna(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return int(round(f))


def _format_stat_cell_value(stat_value: Any) -> str:
    """Primary stat for the bar (PTS, REB, …) as shown in the stats block."""
    try:
        pv = float(stat_value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(pv):
        return "—"
    if abs(pv - round(pv)) < 1e-9:
        return str(int(round(pv)))
    return f"{pv:.1f}"


def _int_cell_from_row(row: Mapping[str, Any], key: str) -> str:
    v = _optional_int_row(row, key)
    return str(v) if v is not None else "—"


def _rebound_chances_display(row: Mapping[str, Any]) -> str:
    """Total rebound chances when a tracking column is present on the row."""
    for key in REB_CHANCES_TOTAL_CANDIDATES:
        if key not in row:
            continue
        v = _optional_int_row(row, key)
        if v is not None:
            return str(v)
    return "—"


def _passes_made_display(row: Mapping[str, Any]) -> str:
    """Passes made (player tracking) when present."""
    for key in PASSES_MADE_CANDIDATES:
        if key not in row:
            continue
        v = _optional_int_row(row, key)
        if v is not None:
            return str(v)
    return "—"


def _potential_ast_display(row: Mapping[str, Any]) -> str:
    """Potential assists when the league provides a column (often via enrichment)."""
    for key in POTENTIAL_AST_CANDIDATES:
        if key not in row:
            continue
        v = _optional_int_row(row, key)
        if v is not None:
            return str(v)
    return "—"


def _base_research_tooltip_fields(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
) -> dict[str, Any]:
    """Shared header/stats envelope for Points, Rebounds, Assists, and similar markets."""
    try:
        ts = pd.Timestamp(chart_ts)
    except (TypeError, ValueError):
        ts = pd.NaT
    if pd.isna(ts):
        date_s = "Unknown"
    else:
        date_s = ts.strftime("%m/%d/%y")

    opp = (opponent_abbr or "").strip().upper()
    date_vs_opp = f"{date_s} vs {opp}" if opp else f"{date_s} vs Unknown"

    team_margin = _optional_int_row(row, TOOLTIP_TEAM_MARGIN)
    q1_pf = _optional_int_row(row, TOOLTIP_Q1_PF)
    wl = _wl_token_from_row(row)
    pm = _row_float(row, "PLUS_MINUS")
    pm_s = fmt_signed(pm, decimals=0, default="") if pm is not None else ""

    line_txt = fmt_number(prop_line, decimals=1)
    ou = nba_prop_chart_result_label(stat_value, prop_line, is_half_line=is_half_line)

    return {
        "date_vs_opp": date_vs_opp,
        "matchup": (matchup_display or "").strip() or None,
        "team_margin": team_margin,
        "wl": wl,
        "on_court_pm": pm_s,
        "has_on_court_pm": bool(pm_s),
        "fouls_q1": q1_pf,
        "prop_line": line_txt,
        "over_under": ou,
        "minutes": _minutes_prime(row.get("MIN")),
        "fouls_game": (
            str(int(round(pf)))
            if (pf := _row_float(row, "PF")) is not None
            else "—"
        ),
    }


def build_points_tooltip_data(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
) -> dict[str, Any]:
    """Structured tooltip fields for the **Points** market (one game row)."""
    base = _base_research_tooltip_fields(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return {
        **base,
        "market": "Points",
        "points": _format_stat_cell_value(stat_value),
        "ft_made": _makes_att_pct(row, "FTM", "FTA", "FT_PCT"),
        "fg3_made": _makes_att_pct(row, "FG3M", "FG3A", "FG3_PCT"),
        "fg_made": _makes_att_pct(row, "FGM", "FGA", "FG_PCT"),
    }


def build_assists_tooltip_data(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
) -> dict[str, Any]:
    """Structured tooltip fields for the **Assists** market (one game row)."""
    base = _base_research_tooltip_fields(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return {
        **base,
        "market": "Assists",
        "assists": _format_stat_cell_value(stat_value),
        "potential_ast": _potential_ast_display(row),
        "passes_made": _passes_made_display(row),
    }


def build_threes_tooltip_data(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
    location: str = "",
) -> dict[str, Any]:
    """Structured tooltip fields for the **3PM** market (one game row)."""
    base = _base_research_tooltip_fields(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    base["date_vs_opp"] = _date_vs_opponent_line(
        chart_ts, opponent_abbr, location=location,
    )
    base["minutes"] = _minutes_prime_tick(row.get("MIN"))
    pts = _row_float(row, "PTS")
    return {
        **base,
        "market": "3PM",
        "show_close_line": True,
        "points": str(int(round(pts))) if pts is not None else "—",
        "threes": _format_stat_cell_value(stat_value),
        "ft_made": _makes_att_pct(row, "FTM", "FTA", "FT_PCT"),
        "fg3_made": _makes_att_pct(row, "FG3M", "FG3A", "FG3_PCT"),
        "fg_made": _makes_att_pct(row, "FGM", "FGA", "FG_PCT"),
        "dnp_teammates": _dnp_fg3m_entries_from_row(row),
    }


def build_rebounds_tooltip_data(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
) -> dict[str, Any]:
    """Structured tooltip fields for the **Rebounds** market (one game row)."""
    base = _base_research_tooltip_fields(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return {
        **base,
        "market": "Rebounds",
        "rebounds": _format_stat_cell_value(stat_value),
        "rebound_chances": _rebound_chances_display(row),
        "oreb": _int_cell_from_row(row, "OREB"),
        "dreb": _int_cell_from_row(row, "DREB"),
    }


def _render_research_plotly_core(
    data: Mapping[str, Any],
    stat_pairs: list[tuple[str, str]],
) -> str:
    """Shared layout: title, margin, vs line, rule, two-column stats, optional PM footnote."""
    parts: list[str] = []

    title = _esc(str(data.get("date_vs_opp", "")))
    parts.append(f"<b>{title}</b>")
    mu = data.get("matchup")
    if mu:
        parts.append(f"<i>{_esc(str(mu))}</i>")

    parts.append("")

    tm = data.get("team_margin")
    if tm is not None:
        tmi = int(tm)
        if tmi > 0:
            parts.append(f"<b>Won by {tmi}</b>")
        elif tmi < 0:
            parts.append(f"<b>Lost by {abs(tmi)}</b>")
        else:
            parts.append("<b>Tie</b>")
    else:
        wl = data.get("wl")
        pm_note = (data.get("on_court_pm") or "").strip()
        if wl == "W":
            parts.append(
                "<b>Won by </b>"
                + (f" {_esc(pm_note)}" if pm_note else "")
            )
        elif wl == "L":
            parts.append(
                "<b>Lost by </b>"
                + (f" {_esc(pm_note)}" if pm_note else "")
            )
        elif pm_note:
            parts.append(f"+/− {_esc(pm_note)} <i>(on-court)</i>")
        else:
            parts.append("—")

    parts.append("")

    pl = _esc(str(data.get("prop_line", "—")))
    ou = _esc(str(data.get("over_under", "—")))
    if data.get("show_close_line"):
        ou_letter = "O" if ou == "Over" else ("U" if ou == "Under" else "—")
        parts.append(
            f"<b>CL {pl}</b>{_NBSP}{_MIDDOT}{_NBSP}"
            f"<b>{ou_letter}</b>{_NBSP}{_MIDDOT}{_NBSP}<b>{ou}</b>"
        )
    else:
        parts.append(
            f"<i>vs line</i>{_NBSP}{_MIDDOT}{_NBSP}<b>{pl}</b>"
            f"{_NBSP}{_MIDDOT}{_NBSP}<b>{ou}</b>"
        )

    parts.append("")
    parts.append(_section_rule(width=24))
    parts.append("")

    label_col_w = max((len(a) for a, _ in stat_pairs), default=0)
    value_col_w = max((len(str(b)) for _, b in stat_pairs), default=0)
    stat_lines = [
        _two_col_stat_row(lab, val, label_col_w=label_col_w, value_col_w=value_col_w)
        for lab, val in stat_pairs
    ]
    parts.append("<br>".join(stat_lines))

    if data.get("team_margin") is None and data.get("has_on_court_pm"):
        parts.append("")
        parts.append("<i>Final margin unavailable; showing on-court +/−.</i>")

    dnp_entries = data.get("dnp_teammates")
    if isinstance(dnp_entries, list) and dnp_entries:
        parts.append("")
        parts.append(_section_rule(width=24))
        parts.append("")
        parts.append(_esc("DID NOT PLAY [AVG 3PT]"))
        dnp_lines: list[str] = []
        label_w = 16
        val_w = 6
        for entry in dnp_entries:
            if not isinstance(entry, dict):
                continue
            name = _abbr_player_name(str(entry.get("name", "")))
            avg_raw = entry.get("avg")
            avg_s = "(—)"
            if avg_raw is not None:
                try:
                    if not pd.isna(avg_raw):
                        avg_s = f"({float(avg_raw):.1f})"
                except (TypeError, ValueError):
                    pass
            dnp_lines.append(
                _two_col_stat_row(name, avg_s, label_col_w=label_w, value_col_w=val_w),
            )
        if dnp_lines:
            parts.append("<br>".join(dnp_lines))

    return "<br>".join(parts)


def render_points_plotly_hover(data: Mapping[str, Any]) -> str:
    """Turn structured Points data into Plotly-safe hover text (no table/div)."""
    foul_fgame = str(data.get("fouls_game", "—"))
    fq = data.get("fouls_q1")
    fq_txt = str(int(fq)) if fq is not None else "—"
    foul_val = f"{foul_fgame}({fq_txt})"

    shoot_pairs: list[tuple[str, str]] = []
    if data.get("ft_made"):
        shoot_pairs.append(("FT Made", str(data["ft_made"])))
    if data.get("fg3_made"):
        shoot_pairs.append(("3PT Made", str(data["fg3_made"])))
    if data.get("fg_made"):
        shoot_pairs.append(("FG Made", str(data["fg_made"])))

    core_pairs: list[tuple[str, str]] = [
        ("Minutes", str(data.get("minutes", "—"))),
        ("Points", str(data.get("points", "—"))),
    ]
    all_pairs: list[tuple[str, str]] = (
        core_pairs + shoot_pairs + [("Fouls (1Q)", foul_val)]
    )
    return _render_research_plotly_core(data, all_pairs)


def render_assists_plotly_hover(data: Mapping[str, Any]) -> str:
    """Turn structured Assists data into Plotly-safe hover text."""
    foul_fgame = str(data.get("fouls_game", "—"))
    fq = data.get("fouls_q1")
    fq_txt = str(int(fq)) if fq is not None else "—"
    foul_val = f"{foul_fgame}({fq_txt})"

    all_pairs: list[tuple[str, str]] = [
        ("Minutes", str(data.get("minutes", "—"))),
        ("Assists", str(data.get("assists", "—"))),
        ("Potential AST", str(data.get("potential_ast", "—"))),
        ("Passes Made", str(data.get("passes_made", "—"))),
        ("Fouls (1Q)", foul_val),
    ]
    return _render_research_plotly_core(data, all_pairs)


def render_rebounds_plotly_hover(data: Mapping[str, Any]) -> str:
    """Turn structured Rebounds data into Plotly-safe hover text (matches Points layout)."""
    foul_fgame = str(data.get("fouls_game", "—"))
    fq = data.get("fouls_q1")
    fq_txt = str(int(fq)) if fq is not None else "—"
    foul_val = f"{foul_fgame}({fq_txt})"

    all_pairs: list[tuple[str, str]] = [
        ("Minutes", str(data.get("minutes", "—"))),
        ("Rebounds", str(data.get("rebounds", "—"))),
        ("Rebound Chances", str(data.get("rebound_chances", "—"))),
        ("OREB", str(data.get("oreb", "—"))),
        ("DREB", str(data.get("dreb", "—"))),
        ("Fouls (1Q)", foul_val),
    ]
    return _render_research_plotly_core(data, all_pairs)


def render_threes_plotly_hover(data: Mapping[str, Any]) -> str:
    """Turn structured 3PM data into Plotly-safe hover text (Points-style + DNP block)."""
    foul_fgame = str(data.get("fouls_game", "—"))
    fq = data.get("fouls_q1")
    fq_txt = str(int(fq)) if fq is not None else "—"
    foul_val = f"{foul_fgame}({fq_txt})"

    shoot_pairs: list[tuple[str, str]] = []
    if data.get("ft_made"):
        shoot_pairs.append(("FT Made", str(data["ft_made"])))
    if data.get("fg3_made"):
        shoot_pairs.append(("3PT Made", str(data["fg3_made"])))
    elif data.get("threes"):
        shoot_pairs.append(("3PT Made", str(data["threes"])))
    if data.get("fg_made"):
        shoot_pairs.append(("FG Made", str(data["fg_made"])))

    core_pairs: list[tuple[str, str]] = [
        ("Minutes", str(data.get("minutes", "—"))),
        ("Points", str(data.get("points", "—"))),
    ]
    all_pairs: list[tuple[str, str]] = (
        core_pairs + shoot_pairs + [("Fouls (1Q)", foul_val)]
    )
    return _render_research_plotly_core(data, all_pairs)


def _format_points_research_hover_plotly(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
    location: str = "",
) -> str:
    data = build_points_tooltip_data(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return render_points_plotly_hover(data)


def _format_rebounds_research_hover_plotly(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
    location: str = "",
) -> str:
    data = build_rebounds_tooltip_data(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return render_rebounds_plotly_hover(data)


def _format_assists_research_hover_plotly(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
    location: str = "",
) -> str:
    data = build_assists_tooltip_data(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
    )
    return render_assists_plotly_hover(data)


def _format_threes_research_hover_plotly(
    row: Mapping[str, Any],
    *,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent_abbr: str,
    matchup_display: str,
    location: str = "",
) -> str:
    data = build_threes_tooltip_data(
        row,
        stat_value=stat_value,
        prop_line=prop_line,
        is_half_line=is_half_line,
        chart_ts=chart_ts,
        opponent_abbr=opponent_abbr,
        matchup_display=matchup_display,
        location=location,
    )
    return render_threes_plotly_hover(data)


# Register Plotly-safe builders (same kwargs for each market).
_RICH_RESEARCH_HOVER_BUILDERS: dict[tuple[str, str], RichHoverBuilder] = {
    ("Points", "PTS"): _format_points_research_hover_plotly,
    ("Rebounds", "REB"): _format_rebounds_research_hover_plotly,
    ("Assists", "AST"): _format_assists_research_hover_plotly,
    ("3PM", "FG3M"): _format_threes_research_hover_plotly,
}


def format_prop_research_chart_hover_html(
    row: Mapping[str, Any],
    *,
    stat_label: str,
    stat_col: str,
    stat_value: Any,
    prop_line: float,
    is_half_line: bool,
    chart_ts: Any,
    opponent: str,
    location: str,
    matchup_display: str,
    minutes_display: str,
) -> str:
    """Return Plotly hover text: structured **Points / Rebounds / Assists** payload; else legacy HTML lines.

    Legacy path uses only ``<b>`` + ``<br>`` (supported by Plotly).
    ``row`` must match the bar’s game row.
    """
    key = (stat_label.strip(), stat_col.strip().upper())
    builder = _RICH_RESEARCH_HOVER_BUILDERS.get(key)
    if builder is not None:
        return builder(
            row,
            stat_value=stat_value,
            prop_line=prop_line,
            is_half_line=is_half_line,
            chart_ts=chart_ts,
            opponent_abbr=opponent,
            matchup_display=matchup_display,
            location=location,
        )

    try:
        ts = pd.Timestamp(chart_ts)
    except (TypeError, ValueError):
        ts = pd.NaT
    date_iso = "Unknown" if pd.isna(ts) else ts.strftime("%Y-%m-%d")
    stat_phrase = format_nba_prop_stat_phrase(stat_label, stat_value)
    result_label = nba_prop_chart_result_label(
        stat_value, prop_line, is_half_line=is_half_line,
    )
    return format_nba_prop_chart_hover_html(
        date_display=date_iso,
        opponent=opponent or "—",
        location=location,
        matchup_display=matchup_display,
        stat_phrase=stat_phrase,
        minutes_display=minutes_display,
        prop_line=prop_line,
        result_label=result_label,
    )
