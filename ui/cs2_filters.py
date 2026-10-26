"""Session-state handling for the CS2 page.

Keeps CS2 state completely decoupled from the player-prop ``FilterState``
per project convention (FilterState is for Player Props only). All CS2
keys live under the ``_cs2_*`` prefix in ``st.session_state``.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from config import CS2_RECENT_N
from services.cs2.constants import CS2_ACTIVE_MAPS


CS2_SAMPLE_OPTIONS: tuple[int, ...] = (5, 10, 20, 30)


@dataclass
class CS2FilterState:
    sample_n: int = CS2_RECENT_N
    selected_map: str | None = None     # None = all maps
    best_of: int = 3
    online_only: bool = False           # reserved for future
    event_filter: str | None = None     # reserved for future


def get_cs2_filters() -> CS2FilterState:
    state = st.session_state.get("_cs2_filters")
    if not isinstance(state, CS2FilterState):
        state = CS2FilterState()
        st.session_state["_cs2_filters"] = state
    return state


def render_filter_bar() -> CS2FilterState:
    """Inline filter controls shown above the sub-tabs. Returns the updated
    filter state object (also stored in session state)."""
    state = get_cs2_filters()
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            state.sample_n = st.selectbox(
                "Sample size",
                CS2_SAMPLE_OPTIONS,
                index=CS2_SAMPLE_OPTIONS.index(state.sample_n)
                if state.sample_n in CS2_SAMPLE_OPTIONS else 1,
                key="_cs2_sample_n",
                help="How many recent matches to pull per team / player.",
            )
        with c2:
            map_opts = ["All maps"] + list(CS2_ACTIVE_MAPS)
            current = state.selected_map or "All maps"
            idx = map_opts.index(current) if current in map_opts else 0
            pick = st.selectbox(
                "Map focus", map_opts, index=idx, key="_cs2_map_focus",
                help="Filter team/player analysis to one map.",
            )
            state.selected_map = None if pick == "All maps" else pick
        with c3:
            state.best_of = st.selectbox(
                "Best-of (for series markets)", (1, 3, 5),
                index={1: 0, 3: 1, 5: 2}.get(state.best_of, 1),
                key="_cs2_best_of",
                help="Used by the Total Maps market projection.",
            )
    return state


def reset_cs2_filters() -> None:
    st.session_state.pop("_cs2_filters", None)
