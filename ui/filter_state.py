"""Session-state filter container.

The entire filter-driven workflow hangs off a single `FilterState` object
held in `st.session_state["filter_state"]`. Every widget in the filter
modal reads from and writes to that object; the filter pipeline in
`services/filter_service.py` consumes a snapshot of it to produce
`filtered` and `excluded` game samples.

Defaults use the current-season shorthand plus **Last 30** under **Games**;
Filters can only narrow the sample unless you pick **Max** or raise **Custom N**.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import streamlit as st

from config import formatted_game_count_filter_caption


# Chip key used by the modal when "no filter" is active for a group of
# mutually exclusive chips (e.g. home/away can be "home", "away", or None).
_OFF = None


@dataclass
class FilterState:
    """Filter snapshot for `apply_all_filters` and overlay widgets.

    A fresh instance mirrors the Filters modal defaults (incl. **Last 30** games).
    """

    # -- Season & Games -----------------------------------------------------
    season_selection: str = "25/26"          # one of SEASON_SELECTOR_OPTIONS
    game_count_mode: str = "30"              # see GAME_COUNT_RADIO_OPTIONS / GAME_COUNT_CODES
    game_count_custom_n: int = 15            # used when game_count_mode == "Custom"
    lock_game_count: bool = False            # persist game_count when switching players

    # -- Splits ------------------------------------------------------------
    # Mutually exclusive toggles stored as a single value each so the UI
    # logic stays simple (radio / tri-state).
    home_away: str | None = None             # "home" | "away" | None
    win_loss: str | None = None              # "win" | "loss" | None
    regular_playoff: str | None = None       # "regular" | "playoffs" | None
    margin_filter: str | None = None         # "close" | "blowout" | None
    b2b_only: bool = False
    h2h_only: bool = False                   # filter to current opponent only
    rest_min: int | None = None              # inclusive
    rest_max: int | None = None              # inclusive

    # -- Stat range filters -----------------------------------------------
    # {col: (lo, hi)} — half-open style supported by passing None.
    stat_ranges: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)

    # -- Stats tab overlay ------------------------------------------------
    overlay_stat: str | None = None          # column to overlay on graph
    avg_or_median: str = "avg"               # "avg" | "median"

    # -- Teammate ---------------------------------------------------------
    # Map teammate_player_id -> "with" | "without" (multi-select capable).
    teammate_modes: dict[int, str] = field(default_factory=dict)
    # Manual availability tags: {teammate_player_id: "OUT"|"GTD"|"ACTIVE"|...}
    teammate_availability: dict[int, str] = field(default_factory=dict)

    # -- Opp ranks --------------------------------------------------------
    # {chip_key: True} — set of enabled opp-rank chips from the modal.
    # Opp-rank chips narrow the sample to games against teams whose rank
    # in that column is in the top-10 (favourable). This matches the
    # "suggested context" intent of the original product.
    opp_rank_active: dict[str, bool] = field(default_factory=dict)
    opp_rank_timeframe: str = "Season"       # presentational for now

    # -- Beta chips -------------------------------------------------------
    # Kept purely for UI state. The filter pipeline ignores these so they
    # truly are a no-op. Documented in README.
    beta_chips_active: dict[str, bool] = field(default_factory=dict)

    # =====================================================================
    # Serialization helpers
    # =====================================================================

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilterState":
        if not data:
            return cls()
        # Coerce known typed fields; anything unknown is dropped.
        init: dict[str, Any] = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                init[f] = data[f]
        if "teammate_modes" in init:
            # JSON keys are strings; we want ints.
            init["teammate_modes"] = {
                int(k): v for k, v in (init["teammate_modes"] or {}).items()
            }
        if "teammate_availability" in init:
            init["teammate_availability"] = {
                int(k): v for k, v in (init["teammate_availability"] or {}).items()
            }
        if "stat_ranges" in init:
            init["stat_ranges"] = {
                k: tuple(v) for k, v in (init["stat_ranges"] or {}).items()
            }
        return cls(**init)

    # =====================================================================
    # Active-summary helpers
    # =====================================================================

    def active_chips(self) -> list[tuple[str, str]]:
        """Return a list of (key, label) pairs describing currently active
        filters. Used to render the chip strip at the top of the app."""
        out: list[tuple[str, str]] = []
        if self.season_selection and self.season_selection != "25/26":
            out.append(("season", f"Season: {self.season_selection}"))
        if self.game_count_mode != "Max":
            out.append((
                "games",
                formatted_game_count_filter_caption(
                    self.game_count_mode,
                    custom_n=self.game_count_custom_n,
                ),
            ))
        if self.home_away:
            out.append(("home_away", self.home_away.capitalize()))
        if self.win_loss:
            out.append(("win_loss", self.win_loss.capitalize()))
        if self.regular_playoff:
            out.append(("regular_playoff", self.regular_playoff.capitalize()))
        if self.b2b_only:
            out.append(("b2b", "B2B only"))
        if self.h2h_only:
            out.append(("h2h", "H2H only"))
        if self.margin_filter == "close":
            out.append(("margin", "Close games (±5)"))
        elif self.margin_filter == "blowout":
            out.append(("margin", "Blowouts (>15)"))
        if self.rest_min is not None or self.rest_max is not None:
            lo = self.rest_min if self.rest_min is not None else "-"
            hi = self.rest_max if self.rest_max is not None else "-"
            out.append(("rest", f"Rest {lo}–{hi}"))
        for col, (lo, hi) in self.stat_ranges.items():
            if lo is None and hi is None:
                continue
            lo_s = f"{lo:g}" if lo is not None else "-"
            hi_s = f"{hi:g}" if hi is not None else "-"
            out.append((f"stat:{col}", f"{col} {lo_s}–{hi_s}"))
        for pid, mode in self.teammate_modes.items():
            out.append((f"teammate:{pid}", f"Teammate {pid} ({mode})"))
        for key, enabled in self.opp_rank_active.items():
            if enabled:
                out.append((f"rank:{key}", key.replace("_", " ")))
        return out

    def clear(self) -> None:
        """Reset every filter to its default ('show everything') state.
        season_selection and game_count_mode stay on their current defaults
        unless the user explicitly locked them."""
        defaults = FilterState()
        for f in self.__dataclass_fields__:
            if f in ("season_selection", "game_count_mode", "lock_game_count") and self.lock_game_count:
                continue
            setattr(self, f, getattr(defaults, f))


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------

_SESSION_KEY = "filter_state"
_WNBA_SESSION_KEY = "filter_state_wnba"


def _session_key() -> str:
    league = st.session_state.get("_props_league", "nba")
    return _SESSION_KEY if league != "wnba" else _WNBA_SESSION_KEY


def get_state() -> FilterState:
    """Return the FilterState for the active props league (NBA vs WNBA)."""
    key = _session_key()
    if key not in st.session_state:
        st.session_state[key] = FilterState()
    return st.session_state[key]


def set_state(new_state: FilterState) -> None:
    st.session_state[_session_key()] = new_state


def reset_state(preserve_lock: bool = True) -> None:
    key = _session_key()
    s = get_state()
    if preserve_lock and s.lock_game_count:
        prev_season = s.season_selection
        prev_mode = s.game_count_mode
        prev_n = s.game_count_custom_n
        st.session_state[key] = FilterState(
            season_selection=prev_season,
            game_count_mode=prev_mode,
            game_count_custom_n=prev_n,
            lock_game_count=True,
        )
    else:
        st.session_state[key] = FilterState()
