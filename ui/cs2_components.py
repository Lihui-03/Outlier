"""Reusable UI components for the CS2 research page.

All functions render Streamlit widgets.  None of them fetch data — they
accept already-loaded model objects and render them.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

import streamlit as st

from services.cs2.models import (
    CS2MatchResult,
    CS2SourceStatus,
    CS2Team,
    CS2TeamStats,
    CS2Tournament,
)


# ---------------------------------------------------------------------------
# Source status panel
# ---------------------------------------------------------------------------


def cs2_source_status_panel(status: CS2SourceStatus) -> None:
    """Compact provider status panel shown in the CS2 sidebar."""
    with st.expander("CS2 Data Source", expanded=False):
        emoji = status.mode_emoji
        provider_label = status.provider_label

        st.markdown(f"**Source:** {provider_label}")
        st.markdown(f"**Status:** {emoji} {status.mode_label}")

        now_ts = datetime.datetime.now().timestamp()

        if status.provider == "hltv":
            if not status.hltv_bridge_installed:
                st.error(
                    "HLTV bridge not installed. "
                    "Run: `cd services/cs2/hltv_bridge && npm install`"
                )
            if status.last_success_ts:
                age_s = now_ts - status.last_success_ts
                st.markdown(f"**Last HLTV sync:** {_human_duration(age_s)} ago")
            else:
                st.markdown("**Last HLTV sync:** — (session start)")
            if status.hltv_cb_open and status.hltv_cb_open_until:
                remaining = max(0, status.hltv_cb_open_until - now_ts)
                st.warning(
                    f"HLTV paused ({remaining:.0f}s remaining). Serving from cache.",
                    icon="🔴",
                )
            st.markdown(f"**Throttle:** {status.throttle_s:.0f}s between requests")

        elif status.provider == "grid":
            st.caption("GRID is the experimental path (not the default).")
            if not status.grid_configured:
                st.warning(
                    "GRID selected but `GRID_API_KEY` is not set — falling "
                    "back to HLTV / mock. Edit `.env` to continue on GRID.",
                    icon="⚠️",
                )
            else:
                if status.grid_last_success_ts:
                    age_s = now_ts - status.grid_last_success_ts
                    st.markdown(f"**Last GRID sync:** {_human_duration(age_s)} ago")
                else:
                    st.markdown("**Last GRID sync:** — (session start)")
                st.markdown(f"**Throttle:** {status.throttle_s:.0f}s between requests")

            if status.hltv_enabled:
                _status_hltv_enrichment_row(status, now_ts)

        elif status.provider == "mock":
            st.info(
                "Running on **demo data**. Install the HLTV bridge for "
                "live data: `cd services/cs2/hltv_bridge && npm install`.",
                icon="🎮",
            )

        cs = _cache_stats_from_status()
        if cs:
            st.caption(
                f"Cache: {cs.get('entry_count', 0)} entries "
                f"({cs.get('total_kb', 0):.1f} KB)"
            )


def _status_hltv_enrichment_row(status: CS2SourceStatus, now_ts: float) -> None:
    if not status.hltv_bridge_installed:
        st.caption(
            "HLTV enrichment: bridge not installed. "
            "Run `npm install` in services/cs2/hltv_bridge/."
        )
        return
    if status.hltv_last_success_ts:
        age_s = now_ts - status.hltv_last_success_ts
        st.caption(f"HLTV enrichment: active (last sync {_human_duration(age_s)} ago)")
    else:
        st.caption("HLTV enrichment: enabled (no call yet this session)")


def _cache_stats_from_status() -> dict:
    """Safely pull cache stats."""
    try:
        from services.cs2.cs2_cache_service import cache_stats  # noqa: PLC0415
        return cache_stats()
    except Exception:
        return {}


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------------------
# Stale / cooldown banners
# ---------------------------------------------------------------------------


def cs2_stale_banner(label: str = "match results", source: str = "") -> None:
    """Show a compact info notice when data is from stale cache."""
    src_note = f" ({source})" if source else ""
    st.info(
        f"Showing stale disk cache{src_note} — live refresh of {label} failed. "
        "Data may be a few hours old.",
        icon="🟠",
    )


def cs2_cooldown_banner(remaining_s: float | None = None) -> None:
    """Show a warning when the HLTV circuit breaker is open."""
    msg = "HLTV requests are temporarily paused to avoid rate-limiting."
    if remaining_s and remaining_s > 0:
        msg += f" Resuming in ~{remaining_s:.0f}s."
    msg += " Serving cached data where available."
    st.warning(msg, icon="🔴")


def cs2_mock_banner() -> None:
    """Show a notice that demo/mock data is in use."""
    st.info(
        "Showing **demo data** (live provider unavailable or "
        "`CS2_USE_MOCK_FALLBACK=true`).  Install the HLTV bridge "
        "(`cd services/cs2/hltv_bridge && npm install`) to enable live data.",
        icon="🎮",
    )


def cs2_grid_no_key_banner() -> None:
    """Show a notice when the experimental GRID path is selected without a key."""
    st.warning(
        "**GRID is selected but `GRID_API_KEY` is not set.**  \n"
        "GRID is the experimental path; the default is HLTV.  \n"
        "- To continue on GRID: add `GRID_API_KEY=…` to `.env` "
        "(register a free Open Access key at [grid.gg](https://www.grid.gg/)).  \n"
        "- To switch back to HLTV (recommended): set `CS2_PROVIDER=hltv` in `.env`.",
        icon="🔑",
    )


def cs2_source_badge(source: str) -> None:
    """Render a small 'Source: X' caption, e.g. after a data section."""
    labels = {
        "hltv":  "Source: HLTV (gigobyte/hltv)",
        "grid":  "Source: GRID Open Access",
        "mixed": "Source: HLTV + GRID",
        "mock":  "Source: demo data",
    }
    label = labels.get(source.lower(), f"Source: {source}")
    st.caption(f"<small style='color:#777'>{label}</small>", unsafe_allow_html=True)


def cs2_cloudflare_notice() -> None:
    """Explain Cloudflare blocking (HLTV path only)."""
    st.warning(
        "HLTV blocked the request via Cloudflare. "
        "More likely on data-center IPs than home connections. "
        "Stale cached data is shown where available.",
        icon="🛡️",
    )


def cs2_capability_notice(key: str) -> None:
    """Render the unsupported-capability notice for a feature."""
    from services.cs2.cs2_capabilities import notice  # noqa: PLC0415
    st.info(notice(key), icon="ℹ️")


# ---------------------------------------------------------------------------
# Match result cards
# ---------------------------------------------------------------------------


def cs2_match_result_card(r: CS2MatchResult, compact: bool = False) -> None:
    """Render a single match result as a styled card row."""
    t1 = (r.team1.name if r.team1 else None) or "TBD"
    t2 = (r.team2.name if r.team2 else None) or "TBD"
    s1 = r.score_t1
    s2 = r.score_t2
    fmt = r.format or ""
    event = r.event_name or ""
    date_str = _date_str(r.date_unix_ms)

    if s1 is not None and s2 is not None:
        score = f"**{s1}** – **{s2}**" if s1 > s2 else f"{s1} – {s2}" if s1 < s2 else f"{s1} – {s2}"
        label = f"{t1}  {score}  {t2}"
    else:
        label = f"{t1}  vs  {t2}"

    if compact:
        st.markdown(
            f"<small style='color:#aaa'>{date_str}</small> &nbsp; "
            f"{label} &nbsp; <small style='color:#888'>({fmt}) {event}</small>",
            unsafe_allow_html=True,
        )
    else:
        cols = st.columns([2, 1, 2, 1, 2])
        with cols[0]:
            w1 = (s1 is not None and s2 is not None and s1 > s2)
            st.markdown(f"{'**' if w1 else ''}{t1}{'**' if w1 else ''}")
        with cols[1]:
            st.markdown(f"`{s1 if s1 is not None else '?'} – {s2 if s2 is not None else '?'}`")
        with cols[2]:
            w2 = (s1 is not None and s2 is not None and s2 > s1)
            st.markdown(f"{'**' if w2 else ''}{t2}{'**' if w2 else ''}")
        with cols[3]:
            st.caption(fmt)
        with cols[4]:
            st.caption(f"{date_str}  {event}")

    if r._stale:
        st.caption("🟠 stale cache")


def cs2_stars(n: int) -> str:
    return "★" * min(n, 5) + "☆" * max(0, 5 - n)


def _date_str(unix_ms: int | None) -> str:
    if unix_ms is None:
        return "—"
    try:
        dt = datetime.datetime.utcfromtimestamp(unix_ms / 1000)
        return dt.strftime("%b %d")
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# Player stat cards
# ---------------------------------------------------------------------------


def cs2_tournament_table(tournaments: list[CS2Tournament]) -> None:
    """Render a table of tournaments from the GRID API."""
    if not tournaments:
        cs2_no_data("No tournaments found. GRID may not have data for this query.")
        return

    import pandas as pd

    rows = []
    for t in tournaments:
        rows.append({
            "Tournament": t.name or "—",
            "Start":      t.start_date[:10] if t.start_date else "—",
            "End":        t.end_date[:10] if t.end_date else "—",
            "Prize Pool": t.prize_pool or "—",
            "Location":   ", ".join(filter(None, [t.city, t.country])) or "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    stale_any = any(t._stale for t in tournaments)
    if stale_any:
        st.caption("🟠 stale cache")
    else:
        cs2_source_badge("grid")


def cs2_player_stat_cards(summary: dict) -> None:
    """Render a row of stat metric cards from build_player_summary()."""
    if not summary.get("has_stats"):
        st.info("No stats loaded yet.  Click **Load stats** to fetch data.")
        return

    metrics = [
        ("K/D",        summary.get("kd_ratio"),          "{:.2f}"),
        ("ADR",        summary.get("adr"),                "{:.1f}"),
        ("HS%",        summary.get("hs_pct"),             "{:.1f}%"),
        ("Rating",     summary.get("rating"),             "{:.2f}"),
        ("KPR",        summary.get("kills_per_round"),    "{:.2f}"),
        ("Open KD",    summary.get("opening_kill_ratio"), "{:.2f}"),
    ]

    cols = st.columns(len(metrics))
    for col, (label, value, fmt) in zip(cols, metrics):
        with col:
            if value is not None and not (isinstance(value, float) and math.isnan(value)):
                st.metric(label, fmt.format(value))
            else:
                st.metric(label, "—")

    if summary.get("maps_played"):
        src = summary.get("_source", "hltv")
        stale_note = " — ⚠️ stale cache" if summary.get("_stale") else ""
        src_labels = {"hltv": "HLTV", "grid": "GRID", "mixed": "HLTV+GRID", "mock": "demo"}
        st.caption(
            f"{summary['maps_played']} maps played "
            f"({src_labels.get(src, src)})"
            + stale_note
        )


def cs2_player_map_history_table(recent_maps: list[Any]) -> None:
    """Render per-map history table."""
    if not recent_maps:
        st.info("No per-map history available.")
        return

    import pandas as pd

    rows = []
    for m in recent_maps:
        rows.append({
            "Date":   _date_str(m.date_unix_ms),
            "Map":    m.map_name or "—",
            "K":      m.kills if m.kills is not None else "—",
            "D":      m.deaths if m.deaths is not None else "—",
            "K/D":    f"{m.kills / m.deaths:.2f}" if (m.kills and m.deaths) else "—",
            "Rating": f"{m.rating:.2f}" if m.rating else "—",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Team cards
# ---------------------------------------------------------------------------


def cs2_team_roster_card(team: CS2Team) -> None:
    """Render a team's roster list."""
    players = team.players or []
    if not players:
        st.caption("Roster not available.")
        return
    for p in players:
        st.markdown(f"• **{p.ign or p.name or '—'}**")


def cs2_team_map_pool_table(stats: CS2TeamStats) -> None:
    """Render team's map pool as a coloured DataFrame."""
    from services.cs2.cs2_team_service import map_pool_table
    import pandas as pd

    rows = map_pool_table(stats)
    if not rows:
        st.info("Map pool data not available.")
        return

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    if stats._stale:
        st.caption("🟠 stale cache")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def cs2_section_header(title: str, subtitle: str = "") -> None:
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


def cs2_no_data(message: str = "No data available.") -> None:
    """Show a clean 'no data' message without broken widgets."""
    st.markdown(
        f"<div style='padding:1rem;background:#1e1e2e;border-radius:8px;"
        f"border-left:4px solid #555;color:#aaa'>{message}</div>",
        unsafe_allow_html=True,
    )
