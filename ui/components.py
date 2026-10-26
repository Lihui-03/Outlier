"""Reusable presentation primitives.

Everything here is a pure-rendering helper built on top of `st.markdown`
or `st.button`. They don't touch session state directly; their callers
do. That keeps this module drop-in reusable.

The two kinds of helpers:
    * HTML helpers (`chip_html`, `rank_badge_html`, ...) that return
      strings and are meant to be composed inside an `st.markdown(...)`
      call so you can have multiple chips on one line.
    * Widget helpers (`toggle_chip`, `segmented`, `stat_card`,
      `teammate_card`) that render via Streamlit widgets and support
      user interaction.
"""

from __future__ import annotations

import html
import math
from typing import Any, Iterable, Sequence

import streamlit as st

from utils.formatting import fmt_number, fmt_signed


# ---------------------------------------------------------------------------
# HTML-only helpers
# ---------------------------------------------------------------------------


def chip_html(label: str, on: bool = False, beta: bool = False,
              badge: str | None = None) -> str:
    """Return an <span> chip. Use inside st.markdown(..., unsafe_allow_html=True)."""
    classes = ["pp-chip"]
    if on:
        classes.append("on")
    if beta:
        classes.append("beta")
    cls = " ".join(classes)
    safe_label = html.escape(str(label))
    badge_html = (
        f'<span class="pp-chip-badge">{html.escape(str(badge))}</span>'
        if badge else ""
    )
    return f'<span class="{cls}">{safe_label}{badge_html}</span>'


def rank_badge_html(rank: int | None, out_of: int = 30) -> str:
    """Return a coloured rank badge HTML string.

    Rank semantics: 1 = team allows the most of this stat (favourable for
    the offensive player), 30 = allows the least (tough matchup). We map
    that to green (good) / red (bad) / grey (middle).
    """
    if rank is None or (isinstance(rank, float) and math.isnan(rank)):
        return ""
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return ""
    # Lower ranks = more favourable. Top third green, bottom third red.
    third = max(1, out_of // 3)
    if r <= third:
        cls = "good"
    elif r > out_of - third:
        cls = "bad"
    else:
        cls = "mid"
    return f'<span class="pp-rank {cls}">#{r}/{out_of}</span>'


def active_filter_chip_strip_html(active: list[tuple[str, str]]) -> str:
    """Given [(key, label), ...] return a <div> of chips. Use inside st.markdown."""
    if not active:
        return '<div class="pp-section">No filters active.</div>'
    pieces = [chip_html(label, on=True) for _, label in active]
    return '<div style="margin-bottom:6px">' + "".join(pieces) + "</div>"


# ---------------------------------------------------------------------------
# Widget helpers (accept click interactions)
# ---------------------------------------------------------------------------


def toggle_chip(label: str, key: str, *, on: bool, beta: bool = False,
                badge: str | None = None, help: str | None = None) -> bool:
    """A tiny clickable "chip" built from st.button.

    Because Streamlit doesn't style individual buttons, we render the
    chip HTML next to a near-invisible button. The button is what drives
    state; the HTML is what the user sees. Returns True when clicked
    this rerun.
    """
    # Unicode dot indicates ON state so colour-blind users still see a
    # difference even if CSS doesn't apply for some reason.
    prefix = "●  " if on else "○  "
    beta_suffix = "  · beta" if beta else ""
    badge_suffix = f"  [{badge}]" if badge else ""
    full = f"{prefix}{label}{badge_suffix}{beta_suffix}"
    # Streamlit button accepts `disabled`; we keep beta chips clickable
    # so the user can read the tooltip but it's a no-op inside the filter
    # service (enforced there).
    return st.button(full, key=key, help=help, use_container_width=True)


def segmented(label: str, options: Sequence[str], *, key: str,
              default: str | None = None) -> str:
    """Single-select segmented control. Uses st.radio horizontal."""
    default = default if default in options else options[0]
    idx = list(options).index(default)
    return st.radio(label, list(options), index=idx, key=key, horizontal=True)


def stat_card(title: str, value: Any, diff: Any = None, *,
              value_decimals: int = 1, diff_decimals: int = 2) -> None:
    """Render a card with a big value and an optional signed diff."""
    val_text = fmt_number(value, decimals=value_decimals)
    if diff is None or (isinstance(diff, float) and math.isnan(diff)):
        diff_html = '<div class="pp-card-diff neu">no comparison sample</div>'
    else:
        cls = "pos" if float(diff) > 0 else ("neg" if float(diff) < 0 else "neu")
        diff_html = (
            f'<div class="pp-card-diff {cls}">'
            f'{fmt_signed(diff, decimals=diff_decimals)} vs excluded'
            '</div>'
        )
    st.markdown(
        f"""
        <div class="pp-card">
            <div class="pp-card-title">{html.escape(title)}</div>
            <div class="pp-card-value">{html.escape(val_text)}</div>
            {diff_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def teammate_card(name: str, *, diff: float | None, availability: str | None,
                  state: str, on_toggle_key: str) -> str | None:
    """Render a teammate card. Returns the new state if the user clicked a
    WITH/WITHOUT action this rerun, otherwise None.

    `state` is one of "off", "with", "without".
    """
    safe_name = html.escape(name)
    avail_html = (
        f'<div class="pp-teammate-sub">{html.escape(availability)}</div>'
        if availability else ""
    )
    if diff is None:
        diff_cls, diff_text = "neu", "diff: -"
    else:
        diff_cls = "pos" if diff > 0 else ("neg" if diff < 0 else "neu")
        diff_text = f"Δ {fmt_signed(diff, decimals=2)}"

    st.markdown(
        f"""
        <div class="pp-teammate">
            <div class="pp-teammate-name">{safe_name}</div>
            {avail_html}
            <div class="pp-teammate-diff {diff_cls}">{diff_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with_col, without_col, off_col = st.columns(3)
    new_state: str | None = None
    with with_col:
        label = "● WITH" if state == "with" else "WITH"
        if st.button(label, key=f"{on_toggle_key}-with", use_container_width=True):
            new_state = "with"
    with without_col:
        label = "● W/O" if state == "without" else "W/O"
        if st.button(label, key=f"{on_toggle_key}-without", use_container_width=True):
            new_state = "without"
    with off_col:
        label = "● Off" if state == "off" else "Off"
        if st.button(label, key=f"{on_toggle_key}-off", use_container_width=True):
            new_state = "off"
    return new_state


def section_label(text: str) -> None:
    st.markdown(f'<div class="pp-section">{html.escape(text)}</div>',
                unsafe_allow_html=True)


def action_pill(label: str, *, key: str, active: bool,
                help: str | None = None,
                use_container_width: bool = True,
                disabled: bool = False) -> bool:
    """Compact WITH / WITHOUT pill button.

    Uses Streamlit's own primary type for the active state plus the
    scoped dialog CSS in `ui/theme.py` that tightens padding and radius.
    Button always reports as clicked on the rerun it was pressed.
    """
    return st.button(
        label, key=key, help=help,
        type="primary" if active else "secondary",
        use_container_width=use_container_width,
        disabled=disabled,
    )


_STATUS_CLASSES: dict[str, str] = {
    "OUT":    "pp-tm-status pp-tm-status-out",
    "GTD":    "pp-tm-status pp-tm-status-gtd",
    "QUES":   "pp-tm-status pp-tm-status-ques",
    "ACTIVE": "pp-tm-status pp-tm-status-active",
}


def status_chip(tag: str | None) -> str:
    """Return HTML for a teammate availability chip.

    Returns an empty string when `tag` is falsy or unknown so callers
    can `st.markdown(status_chip(tag) or "", ...)` without branching.
    Case-insensitive input.
    """
    if not tag:
        return ""
    key = str(tag).upper().strip()
    if key == "ACTIVE":
        # Active is the implicit default — render nothing to keep rows tidy.
        return ""
    cls = _STATUS_CLASSES.get(key)
    if not cls:
        return ""
    # Match compact injury-row styling (e.g. "+ GTD" in the teammate modal).
    plus = (
        '<span class="pp-tm-status-plus" aria-hidden="true">+ </span>'
        if key == "GTD" else "")
    return f'<span class="{cls}">{plus}{html.escape(key)}</span>'


def headshot_html(player_id: int | None, name: str = "", size: int = 34,
                  *, active: str | None = None) -> str:
    """Return an <img> tag for the official NBA headshot with an
    initials-bubble fallback on load error.

    `active` accepts ``"with"``, ``"without"``, or ``None``. When set, a
    coloured ring is drawn around the headshot so the row's active
    filter state reads without having to look at the pills.

    Uses the public NBA CDN which does not require auth. For historical
    players the CDN may 404 — the `onerror` handler replaces the broken
    image with the initials bubble so the row stays aligned.
    """
    active_class = f" active-{active}" if active in ("with", "without") else ""
    initials = "".join(part[:1] for part in (name or "").split()[:2]).upper() or "?"
    safe_initials = html.escape(initials)
    if not player_id:
        return (
            f'<span class="pp-tm-head-circle{active_class}" '
            f'style="width:{size}px;height:{size}px">{safe_initials}</span>'
        )
    url = f"https://cdn.nba.com/headshots/nba/latest/260x190/{int(player_id)}.png"
    # The onerror handler swaps the img for an initials bubble while
    # preserving the active-state ring class so the visual state survives.
    fallback_cls = f"pp-tm-head-circle{active_class}"
    fallback = (
        f"this.onerror=null;"
        f"this.outerHTML='<span class=\\'{fallback_cls}\\' "
        f"style=\\'width:{size}px;height:{size}px\\'>{safe_initials}</span>';"
    )
    return (
        f'<img class="pp-tm-headshot{active_class}" src="{url}" '
        f'alt="{html.escape(name)}" width="{size}" height="{size}" '
        f'onerror="{fallback}"/>'
    )


def render_chip_row(chips: Iterable[dict]) -> None:
    """Render a display-only row of chip HTML. Useful for suggested chips
    or active-filter summaries where interaction happens elsewhere."""
    pieces = []
    for c in chips:
        pieces.append(
            chip_html(
                c.get("label", ""),
                on=bool(c.get("on")),
                beta=bool(c.get("beta")),
                badge=c.get("badge"),
            )
        )
    st.markdown("".join(pieces), unsafe_allow_html=True)
