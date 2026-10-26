"""Dark-dashboard theme tokens and CSS injection.

Centralising the CSS here means every chip, card, and badge in the app
uses the same visual vocabulary. We build the CSS from the `COLORS`
dictionary in config so colour changes propagate automatically.
"""

from __future__ import annotations

import streamlit as st

from config import COLORS

# The full stylesheet. Written as a raw string with simple {token}
# substitutions to keep Python escaping out of CSS. Every class name is
# prefixed with "pp-" ("prop-panel") so it cannot collide with built-in
# Streamlit classes.
_CSS_TEMPLATE = """
/* ------------ Layout ------------ */
.block-container {{
    padding-top: 1.4rem;
    padding-bottom: 3rem;
    max-width: 1400px;
}}

/* ------------ Top bar ------------ */
.pp-topbar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border: 1px solid {chip_border};
    border-radius: 12px;
    background: linear-gradient(90deg, rgba(40,50,65,0.35), rgba(25,30,40,0.35));
    margin-bottom: 12px;
}}
.pp-topbar-title {{
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: 0.2px;
}}
.pp-topbar-subtitle {{
    font-size: 0.85rem;
    opacity: 0.75;
    margin-left: 10px;
}}

/* ------------ Chips ------------ */
.pp-chip {{
    display: inline-flex;
    align-items: center;
    padding: 5px 10px;
    margin: 3px 4px 3px 0;
    border-radius: 999px;
    border: 1px solid {chip_border};
    background: {chip_bg};
    font-size: 0.82rem;
    color: #e6edf5;
    cursor: default;
    user-select: none;
    line-height: 1.1;
}}
.pp-chip.on {{
    background: {chip_bg_on};
    border-color: {overlay};
    color: #ffffff;
}}
.pp-chip.beta {{
    background: {beta_bg};
    color: {beta_text};
    border-style: dashed;
    opacity: 0.85;
}}
.pp-chip .pp-chip-badge {{
    margin-left: 6px;
    padding: 1px 6px;
    font-size: 0.7rem;
    border-radius: 6px;
    background: rgba(255,255,255,0.08);
}}

/* Rank badge: coloured by rank quality. */
.pp-rank {{
    display: inline-block;
    margin-left: 6px;
    padding: 1px 6px;
    border-radius: 6px;
    font-size: 0.72rem;
    font-weight: 600;
    color: #0d1117;
}}
.pp-rank.good {{ background: {rank_good}; }}
.pp-rank.bad  {{ background: {rank_bad}; color: #ffffff; }}
.pp-rank.mid  {{ background: {rank_mid}; color: #0d1117; }}

/* ------------ Cards ------------ */
.pp-card {{
    border: 1px solid {chip_border};
    border-radius: 12px;
    padding: 12px 14px;
    background: rgba(127,127,127,0.05);
}}
.pp-card-title {{
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    opacity: 0.65;
    margin-bottom: 4px;
}}
.pp-card-value {{
    font-size: 1.35rem;
    font-weight: 700;
}}
.pp-card-diff {{
    font-size: 0.82rem;
    margin-top: 4px;
}}
.pp-card-diff.pos {{ color: {rank_good}; }}
.pp-card-diff.neg {{ color: {rank_bad}; }}
.pp-card-diff.neu {{ opacity: 0.6; }}

/* ------------ Teammate card ------------ */
.pp-teammate {{
    border: 1px solid {chip_border};
    border-radius: 12px;
    padding: 10px 12px;
    background: rgba(127,127,127,0.05);
    min-width: 160px;
}}
.pp-teammate-name {{
    font-size: 0.92rem;
    font-weight: 600;
}}
.pp-teammate-sub {{
    font-size: 0.75rem;
    opacity: 0.7;
    margin-top: 2px;
}}
.pp-teammate-diff {{
    margin-top: 6px;
    font-size: 0.82rem;
    font-weight: 600;
}}
.pp-teammate-diff.pos {{ color: {rank_good}; }}
.pp-teammate-diff.neg {{ color: {rank_bad}; }}

/* ------------ Section label ------------ */
.pp-section {{
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 1.3px;
    opacity: 0.7;
    margin: 14px 0 6px 0;
}}

/* ------------ Teammate table ------------ */
.pp-tm-selector-bar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    margin-bottom: 10px;
    border-radius: 12px;
    border: 1px solid {chip_border};
    background: linear-gradient(
        135deg,
        rgba(22, 28, 36, 0.95),
        rgba(34, 40, 52, 0.88));
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}}
.pp-tm-sel-left {{
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
}}
.pp-tm-sel-season {{
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: rgba(230, 237, 245, 0.75);
    flex-shrink: 0;
}}
.pp-tm-sel-logo {{
    width: 26px;
    height: 26px;
    object-fit: contain;
    flex-shrink: 0;
}}
.pp-tm-sel-team {{
    font-size: 0.9rem;
    font-weight: 600;
    color: #f7faff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.pp-tm-sel-chev {{
    opacity: 0.4;
    font-size: 0.82rem;
    margin-left: 6px;
    flex-shrink: 0;
}}

.pp-tm-player-cell {{
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 40px;
}}
.pp-tm-name-stack {{
    min-width: 0;
    flex: 1;
}}
.pp-tm-name-badges {{
    margin-top: 3px;
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    align-items: center;
}}
.pp-tm-player-meta {{
    font-size: 0.7rem;
    font-weight: 500;
    color: rgba(230, 237, 245, 0.45);
    margin-left: 8px;
    white-space: nowrap;
}}
.pp-tm-you-pill {{
    display: inline-flex;
    align-items: center;
    padding: 1px 7px;
    border-radius: 6px;
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    line-height: 1.15;
    color: rgba(230, 237, 245, 0.85);
    background: rgba(127, 127, 127, 0.12);
    border: 1px solid rgba(127, 127, 127, 0.25);
}}
/* Subject (researched) player row — visually muted; W/W-O stay disabled separately. */
.pp-tm-self-dim {{
    opacity: 0.52 !important;
    filter: saturate(0.85);
}}

/* Header row: column labels. Rendered once at the top of the scroll area. */
.pp-tm-head {{
    display: flex;
    align-items: center;
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 1.3px;
    text-transform: uppercase;
    color: rgba(230, 237, 245, 0.45);
    padding: 8px 4px 6px 4px;
    margin-bottom: 4px;
    border-bottom: 1px solid {chip_border};
    border-radius: 8px;
    background: rgba(10, 12, 18, 0.55);
}}
.pp-tm-head-v2 {{
    margin-bottom: 2px;
}}
/* Divider between teammate rows rendered from Streamlit widgets. */
div[role="dialog"] .pp-tm-row-gap {{
    height: 1px;
    margin: 4px 0 6px 0;
    background: rgba(127, 127, 127, 0.12);
    border-radius: 2px;
}}
/* Highlighted header pill for the currently-selected market column. */
.pp-tm-head .market-pill {{
    display: inline-block;
    padding: 2px 9px;
    border-radius: 999px;
    background: linear-gradient(180deg,
        rgba(52, 152, 219, 0.28),
        rgba(52, 152, 219, 0.10));
    border: 1px solid rgba(52, 152, 219, 0.45);
    color: #cfe6ff;
    font-size: 0.68rem;
    letter-spacing: 1.1px;
}}
/* Row container — tight vertical rhythm, consistent min-height so
   headshot + text + pills all line up without fiddling. */
.pp-tm-row {{
    display: flex;
    align-items: center;
    padding: 0 2px;
    min-height: 42px;
    border-bottom: 1px solid rgba(127,127,127,0.07);
}}
.pp-tm-row:hover {{ background: rgba(127,127,127,0.05); }}

/* Name block: bold name + thin metadata sub-line. */
.pp-tm-name {{
    font-size: 0.93rem;
    font-weight: 600;
    color: #e6edf5;
    line-height: 1.15;
    display: flex;
    align-items: center;
    gap: 6px;
}}
.pp-tm-sub {{
    font-size: 0.7rem;
    color: rgba(230, 237, 245, 0.5);
    margin-top: 1px;
    display: flex;
    align-items: center;
    gap: 6px;
    line-height: 1.1;
}}
/* Inline "WITH / WITHOUT" indicator next to the name when a filter is on. */
.pp-tm-active-badge {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 999px;
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    line-height: 1;
}}
.pp-tm-active-badge.with    {{ background: rgba(46, 204, 113, 0.18); color: #4cd483;
                              border: 1px solid rgba(46, 204, 113, 0.4); }}
.pp-tm-active-badge.without {{ background: rgba(231, 76, 60, 0.18); color: #ff7b6d;
                              border: 1px solid rgba(231, 76, 60, 0.4); }}

/* Generic right-aligned numeric cell. */
.pp-tm-stat {{
    font-size: 0.92rem;
    font-weight: 600;
    text-align: right;
    line-height: 1;
}}
/* Market column value: accent colour + bolder, so the "selected market"
   reads immediately even in a long list. */
.pp-tm-market-value {{
    font-size: 0.95rem;
    font-weight: 700;
    text-align: right;
    color: #9cc7ef;
    line-height: 1;
}}
.pp-tm-market-value.dim {{ color: rgba(230, 237, 245, 0.3); font-weight: 500; }}

/* POS pill — small neutral chip that keeps the column scannable. */
.pp-tm-pos {{
    display: inline-block;
    padding: 1px 7px;
    border-radius: 6px;
    background: rgba(127, 127, 127, 0.12);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.5px;
    color: rgba(230, 237, 245, 0.75);
}}

/* Headshot: same dimensions as the fallback bubble so rows never
   reflow if an image 404s. Colour ring signals active filter state. */
.pp-tm-headshot, .pp-tm-head-circle {{
    width: 34px; height: 34px;
    border-radius: 50%;
    background: {chip_bg};
    border: 2px solid transparent;
    box-sizing: border-box;
    vertical-align: middle;
}}
.pp-tm-headshot      {{ object-fit: cover; border-color: {chip_border}; }}
.pp-tm-head-circle   {{ display: inline-flex; align-items: center; justify-content: center;
                        font-size: 0.78rem; font-weight: 700; color: rgba(230,237,245,0.7);
                        border-color: {chip_border}; }}
.pp-tm-headshot.active-with,
.pp-tm-head-circle.active-with    {{ border-color: {rank_good}; box-shadow: 0 0 0 2px rgba(46, 204, 113, 0.18); }}
.pp-tm-headshot.active-without,
.pp-tm-head-circle.active-without {{ border-color: {rank_bad};  box-shadow: 0 0 0 2px rgba(231, 76, 60, 0.18); }}

/* Availability chip rendered inline under the name. */
.pp-tm-status {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 6px;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    line-height: 1.15;
}}
.pp-tm-status-out    {{ background: rgba(231, 76, 60, 0.18); color: #ff7b6d; }}
.pp-tm-status-gtd    {{ background: rgba(241, 196, 15, 0.18); color: #ffd35a; }}
.pp-tm-status-plus {{
    font-weight: 700;
    margin-right: 1px;
    opacity: 0.95;
}}
.pp-tm-status-ques   {{ background: rgba(52, 152, 219, 0.18); color: #74c0fc; }}
.pp-tm-status-active {{ background: rgba(46, 204, 113, 0.15); color: #4cd483; }}

/* Scope: tighten every button that lives inside the modal dialogs so
   the WITH/WITHOUT pills read as pills rather than chunky CTAs. */
div[role="dialog"] .stButton > button {{
    min-height: 30px;
    padding: 3px 8px;
    font-weight: 700;
    line-height: 1;
    border-radius: 8px;
}}
/* Slightly brighten the active (primary) pill so the WITH/WITHOUT
   selection reads at a glance even before the row indicators kick in. */
div[role="dialog"] .stButton > button[kind="primary"] {{
    letter-spacing: 0.5px;
    box-shadow: 0 0 0 2px rgba(52, 152, 219, 0.22);
}}
div[role="dialog"] .stButton > button:disabled {{
    opacity: 0.32 !important;
    filter: saturate(0.35);
    box-shadow: none !important;
    cursor: default;
}}
/* Compact selectbox (availability editor) */
div[role="dialog"] div[data-baseweb="select"] > div {{
    min-height: 30px;
}}

.pp-tm-summary-chip {{
    display: inline-flex;
    align-items: center;
    padding: 3px 8px;
    margin: 2px 4px 2px 0;
    border-radius: 999px;
    font-size: 0.75rem;
    border: 1px solid {chip_border};
    background: {chip_bg_on};
    color: #ffffff;
}}
.pp-tm-summary-chip .sym {{
    display: inline-block;
    min-width: 18px;
    text-align: center;
    font-weight: 700;
    margin-right: 6px;
    padding: 0 5px;
    border-radius: 6px;
    background: rgba(255,255,255,0.10);
}}
.pp-tm-summary-chip.with-mode .sym {{ color: {rank_good}; }}
.pp-tm-summary-chip.without-mode .sym {{ color: {rank_bad}; }}

/* ------------ Streamlit built-ins ------------ */
div[data-testid="stMetric"] {{
    background-color: rgba(127,127,127,0.06);
    border: 1px solid {chip_border};
    border-radius: 10px;
    padding: 12px 14px;
}}
div[data-testid="stMetricDelta"] {{ font-weight: 600; }}
button[data-baseweb="tab"] {{ font-size: 1rem; }}
"""


def inject_theme() -> None:
    """Inject the CSS into the Streamlit document head. Call once per run."""
    css = _CSS_TEMPLATE.format(**COLORS)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
