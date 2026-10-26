"""Offline smoke-test for the expanded stat categories.

Run with:
    python _smoke_test.py

Does NOT call any live API.  Validates:
  1. config.py constants are internally consistent
  2. models/stats.py  — sum-based derived stats (PR, Stocks, PRA, …)
  3. models/stats.py  — threshold-based stats (DD, TD)
  4. partial_game_service (+ quarter aliases) — PBP-derived 1Q/1H aggregates
  5. app.py default-line logic for DD/TD (simulated, no Streamlit)

Exits with code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import math
import sys
import types
import unittest
import unittest.mock as mock
from pathlib import Path

# Make sure project root is importable.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ── 1. Config consistency ─────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    def test_all_derived_stats_in_stat_columns(self):
        from config import DERIVED_STATS, STAT_COLUMNS
        for col in DERIVED_STATS:
            self.assertIn(col, STAT_COLUMNS.values(),
                          f"DERIVED_STATS key {col!r} missing from STAT_COLUMNS values")

    def test_all_threshold_stats_in_stat_columns(self):
        from config import THRESHOLD_STATS, STAT_COLUMNS
        for col in THRESHOLD_STATS:
            self.assertIn(col, STAT_COLUMNS.values(),
                          f"THRESHOLD_STATS key {col!r} missing from STAT_COLUMNS values")

    def test_quarter_cols_in_stat_columns(self):
        from config import QUARTER_STAT_COLS, STAT_COLUMNS
        for col in QUARTER_STAT_COLS:
            self.assertIn(col, STAT_COLUMNS.values(),
                          f"QUARTER_STAT_COLS member {col!r} missing from STAT_COLUMNS values")

    def test_base_numeric_columns_has_tier1(self):
        from config import BASE_NUMERIC_COLUMNS
        for col in ("OREB", "DREB", "FTM", "FTA", "FG3A", "PF"):
            self.assertIn(col, BASE_NUMERIC_COLUMNS,
                          f"Tier-1 column {col!r} missing from BASE_NUMERIC_COLUMNS")


# ── 2. Sum-based derived stats ────────────────────────────────────────────────

import pandas as pd

SAMPLE_GAMELOGS = pd.DataFrame({
    "PTS":  [30, 12,  5],
    "REB":  [10,  5,  2],
    "AST":  [10,  4,  1],
    "STL":  [ 3,  0,  0],
    "BLK":  [ 1,  0,  0],
})


class TestSumDerivedStats(unittest.TestCase):
    def setUp(self):
        from models.stats import add_derived_stats
        self.df = add_derived_stats(SAMPLE_GAMELOGS.copy())

    def test_pra(self):
        expected = [50, 21, 8]
        self.assertListEqual(list(self.df["PRA"]), expected)

    def test_pr(self):
        expected = [40, 17, 7]
        self.assertListEqual(list(self.df["PR"]), expected)

    def test_stocks(self):
        expected = [4, 0, 0]
        self.assertListEqual(list(self.df["Stocks"]), expected)

    def test_pa(self):
        expected = [40, 16, 6]
        self.assertListEqual(list(self.df["PA"]), expected)

    def test_ra(self):
        expected = [20, 9, 3]
        self.assertListEqual(list(self.df["RA"]), expected)


# ── 3. Threshold-based stats (DD / TD) ───────────────────────────────────────

class TestThresholdStats(unittest.TestCase):
    """
    Row 0: PTS=30, REB=10, AST=10 → 3 categories ≥ 10  → DD=1, TD=1
    Row 1: PTS=12,  REB=5, AST=4  → 0 categories ≥ 10  → DD=0, TD=0
    Row 2: PTS=10, REB=10, AST=2  → 2 categories ≥ 10  → DD=1, TD=0
    """
    def setUp(self):
        from models.stats import add_derived_stats
        data = pd.DataFrame({
            "PTS": [30, 12, 10],
            "REB": [10,  5, 10],
            "AST": [10,  4,  2],
            "STL": [ 0,  0,  0],
            "BLK": [ 0,  0,  0],
        })
        self.df = add_derived_stats(data)

    def test_dd_row0(self):
        self.assertEqual(self.df.loc[0, "DD"], 1.0)

    def test_td_row0(self):
        self.assertEqual(self.df.loc[0, "TD"], 1.0)

    def test_dd_row1(self):
        self.assertEqual(self.df.loc[1, "DD"], 0.0)

    def test_td_row1(self):
        self.assertEqual(self.df.loc[1, "TD"], 0.0)

    def test_dd_row2(self):
        self.assertEqual(self.df.loc[2, "DD"], 1.0)

    def test_td_row2(self):
        self.assertEqual(self.df.loc[2, "TD"], 0.0)

    def test_dd_columns_exist(self):
        self.assertIn("DD", self.df.columns)
        self.assertIn("TD", self.df.columns)

    def test_empty_df_returns_empty(self):
        from models.stats import add_derived_stats
        result = add_derived_stats(pd.DataFrame())
        self.assertTrue(result.empty)

    def test_missing_source_columns_skipped_gracefully(self):
        """If STL/BLK are absent, DD/TD are still computed from available cols."""
        from models.stats import add_derived_stats
        data = pd.DataFrame({"PTS": [15], "REB": [12]})
        result = add_derived_stats(data)
        # With 2 of 5 columns available, at least 2 can hit threshold.
        self.assertIn("DD", result.columns)


# ── 4. partial_game / quarter laziness (PlayByPlayV3 cache keys) ────────────────

class TestPartialGameCaching(unittest.TestCase):
    """Cache behaviour wired through ``services.quarter_service`` aliases."""

    def test_partial_stats_cached_empty_df(self):
        from services.quarter_service import quarter_stats_are_cached
        self.assertTrue(quarter_stats_are_cached(pd.DataFrame()))

    @mock.patch("services.partial_game_service.get_cached", return_value=None)
    def test_partial_stats_not_cached_when_miss(self, _mock_get):
        from services.quarter_service import quarter_stats_are_cached
        gl = pd.DataFrame({"Game_ID": ["0022400001", "0022400002"]})
        self.assertFalse(quarter_stats_are_cached(gl))

    def test_partial_stats_cached_when_both_hit(self):
        """Both PBP and roster namespaces must resolve for every game id."""
        from services import partial_game_service as pgs

        cache_hits = {"calls": 0}

        def _fake_get(namespace: str, key: str, ttl_seconds):
            cache_hits["calls"] += 1
            if namespace == pgs._PBP_NAMESPACE:
                return [{"stub": True}]
            if namespace == pgs._ROSTER_NAMESPACE:
                return [{"stub": True}]
            return None

        with mock.patch("services.partial_game_service.get_cached", side_effect=_fake_get):
            from services.quarter_service import quarter_stats_are_cached

            gl = pd.DataFrame({"Game_ID": ["0022400001"]})
            self.assertTrue(quarter_stats_are_cached(gl))
            self.assertEqual(cache_hits["calls"], 2)

    def test_enrich_returns_unchanged_on_empty_log(self):
        from services.quarter_service import enrich_with_quarter_stats
        result = enrich_with_quarter_stats(pd.DataFrame(), player_id=2544)
        self.assertTrue(result.empty)

    @mock.patch(
        "services.partial_game_service.aggregate_player_period_stats",
        return_value={
            "PTS": 9.0, "REB": 4.0, "AST": 5.0, "FG3M": 2.0,
        },
    )
    @mock.patch(
        "services.partial_game_service.fetch_playbyplay_v3_dataframe",
        return_value=pd.DataFrame(),
    )
    @mock.patch(
        "services.partial_game_service.fetch_boxscore_roster_dataframe",
        return_value=pd.DataFrame(
            [{"teamId": 1, "personId": 2544, "familyName": "T", "minutes": "PT1M"}]),
    )
    def test_enrich_adds_partial_columns(self, _mock_roster, _mock_pbp, _mock_agg):
        from services.quarter_service import enrich_with_quarter_stats

        req = (
            "Q1_PTS", "Q1_REB", "Q1_AST", "Q1_FG3M",
            "H1_PTS", "H1_REB", "H1_AST", "H1_FG3M",
        )
        gl = pd.DataFrame({
            "Game_ID": ["0022400001"],
            "GAME_DATE": ["2024-11-01"],
        })
        result = enrich_with_quarter_stats(gl, player_id=2544, progress_callback=None)
        for col in req:
            self.assertIn(col, result.columns, f"Missing {col}")

        self.assertEqual(result.loc[0, "Q1_PTS"], 9.0)


class TestPartialGameAggregation(unittest.TestCase):
    """Pure aggregation tests (no network)."""

    def test_points_threes_ft_rebounds(self):
        from services.partial_game_service import aggregate_player_period_stats, SCOPE_1Q_PERIODS

        pbp = pd.DataFrame([
            {"period": 1, "personId": 100, "teamId": 1, "actionType": "Made Shot",
             "shotValue": 2, "description": "X"},
            {"period": 1, "personId": 100, "teamId": 1, "actionType": "Made Shot",
             "shotValue": 3, "description": "Y"},
            {"period": 1, "personId": 100, "teamId": 1, "actionType": "Free Throw",
             "description": "A Free Throw 1 of 1 (1 PTS)"},
            {"period": 1, "personId": 100, "teamId": 1, "actionType": "Rebound",
             "description": "REBOUND"},
            {"period": 2, "personId": 100, "teamId": 1, "actionType": "Made Shot",
             "shotValue": 2, "description": "Z"},
        ])
        ros = pd.DataFrame([{"teamId": 1, "personId": 100,
                             "familyName": "Allen", "minutes": "PT20M"}])
        q1 = aggregate_player_period_stats(
            pbp, ros, player_id=100,
            periods=SCOPE_1Q_PERIODS, played_this_game=True,
        )
        assert q1 is not None
        self.assertEqual(q1["PTS"], 6.0)
        self.assertEqual(q1["FG3M"], 1.0)
        self.assertEqual(q1["REB"], 1.0)

    def test_assist_credit_from_description(self):
        import services.partial_game_service as pgs

        pbp = pd.DataFrame([
            {"period": 1, "personId": 10, "teamId": 201, "actionType": "Made Shot",
             "shotValue": 2,
             "description": "Jones  Layup (2 PTS) (Smith 1 AST)"},
        ])
        ros = pd.DataFrame([
            {"teamId": 201, "personId": 11, "familyName": "Smith", "minutes": "PT1M"},
            {"teamId": 201, "personId": 10, "familyName": "Jones", "minutes": "PT2M"},
        ])
        stats = pgs.aggregate_player_period_stats(
            pbp, ros, player_id=11,
            periods=pgs.SCOPE_1Q_PERIODS, played_this_game=True,
        )
        assert stats is not None
        self.assertEqual(stats["AST"], 1.0)

    def test_dnp_returns_none(self):
        from services.partial_game_service import aggregate_player_period_stats, SCOPE_1Q_PERIODS

        pbp = pd.DataFrame()
        ros = pd.DataFrame(
            [{"teamId": 1, "personId": 1, "familyName": "A", "minutes": "DNP-COVID"}])
        stats = aggregate_player_period_stats(
            pbp, ros, player_id=1,
            periods=SCOPE_1Q_PERIODS, played_this_game=False)
        self.assertIsNone(stats)


# ── 5. Prop line default logic ────────────────────────────────────────────────

class TestDefaultPropLine(unittest.TestCase):
    """The sidebar logic defaults the prop line to 0.5 for DD/TD."""

    def test_dd_default_is_half(self):
        from config import THRESHOLD_STATS
        stat_col = "DD"
        default = 0.5 if stat_col in THRESHOLD_STATS else 24.5
        self.assertEqual(default, 0.5)

    def test_td_default_is_half(self):
        from config import THRESHOLD_STATS
        stat_col = "TD"
        default = 0.5 if stat_col in THRESHOLD_STATS else 24.5
        self.assertEqual(default, 0.5)

    def test_pts_default_is_not_half(self):
        from config import THRESHOLD_STATS
        stat_col = "PTS"
        default = 0.5 if stat_col in THRESHOLD_STATS else 24.5
        self.assertEqual(default, 24.5)


# ── 6. Filter pipeline ────────────────────────────────────────────────────────

import pandas as pd  # noqa: E402  (kept after explicit sys.path insert above)


def _sample_game_log() -> pd.DataFrame:
    """Small, deterministic game-log fixture used by the filter tests."""
    return pd.DataFrame({
        "Game_ID":      ["0022400001", "0022400002", "0022400003",
                          "0022400004", "0022400005"],
        "GAME_DATE":    pd.to_datetime([
            "2024-11-05", "2024-11-03", "2024-11-01", "2024-10-30", "2024-10-28",
        ]),
        "SEASON_TYPE":  ["Regular Season"] * 5,
        "MATCHUP":      ["LAL vs BOS", "LAL @ GSW", "LAL vs BOS",
                         "LAL @ DEN", "LAL vs PHX"],
        "OPPONENT":     ["BOS", "GSW", "BOS", "DEN", "PHX"],
        "OPP_TEAM_ID":  [1610612738, 1610612744, 1610612738,
                          1610612743, 1610612756],
        "IS_HOME":      [True, False, True, False, True],
        "IS_WIN":       [True, False, True, False, True],
        "REST_DAYS":    [1, 1, 2, 3, 1],
        "PLUS_MINUS":   [3, -12, 18, -4, 22],
        "PTS":          [24, 18, 30, 21, 27],
        "REB":          [6, 4, 8, 5, 7],
        "AST":          [7, 5, 9, 6, 8],
        "MIN":          [32, 28, 36, 30, 34],
    })


class TestFilterPipeline(unittest.TestCase):
    """End-to-end filter-service behaviour on the sample fixture."""

    def test_home_filter_narrows_to_home_games(self):
        from services.filter_service import apply_home_away_filter
        df = _sample_game_log()
        out = apply_home_away_filter(df, "home")
        self.assertEqual(len(out), 3)
        self.assertTrue(all(out["IS_HOME"]))

    def test_h2h_filter_matches_by_abbr(self):
        from services.filter_service import apply_h2h_filter
        df = _sample_game_log()
        out = apply_h2h_filter(df, "BOS")
        self.assertEqual(len(out), 2)
        self.assertTrue(all(out["OPPONENT"] == "BOS"))

    def test_b2b_filter_keeps_only_short_rest(self):
        from services.filter_service import apply_b2b_filter
        df = _sample_game_log()
        out = apply_b2b_filter(df, True)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(out["REST_DAYS"] <= 1))

    def test_margin_filter_close(self):
        from services.filter_service import apply_margin_filter
        df = _sample_game_log()
        out = apply_margin_filter(df, "close")
        self.assertEqual(len(out), 2)  # ±3 and ±4

    def test_stat_range_filter(self):
        from services.filter_service import apply_stat_range_filter
        df = _sample_game_log()
        out = apply_stat_range_filter(df, "PTS", 22, None)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(out["PTS"] >= 22))

    def test_game_count_caps_to_most_recent(self):
        from services.filter_service import apply_game_count_filter
        df = _sample_game_log()
        # Fixture is newest-first already.
        out = apply_game_count_filter(df, "2")
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[0]["Game_ID"], "0022400001")

    def test_opp_rank_filter_uses_rank_map(self):
        from services.filter_service import apply_opp_rank_filter
        df = _sample_game_log()
        # Rank map pretends BOS defence allows the most PTS (rank 1).
        rank_map = {1610612738: 1, 1610612744: 20, 1610612743: 15, 1610612756: 9}
        out = apply_opp_rank_filter(df, {"opp_pts_rank": True},
                                     {"opp_pts_rank": rank_map},
                                     favourable_rank_cutoff=10)
        # Keeps only games vs teams with rank <= 10: BOS (x2) and PHX.
        self.assertEqual(set(out["OPPONENT"]), {"BOS", "PHX"})

    def test_teammate_with_intersects_game_ids(self):
        from services.filter_service import apply_teammate_filter
        df = _sample_game_log()
        # Teammate only played in games 1 and 3.
        gids = {1: {"0022400001", "0022400003"}}
        out = apply_teammate_filter(df, gids, {1: "with"})
        self.assertEqual(set(out["Game_ID"]), {"0022400001", "0022400003"})

    def test_teammate_without_excludes_game_ids(self):
        from services.filter_service import apply_teammate_filter
        df = _sample_game_log()
        gids = {1: {"0022400001", "0022400003"}}
        out = apply_teammate_filter(df, gids, {1: "without"})
        self.assertEqual(set(out["Game_ID"]),
                         {"0022400002", "0022400004", "0022400005"})

    def test_teammate_missing_game_ids_is_noop(self):
        """Empty teammate game-id set must not silently drop the whole sample."""
        from services.filter_service import apply_teammate_filter
        df = _sample_game_log()
        out = apply_teammate_filter(df, {1: set()}, {1: "with"})
        self.assertEqual(len(out), len(df))

    def test_apply_all_filters_determinism(self):
        """Same input + state = same output rows, regardless of call order."""
        from services.filter_service import apply_all_filters
        from ui.filter_state import FilterState
        df = _sample_game_log()
        state = FilterState(home_away="home", b2b_only=False,
                             game_count_mode="Max")
        res1 = apply_all_filters(df, state)
        res2 = apply_all_filters(df, state)
        pd.testing.assert_frame_equal(res1.filtered.reset_index(drop=True),
                                       res2.filtered.reset_index(drop=True))
        pd.testing.assert_frame_equal(res1.excluded.reset_index(drop=True),
                                       res2.excluded.reset_index(drop=True))

    def test_apply_all_filters_order_stat_range_before_count(self):
        """game_count cap is applied LAST so the cap reflects the narrowed
        sample, not the original."""
        from services.filter_service import apply_all_filters
        from ui.filter_state import FilterState
        df = _sample_game_log()
        state = FilterState(
            home_away="home",   # narrows to 3 home games
            game_count_mode="2",
        )
        res = apply_all_filters(df, state)
        # After home filter → 3 rows, after cap → 2 rows (most recent home).
        self.assertEqual(len(res.filtered), 2)
        self.assertEqual(res.narrowed_total, 3)
        # Excluded is the 2 away games (cap does NOT contribute).
        self.assertEqual(len(res.excluded), 2)

    def test_filtered_excluded_partition_is_disjoint(self):
        from services.filter_service import apply_all_filters
        from ui.filter_state import FilterState
        df = _sample_game_log()
        state = FilterState(win_loss="win", game_count_mode="Max")
        res = apply_all_filters(df, state)
        overlap = set(res.filtered["Game_ID"]) & set(res.excluded["Game_ID"])
        self.assertFalse(overlap, "filtered and excluded must be disjoint")
        total = len(res.filtered) + len(res.excluded)
        self.assertEqual(total, len(df))


# ── 7. Diff math ──────────────────────────────────────────────────────────────


class TestDiffMath(unittest.TestCase):
    def test_diff_is_filtered_minus_excluded(self):
        from models.metrics import compute_filtered_vs_excluded_diff
        filtered = pd.DataFrame({"PTS": [30, 20]})   # mean = 25
        excluded = pd.DataFrame({"PTS": [10, 20]})   # mean = 15
        d = compute_filtered_vs_excluded_diff(filtered, excluded, "PTS", mode="avg")
        self.assertAlmostEqual(d["filtered"], 25.0)
        self.assertAlmostEqual(d["excluded"], 15.0)
        self.assertAlmostEqual(d["diff"], 10.0)

    def test_diff_none_when_excluded_empty(self):
        from models.metrics import compute_filtered_vs_excluded_diff
        filtered = pd.DataFrame({"PTS": [30, 20]})
        empty = pd.DataFrame({"PTS": []})
        d = compute_filtered_vs_excluded_diff(filtered, empty, "PTS")
        self.assertIsNone(d["excluded"])
        self.assertIsNone(d["diff"])

    def test_median_mode_differs_from_mean(self):
        from models.metrics import compute_graph_avg
        df = pd.DataFrame({"PTS": [1, 2, 100]})
        self.assertAlmostEqual(compute_graph_avg(df, "PTS", mode="avg"), 34.333333, places=4)
        self.assertAlmostEqual(compute_graph_avg(df, "PTS", mode="median"), 2.0)

    def test_hit_rate_half_line_uses_strict_gt(self):
        from models.metrics import compute_hit_rate
        df = pd.DataFrame({"PTS": [20, 24, 25]})
        hr = compute_hit_rate(df, "PTS", 24.5)
        self.assertEqual(hr.hits, 1)
        self.assertEqual(hr.games, 3)

    def test_hit_rate_integer_line_uses_gte(self):
        from models.metrics import compute_hit_rate
        df = pd.DataFrame({"PTS": [20, 24, 25]})
        hr = compute_hit_rate(df, "PTS", 24)
        self.assertEqual(hr.hits, 2)


# ── 8. Beta-chip no-op safety ─────────────────────────────────────────────────


class TestBetaChipSafety(unittest.TestCase):
    """Toggling beta chips must never narrow the sample."""

    def test_beta_opp_rank_chip_has_no_effect(self):
        from services.filter_service import apply_opp_rank_filter
        df = _sample_game_log()
        # No rank map for chip "pt_iso" → pipeline should return df unchanged.
        out = apply_opp_rank_filter(df, {"pt_iso": True}, {}, favourable_rank_cutoff=10)
        pd.testing.assert_frame_equal(out.reset_index(drop=True),
                                       df.reset_index(drop=True))

    def test_filter_state_beta_toggle_is_ignored_by_pipeline(self):
        from services.filter_service import apply_all_filters
        from ui.filter_state import FilterState
        df = _sample_game_log()
        state = FilterState()
        state.beta_chips_active["pt_iso"] = True
        state.beta_chips_active["closing_spread"] = True
        res = apply_all_filters(df, state)
        # Everything should be kept; nothing excluded.
        self.assertEqual(len(res.filtered), len(df))
        self.assertEqual(len(res.excluded), 0)


# ── 9. Graph DNP filter ───────────────────────────────────────────────────────


class TestGraphDropDnp(unittest.TestCase):
    """ui.graph._drop_dnp skips DNP rows so bars stay contiguous."""

    def test_drops_zero_and_nan_minutes(self):
        from ui.graph import _drop_dnp
        import numpy as np
        df = pd.DataFrame({
            "GAME_DATE": pd.to_datetime(["2024-11-01", "2024-11-03", "2024-11-05"]),
            "MIN":       [28, 0, np.nan],
            "PTS":       [22, 0, 0],
        })
        kept, dropped = _drop_dnp(df, "PTS")
        self.assertEqual(dropped, 2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept.iloc[0]["MIN"], 28)

    def test_no_drops_when_all_played(self):
        from ui.graph import _drop_dnp
        df = pd.DataFrame({"MIN": [32, 28], "PTS": [24, 18]})
        kept, dropped = _drop_dnp(df, "PTS")
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 2)

    def test_falls_back_to_stat_nan_when_min_missing(self):
        from ui.graph import _drop_dnp
        import numpy as np
        df = pd.DataFrame({"PTS": [24, np.nan, 18]})
        kept, dropped = _drop_dnp(df, "PTS")
        self.assertEqual(dropped, 1)
        self.assertEqual(set(kept["PTS"]), {24, 18})

    def test_empty_df_is_safe(self):
        from ui.graph import _drop_dnp
        kept, dropped = _drop_dnp(pd.DataFrame(), "PTS")
        self.assertEqual(dropped, 0)
        self.assertTrue(kept.empty)

    def test_bar_value_labels(self):
        from ui.graph import _bar_value_label
        import numpy as np

        self.assertEqual(_bar_value_label(float("nan")), "")
        self.assertEqual(_bar_value_label(np.nan), "")
        self.assertEqual(_bar_value_label(18.0), "18")
        self.assertEqual(_bar_value_label(6.75), "6.75")
        self.assertEqual(_bar_value_label(7 - 2e-10), "7")

    def test_chart_tick_home_vs_away(self):
        from utils.formatting import format_nba_prop_chart_tick
        ts = pd.Timestamp("2026-05-01")
        self.assertEqual(
            format_nba_prop_chart_tick(
                chart_ts=ts, opponent_abbr="TOR", is_home=True,
            ),
            "05/01 vs TOR",
        )
        self.assertEqual(
            format_nba_prop_chart_tick(
                chart_ts=ts, opponent_abbr="DET", is_home=False,
            ),
            "05/01 @ DET",
        )

    def test_chart_tick_rejects_nat_timestamp(self):
        from utils.formatting import format_nba_prop_chart_tick
        with self.assertRaises(ValueError):
            format_nba_prop_chart_tick(
                chart_ts=pd.NaT,
                opponent_abbr="TOR",
                is_home=True,
            )

    def test_compact_axis_month_day_locale_stable(self):
        from utils.formatting import format_nba_prop_chart_axis_month, format_nba_prop_chart_axis_day
        ts = pd.Timestamp("2026-03-08")
        self.assertEqual(format_nba_prop_chart_axis_month(ts), "Mar")
        self.assertEqual(format_nba_prop_chart_axis_day(ts), "08")

    def test_matchup_line_home_away_unknown(self):
        from utils.formatting import format_nba_matchup_line
        self.assertEqual(format_nba_matchup_line("LAC", "ORL", True), "LAC vs ORL")
        self.assertEqual(format_nba_matchup_line("LAC", "ORL", False), "LAC @ ORL")
        self.assertEqual(format_nba_matchup_line("LAC", "ORL", None), "LAC · ORL")

    def test_infer_player_team_abbr_from_row(self):
        from utils.formatting import infer_player_team_abbr_from_row
        self.assertEqual(
            infer_player_team_abbr_from_row({"TEAM_ABBREVIATION": "bos"}),
            "BOS",
        )
        self.assertEqual(
            infer_player_team_abbr_from_row({"MATCHUP": "LAC vs. ORL"}),
            "LAC",
        )
        self.assertEqual(
            infer_player_team_abbr_from_row({"MATCHUP": "LAC @ DEN"}),
            "LAC",
        )

    def test_nba_logo_url_png(self):
        from utils.nba_logos import nba_team_logo_url_png
        url = nba_team_logo_url_png("ORL")
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("orl.png", url.lower())
        self.assertIsNone(nba_team_logo_url_png(""))
        self.assertIsNone(nba_team_logo_url_png(None))

    def test_prop_hover_html_matchup_stat_phrase(self):
        from utils.formatting import format_nba_prop_chart_hover_html
        h = format_nba_prop_chart_hover_html(
            date_display="2026-05-01",
            opponent="ORL",
            location="Away",
            matchup_display="LAC @ ORL",
            stat_phrase="24.0 points",
            minutes_display="34.0",
            prop_line=22.5,
            result_label="Over",
        )
        self.assertIn("<b>Matchup:</b> LAC @ ORL", h)
        self.assertIn("<b>Stat:</b> 24.0 points", h)

    def test_prop_points_rich_hover_from_row(self):
        from utils.prop_chart_hover import format_prop_research_chart_hover_html
        import pandas as pd
        row = pd.Series({
            "WL": "W",
            "PLUS_MINUS": -7.0,
            "TOOLTIP_TEAM_MARGIN": 5,
            "TOOLTIP_Q1_PF": 0,
            "MIN": 34.5,
            "PTS": 34.0,
            "FTM": 11, "FTA": 13, "FT_PCT": 0.846,
            "FG3M": 1, "FG3A": 4, "FG3_PCT": 0.25,
            "FGM": 11, "FGA": 23, "FG_PCT": 0.478,
            "PF": 2.0,
        })
        h = format_prop_research_chart_hover_html(
            row,
            stat_label="Points",
            stat_col="PTS",
            stat_value=34.0,
            prop_line=30.5,
            is_half_line=False,
            chart_ts=pd.Timestamp("2026-01-13"),
            opponent="SAS",
            location="Away",
            matchup_display="OKC @ SAS",
            minutes_display="34.5",
        )
        self.assertIn("01/13/26 vs SAS", h)
        self.assertIn("OKC @ SAS", h)
        self.assertIn("Won by 5", h)
        self.assertNotIn("on-court", h)
        self.assertIn("vs line", h)
        self.assertIn("30.5", h)
        self.assertIn("Over", h)
        self.assertIn("Minutes", h)
        self.assertIn("Points", h)
        self.assertIn("34", h)
        self.assertIn("FT Made", h)
        self.assertIn("3PT Made", h)
        self.assertIn("FG Made", h)
        self.assertIn("Fouls (1Q)", h)
        self.assertIn("(0)", h)
        self.assertNotIn("<table", h)
        self.assertNotIn("<div", h)
        self.assertNotIn("<td", h)

    def test_prop_rebounds_rich_hover_from_row(self):
        from utils.prop_chart_hover import format_prop_research_chart_hover_html
        import pandas as pd
        row = pd.Series({
            "WL": "W",
            "PLUS_MINUS": 3.0,
            "TOOLTIP_TEAM_MARGIN": 5,
            "TOOLTIP_Q1_PF": 0,
            "MIN": 35.0,
            "REB": 9.0,
            "REB_CHANCES_TOTAL": 14,
            "OREB": 1.0,
            "DREB": 8.0,
            "PF": 2.0,
        })
        h = format_prop_research_chart_hover_html(
            row,
            stat_label="Rebounds",
            stat_col="REB",
            stat_value=9.0,
            prop_line=8.5,
            is_half_line=False,
            chart_ts=pd.Timestamp("2026-05-11"),
            opponent="LAL",
            location="Away",
            matchup_display="GSW @ LAL",
            minutes_display="35.0",
        )
        self.assertIn("05/11/26 vs LAL", h)
        self.assertIn("GSW @ LAL", h)
        self.assertIn("Won by 5", h)
        self.assertIn("vs line", h)
        self.assertIn("8.5", h)
        self.assertIn("Minutes", h)
        self.assertIn("Rebounds", h)
        self.assertIn("9", h)
        self.assertIn("Rebound Chances", h)
        self.assertIn("14", h)
        self.assertIn("OREB", h)
        self.assertIn("DREB", h)
        self.assertIn("Fouls (1Q)", h)
        self.assertIn("2(0)", h)
        self.assertNotIn("<table", h)
        self.assertNotIn("<div", h)
        self.assertNotIn("<td", h)

    def test_prop_assists_rich_hover_from_row(self):
        from utils.prop_chart_hover import format_prop_research_chart_hover_html
        import pandas as pd
        row = pd.Series({
            "WL": "W",
            "PLUS_MINUS": 2.0,
            "TOOLTIP_TEAM_MARGIN": 5,
            "TOOLTIP_Q1_PF": 0,
            "MIN": 39.0,
            "AST": 8.0,
            "TOOLTIP_PASSES_MADE": 39,
            "TOOLTIP_POTENTIAL_AST": 9,
            "PF": 1.0,
        })
        h = format_prop_research_chart_hover_html(
            row,
            stat_label="Assists",
            stat_col="AST",
            stat_value=8.0,
            prop_line=6.5,
            is_half_line=False,
            chart_ts=pd.Timestamp("2026-05-11"),
            opponent="LAL",
            location="Away",
            matchup_display="GSW @ LAL",
            minutes_display="39.0",
        )
        self.assertIn("05/11/26 vs LAL", h)
        self.assertIn("GSW @ LAL", h)
        self.assertIn("Won by 5", h)
        self.assertIn("vs line", h)
        self.assertIn("6.5", h)
        self.assertIn("Minutes", h)
        self.assertIn("Assists", h)
        self.assertIn("Potential AST", h)
        self.assertIn("Passes Made", h)
        self.assertIn("Fouls (1Q)", h)
        self.assertIn("1(0)", h)
        self.assertNotIn("<table", h)

    def test_prop_threes_rich_hover_from_row(self):
        from utils.prop_chart_hover import format_prop_research_chart_hover_html
        import pandas as pd
        row = pd.Series({
            "WL": "L",
            "PLUS_MINUS": -15.0,
            "TOOLTIP_TEAM_MARGIN": -15,
            "TOOLTIP_Q1_PF": 0,
            "MIN": 36.0,
            "PTS": 20.0,
            "FG3M": 3.0,
            "FG3A": 7.0,
            "FG3_PCT": 0.429,
            "FTM": 5, "FTA": 7, "FT_PCT": 0.714,
            "FGM": 6, "FGA": 19, "FG_PCT": 0.316,
            "PF": 4.0,
            "TOOLTIP_DNP_FG3M": [
                {"name": "Miles McBride", "avg": 2.6},
                {"name": "Kevin McCullar Jr.", "avg": 0.4},
            ],
        })
        h = format_prop_research_chart_hover_html(
            row,
            stat_label="3PM",
            stat_col="FG3M",
            stat_value=3.0,
            prop_line=2.5,
            is_half_line=False,
            chart_ts=pd.Timestamp("2026-02-24"),
            opponent="CLE",
            location="Away",
            matchup_display="NYK @ CLE",
            minutes_display="36.0",
        )
        self.assertIn("02/24/26 @ CLE", h)
        self.assertIn("NYK @ CLE", h)
        self.assertIn("Lost by 15", h)
        self.assertIn("CL 2.5", h)
        self.assertIn("Over", h)
        self.assertIn("Minutes", h)
        self.assertTrue("36'" in h or "36&#x27;" in h)
        self.assertIn("Points", h)
        self.assertIn("20", h)
        self.assertIn("FT Made", h)
        self.assertIn("3PT Made", h)
        self.assertIn("3/7", h)
        self.assertIn("FG Made", h)
        self.assertIn("Fouls (1Q)", h)
        self.assertIn("4(0)", h)
        self.assertIn("DID NOT PLAY [AVG 3PT]", h)
        self.assertIn("M. MCBRIDE", h)
        self.assertIn("MCCULLAR JR.", h)
        self.assertIn("(2.6)", h)
        self.assertNotIn("<table", h)

    def test_dnp_teammates_for_game(self):
        from services.research_tooltip_enrichment import dnp_teammates_for_game
        import pandas as pd
        tm = pd.DataFrame([
            {
                "PLAYER": "Miles McBride",
                "GAME_IDS": {"0022500123", "0022500124"},
                "STAT_AVGS": {"FG3M": 2.6},
            },
            {
                "PLAYER": "Kevin McCullar Jr.",
                "GAME_IDS": {"0022500123"},
                "STAT_AVGS": {"FG3M": 0.4},
            },
        ])
        out = dnp_teammates_for_game(tm, "0022500999", stat_col="FG3M")
        self.assertEqual(len(out), 2)
        names = {e["name"] for e in out}
        self.assertIn("Miles McBride", names)

    def test_prop_non_points_hover_uses_legacy_lines(self):
        from utils.prop_chart_hover import format_prop_research_chart_hover_html
        import pandas as pd
        row = pd.Series({"MIN": 32.0, "STL": 2.0})
        h = format_prop_research_chart_hover_html(
            row,
            stat_label="Steals",
            stat_col="STL",
            stat_value=2.0,
            prop_line=1.5,
            is_half_line=False,
            chart_ts=pd.Timestamp("2026-05-01"),
            opponent="ORL",
            location="Away",
            matchup_display="LAC @ ORL",
            minutes_display="32.0",
        )
        self.assertIn("<b>Date:</b> 2026-05-01", h)
        self.assertIn("<b>Stat:</b>", h)

    def test_dedupe_compact_axis_month_day(self):
        from utils.formatting import dedupe_compact_axis_month_day
        mo, dy = dedupe_compact_axis_month_day(
            ["Mar", "Mar"],
            ["08", "08"],
            iso_dates=["2026-03-08", "2026-03-08"],
            opponents_upper=["ORL", "ORL"],
        )
        self.assertEqual(mo, ["Mar", "Mar"])
        self.assertEqual(dy, ["08", "08 (2)"])

    def test_nba_prop_yaxis_locked_at_zero_for_steals(self):
        from utils.nba_prop_chart_axes import nba_prop_chart_yaxis_config
        cfg = nba_prop_chart_yaxis_config(
            stat_col="STL", ys=[0.0, 1.0, 4.0], prop_line=20.0,
        )
        self.assertEqual(cfg["range"][0], 0.0)
        self.assertGreaterEqual(cfg["range"][1], 20.0)
        self.assertGreater(cfg["dtick"], 0)

    def test_nba_prop_yaxis_tight_for_low_counts(self):
        from utils.nba_prop_chart_axes import nba_prop_chart_yaxis_config
        cfg = nba_prop_chart_yaxis_config(
            stat_col="STL", ys=[1.0, 2.0, 3.0], prop_line=2.0,
        )
        self.assertEqual(cfg["range"][0], 0.0)
        self.assertLessEqual(cfg["range"][1], 8.0)

    def test_nba_prop_yaxis_all_zero(self):
        from utils.nba_prop_chart_axes import nba_prop_chart_yaxis_config
        cfg = nba_prop_chart_yaxis_config(
            stat_col="PTS", ys=[0.0, 0.0], prop_line=0.5,
        )
        self.assertEqual(cfg["range"][0], 0.0)
        self.assertGreaterEqual(cfg["range"][1], 0.5)

    def test_exclude_rows_without_chart_datetime(self):
        from ui.graph import exclude_rows_without_chart_datetime
        df = pd.DataFrame({
            "_chart_ts": [pd.Timestamp("2026-05-01"), pd.NaT],
            "PTS": [10.0, 99.0],
        })
        out, dropped = exclude_rows_without_chart_datetime(df)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["PTS"], 10.0)

    def test_parse_game_date_second_pass_coerces(self):
        from utils.dates import parse_game_date
        s = pd.Series(["May 1, 2026", "2026-05-03"])
        out = parse_game_date(s)
        self.assertFalse(bool(out.isna().any()))

    def test_game_date_compact_numeric_and_missing_comma_space(self):
        """YYYYMMDD floats / dotted strings must not decay to epoch garbage or NaT."""
        from utils.dates import coerce_game_datetime, parse_game_date

        self.assertEqual(
            coerce_game_datetime(20260418.0).strftime("%Y-%m-%d"), "2026-04-18",
        )
        self.assertEqual(
            coerce_game_datetime("20260418.0").strftime("%Y-%m-%d"), "2026-04-18",
        )
        self.assertEqual(
            coerce_game_datetime("Apr 29,2026").strftime("%Y-%m-%d"), "2026-04-29",
        )
        blend = pd.Series([20260418.0, "Apr 29,2026", "May 11, 2026"])
        out = parse_game_date(blend)
        self.assertEqual(out.isna().sum(), 0)
        self.assertEqual(str(out.iloc[0].date()), "2026-04-18")

    def test_repair_opponent_from_matchup(self):
        from ui.graph import repair_opponent_and_home_columns
        df = pd.DataFrame({
            "MATCHUP": ["DET vs. TOR", "DET @ CHI"],
            "OPPONENT": ["", ""],
            "IS_HOME": [True, False],
            "GAME_DATE": pd.to_datetime(["2026-05-01", "2026-05-03"]),
            "PTS": [20, 22],
        })
        fixed = repair_opponent_and_home_columns(df)
        self.assertEqual(list(fixed["OPPONENT"]), ["TOR", "CHI"])

    def test_dedupe_chart_ticks(self):
        from utils.formatting import dedupe_chart_tick_labels
        self.assertEqual(
            dedupe_chart_tick_labels(["05/01 vs TOR", "05/01 vs TOR"]),
            ["05/01 vs TOR", "05/01 vs TOR (2)"],
        )

    def test_game_id_sort_string(self):
        from utils.formatting import nba_game_id_sort_string
        self.assertEqual(len(nba_game_id_sort_string("0022400999")), 10)

    def test_coerce_game_datetime_may_month_string(self):
        from utils.dates import coerce_game_datetime
        ts = coerce_game_datetime("May 01, 2026")
        self.assertEqual(ts.strftime("%Y-%m-%d"), "2026-05-01")


class TestResearchBarChartHelpers(unittest.TestCase):
    """Sparse axis helpers plus game-count captions."""

    def test_sparse_ticks_include_chart_ends_and_cap(self):
        from utils.research_bar_chart import sparse_axis_tick_positions

        ts_list = pd.date_range("2024-10-01", periods=82, freq="D").tolist()
        idx = sparse_axis_tick_positions(ts_list, max_ticks=12)
        self.assertLessEqual(len(idx), 12)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], 81)

    def test_formatted_game_count_caption(self):
        from config import formatted_game_count_filter_caption

        self.assertIn("30", formatted_game_count_filter_caption("30"))
        self.assertEqual(formatted_game_count_filter_caption("Max"), "Max games")
        self.assertIn("42", formatted_game_count_filter_caption(
            "Custom", custom_n=42,
        ))


# ── 10. Teammate modal toggle semantics ──────────────────────────────────────


class TestTeammateModalTogglePill(unittest.TestCase):
    """`ui.teammate_modal.on_pill_click` is the single source of truth for
    WITH / WITHOUT transitions. Tests cover the three interesting paths."""

    def _state(self):
        from ui.filter_state import FilterState
        return FilterState()

    def test_toggles_off_when_clicking_same_target(self):
        from ui.teammate_modal import on_pill_click
        s = self._state()
        on_pill_click(s, 42, "with")
        self.assertEqual(s.teammate_modes[42], "with")
        on_pill_click(s, 42, "with")
        self.assertNotIn(42, s.teammate_modes)

    def test_switches_when_clicking_opposite_target(self):
        from ui.teammate_modal import on_pill_click
        s = self._state()
        on_pill_click(s, 42, "with")
        on_pill_click(s, 42, "without")
        self.assertEqual(s.teammate_modes[42], "without")
        # Switching again goes back to WITH — no "both-active" intermediate.
        on_pill_click(s, 42, "with")
        self.assertEqual(s.teammate_modes[42], "with")

    def test_ignores_invalid_target(self):
        from ui.teammate_modal import on_pill_click
        s = self._state()
        on_pill_click(s, 42, "bogus")
        self.assertEqual(s.teammate_modes, {})

    def test_subject_player_is_never_filtered(self):
        from ui.teammate_modal import on_pill_click
        s = self._state()
        on_pill_click(s, 7, "with", subject_player_id=7)
        self.assertEqual(s.teammate_modes, {})
        on_pill_click(s, 7, "without", subject_player_id=7)
        self.assertEqual(s.teammate_modes, {})
        # Other players still toggle when subject is specified.
        on_pill_click(s, 42, "with", subject_player_id=7)
        self.assertEqual(s.teammate_modes[42], "with")


# ── 11. status_chip behaviour ────────────────────────────────────────────────


class TestStatusChip(unittest.TestCase):
    def test_returns_empty_for_none(self):
        from ui.components import status_chip
        self.assertEqual(status_chip(None), "")

    def test_returns_empty_for_empty_string(self):
        from ui.components import status_chip
        self.assertEqual(status_chip(""), "")

    def test_returns_empty_for_active(self):
        from ui.components import status_chip
        # ACTIVE is the implicit default — no chip to keep rows tidy.
        self.assertEqual(status_chip("ACTIVE"), "")
        self.assertEqual(status_chip("active"), "")

    def test_returns_chip_for_out(self):
        from ui.components import status_chip
        html_out = status_chip("OUT")
        self.assertIn("OUT", html_out)
        self.assertIn("pp-tm-status-out", html_out)

    def test_gtd_chip_includes_compact_plus_prefix(self):
        from ui.components import status_chip
        html_out = status_chip("GTD")
        self.assertIn("GTD", html_out)
        self.assertIn("pp-tm-status-plus", html_out)


# ── 12. build_teammate_table sort order ──────────────────────────────────────


class TestBuildTeammateTable(unittest.TestCase):
    """Verify the teammate table is sorted by AVG_MIN desc with NaNs last.

    Fully mocked — no network or cache calls.
    """

    def test_sorts_by_avg_min_desc_with_nan_last(self):
        import pandas as pd
        from services import teammate_service

        roster = pd.DataFrame([
            {"PLAYER_ID": 1, "PLAYER": "Alpha",   "POSITION": "G", "NUM": "1"},
            {"PLAYER_ID": 2, "PLAYER": "Bravo",   "POSITION": "F", "NUM": "2"},
            {"PLAYER_ID": 3, "PLAYER": "Charlie", "POSITION": "C", "NUM": "3"},
            {"PLAYER_ID": 4, "PLAYER": "Delta",   "POSITION": "G", "NUM": "4"},
        ])

        summaries = {
            1: {"player_id": 1, "games": 10, "avg_min": 22.0,
                "stat_avgs": {"PTS": 12.0}, "game_ids": set()},
            2: {"player_id": 2, "games": 10, "avg_min": 35.5,
                "stat_avgs": {"PTS": 25.0}, "game_ids": set()},
            3: {"player_id": 3, "games":  0, "avg_min": float("nan"),
                "stat_avgs": {}, "game_ids": set()},
            4: {"player_id": 4, "games":  8, "avg_min": 28.2,
                "stat_avgs": {"PTS": 18.5}, "game_ids": set()},
        }

        with mock.patch.object(teammate_service, "list_teammates",
                               return_value=roster), \
             mock.patch.object(teammate_service, "fetch_teammate_summary",
                               side_effect=lambda pid, *a, **kw: summaries[int(pid)]):
            out = teammate_service.build_teammate_table(
                team_id=1610612737, season="2024-25",
                season_type="Regular Season",
                exclude_player_id=99,
            )

        # Bravo (35.5) first, Delta (28.2) second, Alpha (22.0) third,
        # Charlie (NaN) last.
        self.assertEqual(list(out["PLAYER"]), ["Bravo", "Delta", "Alpha", "Charlie"])
        self.assertAlmostEqual(float(out.iloc[0]["AVG_MIN"]), 35.5)
        # Charlie's AVG_MIN should be NaN and sorted to the bottom.
        import math as _math
        self.assertTrue(_math.isnan(float(out.iloc[-1]["AVG_MIN"])))
        # Stat avgs are stored per-column in STAT_AVGS dict (no market_col baked in).
        self.assertAlmostEqual(float(out.iloc[0]["STAT_AVGS"].get("PTS", 0)), 25.0)


# ── 12. Game Markets config + projection engine ─────────────────────────────


class TestGameMarketsConfig(unittest.TestCase):
    """Game Markets constants stay internally consistent."""

    def test_every_market_has_kind_and_needs_side(self):
        from config import (GAME_MARKETS, GAME_MARKET_KIND,
                            GAME_MARKET_NEEDS_SIDE, GAME_MARKET_DEFAULT_LINE)
        for m in GAME_MARKETS:
            self.assertIn(m, GAME_MARKET_KIND, f"missing KIND for {m}")
            self.assertIn(m, GAME_MARKET_NEEDS_SIDE, f"missing NEEDS_SIDE for {m}")
            self.assertIn(m, GAME_MARKET_DEFAULT_LINE, f"missing DEFAULT_LINE for {m}")
            self.assertIn(GAME_MARKET_KIND[m], ("total", "margin"))

    def test_half_shares_sum_to_one(self):
        """The beta heuristic halves must split 100 % of the full game."""
        from config import HALF_SCORING_SHARE, HALF_SPREAD_SHARE
        self.assertAlmostEqual(HALF_SCORING_SHARE["1H"] + HALF_SCORING_SHARE["2H"], 1.0)
        self.assertAlmostEqual(HALF_SPREAD_SHARE["1H"] + HALF_SPREAD_SHARE["2H"], 1.0)

    def test_home_court_adv_is_non_negative(self):
        from config import HOME_COURT_ADV
        self.assertGreaterEqual(HOME_COURT_ADV, 0.0)


def _make_pair_context(pace_a: float = 100.0, pace_b: float = 100.0,
                        league_pace: float = 100.0,
                        net_a: float = 3.0, net_b: float = -2.0,
                        a_off: float = 115.0, a_def: float = 110.0,
                        b_off: float = 112.0, b_def: float = 114.0,
                        home_team_id: int = 1, n_games: int = 10):
    """Build a deterministic PairContext for projection math tests.

    The advanced DataFrame is padded with 28 neutral "league average" rows
    so `_league_mean` returns `league_pace` rather than just the average of
    team A and team B. This keeps the pace-clamp test meaningful.
    """
    import pandas as pd
    from services.matchup_service import PairContext

    def _mkrow(pts_for: float, pts_against: float, is_home: bool, game_id: int):
        return {
            "GAME_ID": str(game_id), "TEAM_ID": 0, "GAME_DATE": pd.Timestamp("2025-01-01"),
            "MATCHUP": "AAA vs. BBB" if is_home else "AAA @ BBB",
            "WL": "W" if pts_for > pts_against else "L",
            "PTS": pts_for,
            "PTS_AGAINST": pts_against,
            "GAME_TOTAL": pts_for + pts_against,
            "MARGIN": pts_for - pts_against,
            "PLUS_MINUS": pts_for - pts_against,
            "IS_HOME": is_home, "IS_WIN": pts_for > pts_against,
            "OPPONENT": "BBB" if is_home else "AAA",
            "REST_DAYS": 1.0, "SEASON_TYPE": "Regular Season",
        }

    log_a = pd.DataFrame([_mkrow(a_off, a_def, i % 2 == 0, 100 + i)
                          for i in range(n_games)])
    log_b = pd.DataFrame([_mkrow(b_off, b_def, i % 2 == 1, 200 + i)
                          for i in range(n_games)])

    # Pad with 28 synthetic teams at the target league_pace so the mean of
    # the PACE column in the advanced frame equals league_pace regardless
    # of the two explicit team rows.
    filler = [{"TEAM_ID": 100 + i, "PACE": league_pace,
               "OFF_RATING": 110.0, "DEF_RATING": 110.0, "NET_RATING": 0.0}
              for i in range(28)]
    advanced = pd.DataFrame([
        {"TEAM_ID": 1, "PACE": pace_a, "OFF_RATING": a_off, "DEF_RATING": a_def, "NET_RATING": net_a},
        {"TEAM_ID": 2, "PACE": pace_b, "OFF_RATING": b_off, "DEF_RATING": b_def, "NET_RATING": net_b},
        *filler,
    ])
    # Re-derive league_pace precisely after adding the two explicit teams:
    # the test scenarios pass an intended league_pace and assume PACE mean
    # == league_pace, so overwrite the PACE column mean to match exactly.
    # (We do this by adjusting filler rows to compensate.)
    current_sum = pace_a + pace_b + league_pace * len(filler)
    target_mean = league_pace
    delta = target_mean * (len(filler) + 2) - current_sum
    if filler:
        # Spread any delta across filler rows so the DataFrame mean hits target.
        for i in range(len(filler)):
            advanced.loc[2 + i, "PACE"] += delta / len(filler)

    return PairContext(
        season="2024-25", season_type="Regular Season",
        team_a={"id": 1, "abbreviation": "AAA", "full_name": "Team A"},
        team_b={"id": 2, "abbreviation": "BBB", "full_name": "Team B"},
        log_a=log_a, log_b=log_b, recent_a=log_a, recent_b=log_b,
        advanced=advanced, home_team_id=home_team_id, is_stale=False,
    )


class TestGameMarketProjections(unittest.TestCase):
    """The projection math is small enough to pin down with exact values."""

    def test_full_game_total_formula_with_equal_pace(self):
        """proj = 0.5×(Aoff+Bdef) + 0.5×(Boff+Adef), pace_factor=1 when both match league."""
        from services import game_market_projection_service as svc
        ctx = _make_pair_context(
            pace_a=100.0, pace_b=100.0,
            a_off=115.0, a_def=110.0, b_off=112.0, b_def=114.0,
        )
        proj = svc.project_full_game_total(ctx, manual_line=200.0)
        expected = 0.5 * (115.0 + 114.0) + 0.5 * (112.0 + 110.0)
        self.assertAlmostEqual(proj.projection, expected, places=2)
        # Edge is projection minus line; lean follows.
        self.assertAlmostEqual(proj.edge, expected - 200.0, places=2)
        self.assertFalse(proj.beta)
        self.assertEqual(proj.kind, "total")
        self.assertGreater(len(proj.factors), 0)

    def test_full_game_total_pace_factor_clamps(self):
        """Extreme pace disparity is clamped to the configured ±10 % window."""
        from services import game_market_projection_service as svc
        # Pace ratio (140+140)/(2*100) = 1.40 → clamped to 1.10.
        ctx = _make_pair_context(pace_a=140.0, pace_b=140.0)
        proj = svc.project_full_game_total(ctx, manual_line=0.0)
        pace_factor = next(f.value for f in proj.factors if f.name == "pace_factor")
        self.assertAlmostEqual(pace_factor, 1.10, places=3)

    def test_team_total_uses_only_selected_side(self):
        from services import game_market_projection_service as svc
        ctx = _make_pair_context(a_off=120.0, a_def=110.0, b_off=100.0, b_def=108.0)
        proj_a = svc.project_team_total(ctx, manual_line=110.0, side_team_id=1)
        # proj for A ≈ 0.5 × (120 + 108) with pace factor = 1 → 114
        self.assertAlmostEqual(proj_a.projection, 0.5 * (120.0 + 108.0), places=2)
        self.assertEqual(proj_a.side_team_id, 1)
        proj_b = svc.project_team_total(ctx, manual_line=110.0, side_team_id=2)
        # proj for B ≈ 0.5 × (100 + 110) = 105
        self.assertAlmostEqual(proj_b.projection, 0.5 * (100.0 + 110.0), places=2)

    def test_full_game_spread_includes_home_court(self):
        """Home team gets +HOME_COURT_ADV; away team gets -HOME_COURT_ADV."""
        from config import HOME_COURT_ADV
        from services import game_market_projection_service as svc

        ctx_home = _make_pair_context(net_a=0.0, net_b=0.0, a_off=110, a_def=110,
                                       b_off=110, b_def=110, home_team_id=1)
        proj_home = svc.project_full_game_spread(ctx_home, manual_spread=-2.0,
                                                  side_team_id=1)
        # Net-ratings equal, recent-form delta zero, but +home_court for A.
        self.assertAlmostEqual(proj_home.projection, HOME_COURT_ADV, places=2)

        ctx_away = _make_pair_context(net_a=0.0, net_b=0.0, a_off=110, a_def=110,
                                       b_off=110, b_def=110, home_team_id=2)
        proj_away = svc.project_full_game_spread(ctx_away, manual_spread=-2.0,
                                                  side_team_id=1)
        # A is road → -HOME_COURT_ADV.
        self.assertAlmostEqual(proj_away.projection, -HOME_COURT_ADV, places=2)

    def test_spread_cover_convention(self):
        """Favourite at -4.5 covers when projected margin > 4.5.

        Uses recent-margin aligned with NET_RATING so the form adjustment
        is zero and the projection is purely net-rating + home-court.
        """
        from services import game_market_projection_service as svc
        # Team A: NET_RATING=6, recent margin also 6 → form adj = 0.
        # Team B: NET_RATING=0, recent margin also 0 → form adj = 0.
        # No home-court. Projection should be exactly 6.
        ctx = _make_pair_context(net_a=6.0, net_b=0.0,
                                  a_off=116.0, a_def=110.0,
                                  b_off=110.0, b_def=110.0,
                                  home_team_id=None)
        proj = svc.project_full_game_spread(ctx, manual_spread=-4.5, side_team_id=1)
        self.assertAlmostEqual(proj.projection, 6.0, places=2)
        self.assertEqual(proj.lean, "Covers")
        # The dog side at +4.5 should then NOT cover (projected to lose by 6).
        proj_dog = svc.project_full_game_spread(ctx, manual_spread=4.5, side_team_id=2)
        self.assertEqual(proj_dog.lean, "No cover")

    def test_half_markets_fall_back_to_beta_when_no_linescore(self):
        """Without TOTAL_1H columns, half totals are heuristic and beta=True."""
        from config import HALF_SCORING_SHARE
        from services import game_market_projection_service as svc
        ctx = _make_pair_context()
        proj = svc.project_half_total(ctx, manual_line=100.0, half="1H")
        self.assertTrue(proj.beta, "heuristic path must flag beta=True")
        # Should match share × full-game projection.
        full = svc.project_full_game_total(ctx, manual_line=0.0)
        self.assertAlmostEqual(proj.projection,
                                full.projection * HALF_SCORING_SHARE["1H"], places=2)

    def test_half_markets_use_real_data_when_present(self):
        """When TOTAL_1H has values, projection uses the real sample avg."""
        from services import game_market_projection_service as svc
        ctx = _make_pair_context()
        # Inject half-level data.
        ctx.log_a = ctx.log_a.copy()
        ctx.log_b = ctx.log_b.copy()
        ctx.log_a["TOTAL_1H"] = 110.0   # 110 per game for A's games
        ctx.log_b["TOTAL_1H"] = 108.0   # 108 for B's games
        proj = svc.project_half_total(ctx, manual_line=100.0, half="1H")
        self.assertFalse(proj.beta)
        # Avg across both teams' last-10 samples = 109.
        self.assertAlmostEqual(proj.projection, 109.0, places=2)

    def test_project_dispatcher_handles_every_market(self):
        from config import GAME_MARKETS
        from services import game_market_projection_service as svc
        ctx = _make_pair_context()
        for market in GAME_MARKETS:
            proj = svc.project(market, ctx, manual_line=5.0, side_team_id=1)
            self.assertEqual(proj.market, market)


# ── 13. Team service matchup parsing ────────────────────────────────────────


class TestTeamService(unittest.TestCase):
    def test_parse_matchup_home(self):
        from services.team_service import _parse_matchup
        opp, is_home = _parse_matchup("ATL vs. NYK")
        self.assertEqual(opp, "NYK")
        self.assertTrue(is_home)

    def test_parse_matchup_away(self):
        from services.team_service import _parse_matchup
        opp, is_home = _parse_matchup("ATL @ BOS")
        self.assertEqual(opp, "BOS")
        self.assertFalse(is_home)

    def test_parse_matchup_garbage(self):
        from services.team_service import _parse_matchup
        opp, is_home = _parse_matchup("unexpected")
        self.assertEqual(opp, "")
        self.assertFalse(is_home)


# ── 14. Matchup service — H2H selection ─────────────────────────────────────


class TestH2H(unittest.TestCase):
    def test_h2h_filters_by_abbr(self):
        import pandas as pd
        from services.matchup_service import h2h_games
        log = pd.DataFrame([
            {"OPPONENT": "NYK", "PTS": 100},
            {"OPPONENT": "BOS", "PTS":  95},
            {"OPPONENT": "NYK", "PTS": 110},
        ])
        out = h2h_games(log, "NYK")
        self.assertEqual(len(out), 2)
        self.assertTrue((out["OPPONENT"] == "NYK").all())

    def test_h2h_empty_when_no_matches(self):
        import pandas as pd
        from services.matchup_service import h2h_games
        log = pd.DataFrame([{"OPPONENT": "BOS", "PTS": 95}])
        self.assertEqual(len(h2h_games(log, "NYK")), 0)


class TestSpreadFormatting(unittest.TestCase):
    """Sportsbook-style helpers used by the Game Markets spread UI."""

    def test_format_team_margin_favourite(self):
        from utils.formatting import format_team_margin
        self.assertEqual(format_team_margin("TOR", -12.5), "TOR -12.5")

    def test_format_team_margin_dog(self):
        from utils.formatting import format_team_margin
        self.assertEqual(format_team_margin("TOR", 12.5), "TOR +12.5")

    def test_format_team_margin_pickem(self):
        from utils.formatting import format_team_margin
        self.assertEqual(format_team_margin("TOR", 0), "TOR PK")

    def test_format_team_margin_nan_returns_default(self):
        from utils.formatting import format_team_margin
        self.assertEqual(format_team_margin("TOR", float("nan")), "-")

    def test_interpretation_favourite_half_point(self):
        """-12.5 must win by 13 or more."""
        from utils.formatting import spread_interpretation
        text = spread_interpretation("TOR", -12.5)
        self.assertIn("TOR", text)
        self.assertIn("wins by 13 or more", text)
        self.assertNotIn("margin exceeds", text)

    def test_interpretation_underdog_half_point(self):
        """+12.5 loses by 12 or fewer, or wins outright."""
        from utils.formatting import spread_interpretation
        text = spread_interpretation("TOR", 12.5)
        self.assertIn("loses by 12 or fewer", text)
        self.assertIn("wins outright", text)

    def test_interpretation_integer_line_mentions_push(self):
        from utils.formatting import spread_interpretation
        fav = spread_interpretation("TOR", -12)
        self.assertIn("push on exactly 12", fav)
        dog = spread_interpretation("TOR", 12)
        self.assertIn("push on exactly 12", dog)

    def test_interpretation_half_window_phrase(self):
        from utils.formatting import spread_interpretation
        text = spread_interpretation("TOR", -4.5, market_window="1h")
        self.assertIn("in the 1st half", text)

    def test_interpretation_handles_nan(self):
        from utils.formatting import spread_interpretation
        self.assertEqual(spread_interpretation("TOR", float("nan")), "")


# ── CS2: PandaScore client URL / cache-key builders ──────────────────────────

class TestCS2PandaScoreClient(unittest.TestCase):
    """Pure unit tests for the PandaScore client — no network calls."""

    def test_build_cache_key_is_stable(self):
        from services.cs2.pandascore_client import build_cache_key
        k1 = build_cache_key("/csgo/matches/upcoming", {"per_page": 25})
        k2 = build_cache_key("/csgo/matches/upcoming", {"per_page": 25})
        self.assertEqual(k1, k2)

    def test_build_cache_key_varies_with_params(self):
        from services.cs2.pandascore_client import build_cache_key
        k1 = build_cache_key("/csgo/matches/upcoming", {"per_page": 25})
        k2 = build_cache_key("/csgo/matches/upcoming", {"per_page": 50})
        self.assertNotEqual(k1, k2)

    def test_flatten_params_serialises_iterables(self):
        from services.cs2.pandascore_client import _flatten_params
        out = _flatten_params({"a": 1, "filter[id]": [1, 2, 3], "nope": None})
        self.assertEqual(out["filter[id]"], "1,2,3")
        self.assertNotIn("nope", out)

    def test_request_raises_when_token_missing(self):
        import os
        from services.cs2.pandascore_client import (
            PandaScoreNotConfigured, request,
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANDASCORE_TOKEN", None)
            # Ensure no secrets bleed through: patch pandascore_token directly.
            with mock.patch("services.cs2.pandascore_client.pandascore_token",
                             return_value=None):
                with self.assertRaises(PandaScoreNotConfigured):
                    request("/csgo/matches/upcoming", ttl_seconds=60)


# ── CS2: provider URL builders ────────────────────────────────────────────────

class TestCS2Provider(unittest.TestCase):
    def test_game_path_prefix(self):
        from services.cs2.pandascore_provider import _game_path
        self.assertTrue(_game_path("/teams").startswith("/"))
        self.assertIn("teams", _game_path("/teams"))

    def test_search_teams_empty_query_short_circuits(self):
        from services.cs2 import pandascore_provider as ps
        # Empty query should never hit the network.
        with mock.patch("services.cs2.pandascore_provider.request") as req:
            self.assertEqual(ps.search_teams(""), [])
            req.assert_not_called()

    def test_search_players_empty_query_short_circuits(self):
        from services.cs2 import pandascore_provider as ps
        with mock.patch("services.cs2.pandascore_provider.request") as req:
            self.assertEqual(ps.search_players("   "), [])
            req.assert_not_called()

    def test_list_upcoming_calls_correct_path(self):
        from services.cs2 import pandascore_provider as ps
        with mock.patch("services.cs2.pandascore_provider.request",
                          return_value=[]) as req:
            ps.list_upcoming_matches(per_page=10)
            req.assert_called_once()
            path = req.call_args[0][0]
            self.assertIn("/matches/upcoming", path)


# ── CS2: data provider normalisation ─────────────────────────────────────────

class TestCS2DataProviderNormalisation(unittest.TestCase):
    def test_normalize_match_flattens_opponents(self):
        from services.cs2.cs2_data_provider import normalize_match
        raw = {
            "id": 1,
            "status": "finished",
            "begin_at": "2026-04-20T12:00:00Z",
            "number_of_games": 3,
            "opponents": [
                {"opponent": {"id": 10, "name": "Team A", "acronym": "AAA"}},
                {"opponent": {"id": 20, "name": "Team B", "acronym": "BBB"}},
            ],
            "results": [{"team_id": 10, "score": 2},
                         {"team_id": 20, "score": 1}],
            "games": [
                {"id": 100, "position": 1, "status": "finished",
                 "finished": True, "map": {"name": "de_mirage"},
                 "winner": {"id": 10}, "rounds": []},
            ],
            "winner_id": 10,
            "tournament": {"name": "Major"},
        }
        normalised = normalize_match(raw)
        self.assertEqual(len(normalised["opponents"]), 2)
        self.assertEqual(normalised["opponents"][0]["id"], 10)
        self.assertEqual(normalised["games"][0]["map"], "Mirage")
        self.assertEqual(normalised["games"][0]["winner_id"], 10)
        self.assertEqual(normalised["tournament"], "Major")
        # passthrough keys added by the honest-refactor: games must have
        # players/stats/results keys so the player service can use them.
        self.assertIn("players", normalised["games"][0])
        self.assertIn("stats", normalised["games"][0])
        self.assertIn("results", normalised["games"][0])

    def test_normalize_handles_empty(self):
        from services.cs2.cs2_data_provider import normalize_match, normalize_team
        self.assertEqual(normalize_match(None), {})
        self.assertEqual(normalize_team(None), {})


# ── CS2: constants / helpers ─────────────────────────────────────────────────

class TestCS2Constants(unittest.TestCase):
    def test_normalize_map_name_strips_prefix(self):
        from services.cs2.constants import normalize_map_name
        self.assertEqual(normalize_map_name("de_mirage"), "Mirage")
        self.assertEqual(normalize_map_name("Dust 2"), "Dust2")
        self.assertEqual(normalize_map_name("Nuke"), "Nuke")
        self.assertEqual(normalize_map_name(None), "")

    def test_markets_all_have_required_fields(self):
        from services.cs2.constants import CS2_MARKETS
        for m in CS2_MARKETS:
            self.assertIn("key", m)
            self.assertIn("label", m)
            self.assertIn("kind", m)
            self.assertIn("needs_hltv", m)


# ── CS2: match service aggregation ───────────────────────────────────────────

class TestCS2MatchService(unittest.TestCase):
    def _mk_match(self, *, winner_id: int, opps: list[int],
                  a_score: int, b_score: int,
                  games: list[dict] | None = None) -> dict:
        return {
            "id": 1, "status": "finished", "winner_id": winner_id,
            "opponents": [{"id": opps[0], "name": "A", "acronym": "A"},
                            {"id": opps[1], "name": "B", "acronym": "B"}],
            "results": [{"team_id": opps[0], "score": a_score},
                          {"team_id": opps[1], "score": b_score}],
            "games": games or [],
        }

    def test_compute_team_form_counts_wins_and_maps(self):
        from services.cs2 import cs2_match_service as svc
        matches = [
            self._mk_match(winner_id=10, opps=[10, 20], a_score=2, b_score=1),
            self._mk_match(winner_id=20, opps=[10, 20], a_score=0, b_score=2),
            self._mk_match(winner_id=10, opps=[10, 30], a_score=2, b_score=0),
        ]
        form = svc.compute_team_form(matches, team_id=10)
        self.assertEqual(form.wins, 2)
        self.assertEqual(form.losses, 1)
        self.assertEqual(form.maps_won, 4)
        self.assertEqual(form.maps_lost, 3)
        self.assertAlmostEqual(form.win_pct, 2 / 3, places=5)

    def test_head_to_head_filters_opponent(self):
        from services.cs2 import cs2_match_service as svc
        a = self._mk_match(winner_id=10, opps=[10, 20], a_score=2, b_score=1)
        b = self._mk_match(winner_id=30, opps=[10, 30], a_score=1, b_score=2)
        only_b = svc.head_to_head([a, b], 20)
        self.assertEqual(len(only_b), 1)
        self.assertEqual(only_b[0], a)

    def test_map_pool_summary_counts_wins(self):
        from services.cs2 import cs2_match_service as svc
        games = [
            {"finished": True, "map": "Mirage", "winner_id": 10},
            {"finished": True, "map": "Mirage", "winner_id": 20},
            {"finished": True, "map": "Nuke",   "winner_id": 10},
            {"finished": False, "map": "Inferno", "winner_id": 10},
        ]
        m = self._mk_match(winner_id=10, opps=[10, 20],
                            a_score=2, b_score=1, games=games)
        summary = svc.map_pool_summary([m], team_id=10)
        self.assertEqual(summary["Mirage"]["games"], 2)
        self.assertEqual(summary["Mirage"]["wins"], 1)
        self.assertAlmostEqual(summary["Mirage"]["win_pct"], 0.5)
        self.assertEqual(summary["Nuke"]["wins"], 1)
        self.assertNotIn("Inferno", summary)  # unfinished ignored


# ── CS2: projection engine ───────────────────────────────────────────────────

class TestCS2ProjectionService(unittest.TestCase):
    def _mk_form(self, *, team_id=1, wins=6, losses=4, maps_won=14,
                  maps_lost=10) -> "object":
        from services.cs2.cs2_match_service import TeamForm
        return TeamForm(team_id=team_id, wins=wins, losses=losses,
                         maps_won=maps_won, maps_lost=maps_lost)

    def test_match_winner_favours_stronger_team(self):
        from services.cs2 import cs2_projection_service as p
        strong = self._mk_form(team_id=1, wins=9, losses=1,
                                maps_won=19, maps_lost=5)
        weak = self._mk_form(team_id=2, wins=2, losses=8,
                               maps_won=5, maps_lost=17)
        proj = p.project_match_winner(strong, weak, team_a_name="A",
                                         team_b_name="B")
        self.assertGreater(proj.probability, 0.6)
        self.assertEqual(proj.lean, "A")
        self.assertTrue(any("match_win_rate_diff" == f.name for f in proj.factors))

    def test_match_winner_thin_sample_pulls_toward_even(self):
        from services.cs2 import cs2_projection_service as p
        strong = self._mk_form(team_id=1, wins=2, losses=0,
                                maps_won=4, maps_lost=1)
        weak = self._mk_form(team_id=2, wins=0, losses=2,
                               maps_won=1, maps_lost=4)
        proj = p.project_match_winner(strong, weak, team_a_name="A",
                                         team_b_name="B")
        # Sample size 2 means regularisation pulls toward 0.5.
        self.assertLess(proj.probability, 0.80)
        self.assertTrue(any(f.name == "sample_regularisation"
                            for f in proj.factors))

    def test_total_maps_bo3_between_2_and_3(self):
        from services.cs2 import cs2_projection_service as p
        proj = p.project_total_maps(0.5, best_of=3, manual_line=2.5)
        self.assertGreaterEqual(proj.projection, 2.0)
        self.assertLessEqual(proj.projection, 3.0)
        self.assertFalse(math.isnan(proj.edge))

    def test_total_maps_bo1_is_beta(self):
        from services.cs2 import cs2_projection_service as p
        proj = p.project_total_maps(0.7, best_of=1, manual_line=None)
        self.assertTrue(proj.beta)
        self.assertEqual(proj.projection, 1.0)

    def test_total_rounds_fallback_when_no_sample(self):
        from services.cs2 import cs2_projection_service as p
        proj = p.project_total_rounds_map({}, "Mirage", manual_line=26.5)
        self.assertTrue(proj.beta)
        self.assertEqual(proj.projection, 24.0)

    def test_total_rounds_uses_sample_average(self):
        from services.cs2 import cs2_projection_service as p
        pool = {"Mirage": {"games": 12, "avg_total_rounds": 25.3,
                             "rounds_sample": 10}}
        proj = p.project_total_rounds_map(pool, "Mirage", manual_line=26.5)
        self.assertFalse(proj.beta)
        self.assertAlmostEqual(proj.projection, 25.3, places=2)
        self.assertEqual(proj.lean, "Under")

    def test_player_kills_without_sample_is_beta(self):
        from services.cs2 import cs2_projection_service as p
        empty = {"has_stats": False, "sample_size": 0,
                  "kills_avg": None, "maps": []}
        proj = p.project_player_kills(empty, manual_line=18.5)
        self.assertTrue(proj.beta)
        self.assertTrue(math.isnan(proj.projection))

    def test_player_kills_uses_base_then_blends_map(self):
        from services.cs2 import cs2_projection_service as p
        sample = {
            "has_stats": True, "sample_size": 8, "kills_avg": 20.0,
            "deaths_avg": 14.0, "assists_avg": 3.0, "headshots_avg": 9.0,
            "hs_pct_avg": 0.45, "kd_avg": 20 / 14,
            "maps": [
                {"kills": 25, "deaths": 12, "assists": 3, "headshots": 10,
                 "map": "Mirage", "winner_id": 1},
                {"kills": 23, "deaths": 13, "assists": 4, "headshots": 10,
                 "map": "Mirage", "winner_id": 1},
                {"kills": 15, "deaths": 16, "assists": 2, "headshots": 5,
                 "map": "Nuke", "winner_id": 2},
                {"kills": 17, "deaths": 15, "assists": 3, "headshots": 6,
                 "map": "Nuke", "winner_id": 2},
            ],
        }
        # No map focus → projection equals base.
        proj = p.project_player_kills(sample, manual_line=18.5)
        self.assertAlmostEqual(proj.projection, 20.0, places=5)
        # Mirage focus → blends toward Mirage average (24).
        proj_m = p.project_player_kills(sample, manual_line=18.5,
                                          selected_map="Mirage")
        self.assertGreater(proj_m.projection, 20.0)
        self.assertLessEqual(proj_m.projection, 24.0)

    def test_player_headshots_uses_hs_avg(self):
        from services.cs2 import cs2_projection_service as p
        sample = {
            "has_stats": True, "sample_size": 6, "kills_avg": 20.0,
            "headshots_avg": 9.0, "hs_pct_avg": 0.45,
            "maps": [], "deaths_avg": None, "assists_avg": None,
        }
        proj = p.project_player_headshots(sample, manual_line=8.5)
        self.assertAlmostEqual(proj.projection, 9.0, places=5)
        self.assertEqual(proj.lean, "Over")


# ── CS2: filter state round-trip ─────────────────────────────────────────────

class TestCS2FilterState(unittest.TestCase):
    def test_filter_state_defaults(self):
        from ui.cs2_filters import CS2FilterState
        s = CS2FilterState()
        self.assertEqual(s.sample_n, 10)
        self.assertIsNone(s.selected_map)
        self.assertEqual(s.best_of, 3)


# ── CS2: refined client resilience helpers ────────────────────────────────────

class TestCS2ClientResilience(unittest.TestCase):
    """Covers the post-refinement behaviours:
      - _mark_stale sets the flag on dict OR on every dict element of a list
      - _parse_retry_after clamps and handles bad values
      - 401/403 short-circuits into PandaScoreNotConfigured (never a retry
        or circuit-breaker trip)
    """

    def test_mark_stale_on_dict(self):
        from services.cs2.pandascore_client import _mark_stale
        d = {"id": 1}
        _mark_stale(d)
        self.assertTrue(d["_stale"])

    def test_mark_stale_on_list_of_dicts(self):
        from services.cs2.pandascore_client import _mark_stale
        lst = [{"id": 1}, {"id": 2}, "oops"]  # non-dict entry ignored
        _mark_stale(lst)
        self.assertTrue(lst[0]["_stale"])
        self.assertTrue(lst[1]["_stale"])
        self.assertEqual(lst[2], "oops")

    def test_parse_retry_after_clamps_and_defaults(self):
        from services.cs2.pandascore_client import _parse_retry_after
        self.assertEqual(_parse_retry_after("5"), 5.0)
        self.assertEqual(_parse_retry_after("0"), 0.5)     # clamp low
        self.assertEqual(_parse_retry_after("999"), 30.0)  # clamp high
        self.assertEqual(_parse_retry_after(None), 1.0)
        self.assertEqual(_parse_retry_after("garbage"), 1.0)

    def test_401_short_circuits_into_not_configured(self):
        from services.cs2.pandascore_client import (
            PandaScoreNotConfigured, request,
        )
        fake_resp = mock.Mock()
        fake_resp.status_code = 401
        fake_resp.headers = {}

        with mock.patch("services.cs2.pandascore_client.pandascore_token",
                         return_value="fake-token"), \
             mock.patch("services.cs2.pandascore_client.cache_db.get_cached",
                         return_value=None), \
             mock.patch("services.cs2.pandascore_client.requests.get",
                         return_value=fake_resp) as req, \
             mock.patch("services.cs2.pandascore_client._cb_register_failure") as cb, \
             mock.patch("services.cs2.pandascore_client._record_failure") as rec:
            with self.assertRaises(PandaScoreNotConfigured):
                request("/csgo/matches/upcoming", ttl_seconds=60,
                        allow_stale=False)
        self.assertEqual(req.call_count, 1, "401 must not trigger retries")
        cb.assert_not_called()
        rec.assert_not_called()


# ── CS2: UI helpers for stale-flag + map-default (pure functions) ────────────

class TestCS2PageHelpers(unittest.TestCase):
    def test_payload_is_stale_dict(self):
        from ui.cs2_page import _payload_is_stale
        self.assertTrue(_payload_is_stale({"id": 1, "_stale": True}))
        self.assertFalse(_payload_is_stale({"id": 1}))

    def test_payload_is_stale_list(self):
        from ui.cs2_page import _payload_is_stale
        self.assertTrue(_payload_is_stale(
            [{"id": 1}, {"id": 2, "_stale": True}]))
        self.assertFalse(_payload_is_stale([{"id": 1}, {"id": 2}]))
        self.assertFalse(_payload_is_stale([]))

    def test_default_map_index_prefers_filter(self):
        from ui.cs2_page import _default_map_index
        self.assertEqual(_default_map_index(["A", "B", "C"], "B"), 1)
        self.assertEqual(_default_map_index(["A", "B", "C"], None), 0)
        self.assertEqual(_default_map_index(["A", "B", "C"], "Z"), 0)

    def test_filter_matches_by_map(self):
        from ui.cs2_page import _filter_matches_by_map
        matches = [
            {"id": 1, "games": [{"map": "Mirage"}, {"map": "Nuke"}]},
            {"id": 2, "games": [{"map": "Inferno"}]},
            {"id": 3, "games": []},
        ]
        self.assertEqual(
            [m["id"] for m in _filter_matches_by_map(matches, "Mirage")],
            [1],
        )
        self.assertEqual(
            [m["id"] for m in _filter_matches_by_map(matches, None)],
            [1, 2, 3],
        )


# ── CS2: capability registry ──────────────────────────────────────────────────

class TestCS2Capabilities(unittest.TestCase):
    """Verify the capability layer returns correct types and doesn't throw."""

    def test_supported_known_true_key(self):
        from services.cs2.cs2_capabilities import supported
        self.assertTrue(supported("match_schedule"))
        self.assertTrue(supported("team_profile"))
        self.assertTrue(supported("player_profile"))

    def test_supported_known_false_key(self):
        from services.cs2.cs2_capabilities import supported
        # Not exposed by either HLTV (primary) or GRID:
        self.assertFalse(supported("round_totals"))
        self.assertFalse(supported("kast"))
        self.assertFalse(supported("side_splits"))
        self.assertFalse(supported("pistol_rounds"))
        # HLTV (the primary provider today) exposes these:
        self.assertTrue(supported("map_level_player_stats"))
        self.assertTrue(supported("player_kills_projection"))
        self.assertTrue(supported("adr"))
        # Tournament listings are GRID-only (experimental); HLTV schema has
        # no event list, so with HLTV primary this should be False.
        self.assertFalse(supported("tournament_list"))

    def test_supported_unknown_key_defaults_false(self):
        from services.cs2.cs2_capabilities import supported
        self.assertFalse(supported("does_not_exist"))
        self.assertFalse(supported(""))

    def test_notice_returns_string_for_known_key(self):
        from services.cs2.cs2_capabilities import notice
        msg = notice("map_level_player_stats")
        self.assertIsInstance(msg, str)
        self.assertGreater(len(msg), 20)

    def test_notice_returns_default_for_unknown_key(self):
        from services.cs2.cs2_capabilities import notice
        msg = notice("__invented_key__")
        self.assertIsInstance(msg, str)
        # The default message says "not available" rather than "unavailable".
        self.assertTrue(
            "unavailable" in msg.lower() or "not available" in msg.lower(),
            f"Expected availability wording in notice, got: {msg!r}",
        )

    def test_all_supported_returns_only_supported(self):
        from services.cs2.cs2_capabilities import all_supported, supported
        for key in all_supported():
            self.assertTrue(supported(key),
                            f"{key} in all_supported() but supported() is False")

    def test_all_unsupported_returns_only_unsupported(self):
        from services.cs2.cs2_capabilities import all_unsupported, supported
        for key in all_unsupported():
            self.assertFalse(supported(key),
                             f"{key} in all_unsupported() but supported() is True")


# ── CS2: normalize_games passthrough ─────────────────────────────────────────

class TestCS2NormalizeGamesPassthrough(unittest.TestCase):
    """_normalize_games must now forward players/stats/results so that
    cs2_player_service can find per-map stats when PandaScore provides them."""

    _RAW_GAME = {
        "id": 42,
        "position": 1,
        "status": "finished",
        "finished": True,
        "map": {"name": "de_mirage"},
        "winner": {"id": 10},
        "rounds": [{"winner_team": 10}, {"winner_team": 20}],
        "players": [{"player": {"id": 99}, "kills": 25, "deaths": 14}],
        "stats": {"something": "extra"},
        "results": [{"team_id": 10, "score": 13},
                    {"team_id": 20, "score": 10}],
    }

    def test_passthrough_keys_present(self):
        from services.cs2.cs2_data_provider import _normalize_games
        normed = _normalize_games([self._RAW_GAME])
        self.assertEqual(len(normed), 1)
        g = normed[0]
        self.assertIn("players", g, "players key must be forwarded")
        self.assertIn("stats", g, "stats key must be forwarded")
        self.assertIn("results", g, "results key must be forwarded")

    def test_passthrough_values_correct(self):
        from services.cs2.cs2_data_provider import _normalize_games
        normed = _normalize_games([self._RAW_GAME])
        g = normed[0]
        self.assertEqual(len(g["players"]), 1)
        self.assertEqual(g["players"][0]["kills"], 25)
        self.assertEqual(g["stats"], {"something": "extra"})
        self.assertEqual(len(g["results"]), 2)

    def test_existing_keys_still_correct(self):
        from services.cs2.cs2_data_provider import _normalize_games
        normed = _normalize_games([self._RAW_GAME])
        g = normed[0]
        self.assertEqual(g["map"], "Mirage")
        self.assertEqual(g["winner_id"], 10)
        self.assertTrue(g["finished"])
        self.assertEqual(len(g["rounds"]), 2)

    def test_missing_passthrough_keys_default_empty(self):
        from services.cs2.cs2_data_provider import _normalize_games
        minimal = {"id": 1, "finished": True, "map": "Mirage"}
        normed = _normalize_games([minimal])
        g = normed[0]
        self.assertEqual(g["players"], [])
        self.assertEqual(g["stats"], {})
        self.assertEqual(g["results"], [])


# ── CS2: compute_team_form status guard ──────────────────────────────────────

class TestCS2TeamFormStatusGuard(unittest.TestCase):
    """After the refactor, compute_team_form accepts only 'finished' and
    'post_game' matches; all other statuses are skipped."""

    def _make_match(self, status: str, winner_id: int = 10) -> dict:
        return {
            "id": 1, "status": status, "winner_id": winner_id,
            "results": [{"team_id": 10, "score": 2},
                        {"team_id": 20, "score": 0}],
            "games": [],
        }

    def test_finished_matches_counted(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match("finished", winner_id=10),
             self._make_match("finished", winner_id=20)],
            team_id=10,
        )
        self.assertEqual(form.wins, 1)
        self.assertEqual(form.losses, 1)

    def test_post_game_matches_counted(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match("post_game", winner_id=10)],
            team_id=10,
        )
        self.assertEqual(form.wins, 1)

    def test_running_matches_skipped(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match("running", winner_id=10)],
            team_id=10,
        )
        self.assertEqual(form.games, 0)

    def test_not_started_matches_skipped(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match("not_started", winner_id=10)],
            team_id=10,
        )
        self.assertEqual(form.games, 0)

    def test_canceled_matches_skipped(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match("canceled", winner_id=10)],
            team_id=10,
        )
        self.assertEqual(form.games, 0)

    def test_none_status_skipped(self):
        """status=None should now be skipped (old code accepted it)."""
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form(
            [self._make_match(None, winner_id=10)],
            team_id=10,
        )
        self.assertEqual(form.games, 0)

    def test_empty_dict_skipped(self):
        from services.cs2.cs2_match_service import compute_team_form
        form = compute_team_form([{}], team_id=10)
        self.assertEqual(form.games, 0)

    def test_mixed_statuses(self):
        from services.cs2.cs2_match_service import compute_team_form
        matches = [
            self._make_match("finished",   winner_id=10),
            self._make_match("running",    winner_id=10),
            self._make_match("post_game",  winner_id=20),
            self._make_match("not_started", winner_id=10),
            self._make_match("canceled",   winner_id=10),
        ]
        form = compute_team_form(matches, team_id=10)
        self.assertEqual(form.wins, 1)   # only "finished" win
        self.assertEqual(form.losses, 1) # only "post_game" loss
        self.assertEqual(form.games, 2)


# ── CS2: HLTV cache service ───────────────────────────────────────────────────

class TestCS2HLTVCacheService(unittest.TestCase):
    """Disk cache service: set/get/stale/invalidate round-trips."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        from services.cs2 import cs2_cache_service as cs
        cs.init(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_roundtrip_simple_payload(self):
        from services.cs2.cs2_cache_service import set_cached, get_cached
        set_cached("test:key", {"ok": True}, ttl_seconds=60)
        result = get_cached("test:key", 60)
        self.assertEqual(result, {"ok": True})

    def test_expired_cache_returns_none(self):
        from services.cs2.cs2_cache_service import set_cached, get_cached
        set_cached("test:expired", {"val": 1}, ttl_seconds=1)
        import time
        time.sleep(1.1)
        result = get_cached("test:expired", 1)
        self.assertIsNone(result)

    def test_get_stale_returns_payload_regardless_of_ttl(self):
        from services.cs2.cs2_cache_service import set_cached, get_stale
        import time
        set_cached("test:stale", [1, 2, 3], ttl_seconds=0)
        time.sleep(0.1)
        result = get_stale("test:stale")
        self.assertEqual(result, [1, 2, 3])

    def test_invalidate_removes_entry(self):
        from services.cs2.cs2_cache_service import set_cached, get_cached, invalidate
        set_cached("test:todel", {"x": 1}, ttl_seconds=3600)
        self.assertIsNotNone(get_cached("test:todel", 3600))
        invalidate("test:todel")
        self.assertIsNone(get_cached("test:todel", 3600))

    def test_cache_stats_counts_entries(self):
        from services.cs2.cs2_cache_service import set_cached, cache_stats
        set_cached("a", {"a": 1}, ttl_seconds=60)
        set_cached("b", {"b": 2}, ttl_seconds=60)
        stats = cache_stats()
        self.assertGreaterEqual(stats["entry_count"], 2)
        self.assertGreater(stats["total_kb"], 0)

    def test_none_ttl_never_expires(self):
        from services.cs2.cs2_cache_service import set_cached, get_cached
        set_cached("perm", {"permanent": True}, ttl_seconds=None)
        result = get_cached("perm", None)
        self.assertEqual(result, {"permanent": True})


# ── CS2: HLTV models ─────────────────────────────────────────────────────────

class TestCS2Models(unittest.TestCase):
    def test_match_result_winner_by_score(self):
        from services.cs2.models import CS2MatchResult, CS2Team
        t1 = CS2Team(id=1, name="Alpha")
        t2 = CS2Team(id=2, name="Beta")
        r = CS2MatchResult(team1=t1, team2=t2, score_t1=2, score_t2=1)
        self.assertEqual(r.winner, t1)

    def test_match_result_winner_none_when_tied(self):
        from services.cs2.models import CS2MatchResult, CS2Team
        r = CS2MatchResult(
            team1=CS2Team(id=1, name="A"),
            team2=CS2Team(id=2, name="B"),
            score_t1=1, score_t2=1,
        )
        self.assertIsNone(r.winner)

    def test_match_result_winner_none_when_no_score(self):
        from services.cs2.models import CS2MatchResult, CS2Team
        r = CS2MatchResult(team1=CS2Team(id=1), team2=CS2Team(id=2))
        self.assertIsNone(r.winner)

    def test_team_map_stat_compute_win_rate(self):
        from services.cs2.models import CS2TeamMapStat
        s = CS2TeamMapStat(map_name="Mirage", wins=8, losses=4)
        s.compute_win_rate()
        self.assertAlmostEqual(s.win_rate, 8 / 12, places=4)

    def test_source_status_mode_label(self):
        from services.cs2.models import CS2SourceStatus
        s = CS2SourceStatus(mode="cooldown")
        self.assertIn("cooldown", s.mode_label.lower())

    def test_source_status_emoji(self):
        from services.cs2.models import CS2SourceStatus
        self.assertEqual(CS2SourceStatus(mode="live").mode_emoji, "🟢")
        self.assertEqual(CS2SourceStatus(mode="stale").mode_emoji, "🟠")


# ── CS2: HLTV provider bridge ─────────────────────────────────────────────────

class TestCS2HLTVProvider(unittest.TestCase):
    """Unit tests for hltv_provider — no network calls."""

    def test_bridge_installed_check(self):
        from services.cs2 import hltv_provider as hp
        # Just check it returns bool without error.
        result = hp.bridge_installed()
        self.assertIsInstance(result, bool)

    def test_circuit_breaker_starts_closed(self):
        from services.cs2 import hltv_provider as hp
        hp.reset_circuit_breaker()
        self.assertFalse(hp._cb_is_open())

    def test_cb_opens_after_threshold_failures(self):
        from services.cs2 import hltv_provider as hp
        import time
        hp.reset_circuit_breaker()
        old_thresh = hp.CB_THRESHOLD
        hp.CB_THRESHOLD = 2
        try:
            hp._cb_register_failure()
            hp._cb_register_failure()
            self.assertTrue(hp._cb_is_open())
        finally:
            hp.CB_THRESHOLD = old_thresh
            hp.reset_circuit_breaker()

    def test_reset_clears_failures(self):
        from services.cs2 import hltv_provider as hp
        hp._cb_register_failure()
        hp.reset_circuit_breaker()
        self.assertFalse(hp._cb_is_open())

    def test_get_status_returns_dict(self):
        from services.cs2 import hltv_provider as hp
        status = hp.get_status()
        self.assertIn("provider", status)
        self.assertIn("mode", status)
        self.assertIn("bridge_installed", status)
        self.assertIn("throttle_s", status)


# ── CS2: mock provider ────────────────────────────────────────────────────────

class TestCS2MockProvider(unittest.TestCase):
    def test_get_matches_returns_list(self):
        from services.cs2 import mock_provider as mp
        matches = mp.get_matches()
        self.assertIsInstance(matches, list)
        self.assertGreater(len(matches), 0)

    def test_get_results_returns_list(self):
        from services.cs2 import mock_provider as mp
        results = mp.get_results()
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    def test_get_results_stars_filter(self):
        from services.cs2 import mock_provider as mp
        all_r = mp.get_results(stars=0)
        high_r = mp.get_results(stars=5)
        self.assertGreaterEqual(len(all_r), len(high_r))

    def test_get_player_stats_has_overview(self):
        from services.cs2 import mock_provider as mp
        s = mp.get_player_stats(7998)
        self.assertIn("overviewStatistics", s)
        self.assertIn("matches", s)

    def test_get_team_has_players(self):
        from services.cs2 import mock_provider as mp
        t = mp.get_team(4608)
        self.assertIn("players", t)
        self.assertGreater(len(t["players"]), 0)


# ── CS2: data provider normalisation (HLTV shapes) ───────────────────────────

class TestCS2HLTVNormalisation(unittest.TestCase):
    """cs2_data_provider normalises HLTV response shapes correctly."""

    def test_norm_match_result_from_hltv_result(self):
        from services.cs2.cs2_data_provider import _norm_match_result
        raw = {
            "id": 9876,
            "date": 1714000000000,
            "team1": {"id": 1, "name": "Alpha", "logo": ""},
            "team2": {"id": 2, "name": "Beta",  "logo": ""},
            "result": {"team1": 2, "team2": 1},
            "stars": 3,
            "format": "bo3",
        }
        r = _norm_match_result(raw)
        self.assertEqual(r.id, 9876)
        self.assertEqual(r.score_t1, 2)
        self.assertEqual(r.score_t2, 1)
        self.assertEqual(r.stars, 3)
        self.assertEqual(r.format, "bo3")
        self.assertEqual(r.team1.name, "Alpha")
        self.assertEqual(r.winner.name, "Alpha")

    def test_norm_player_stats_overview(self):
        from services.cs2.cs2_data_provider import _norm_player_stats
        raw = {
            "id": 7998,
            "ign": "s1mple",
            "overviewStatistics": {
                "kills": 12000, "deaths": 9000,
                "kdRatio": 1.33, "damagePerRound": 87.0,
                "headshots": 38.0, "mapsPlayed": 700,
                "roundsPlayed": 18000, "killsPerRound": 0.71,
                "assistsPerRound": 0.08, "deathsPerRound": 0.52,
                "rating2": 1.30,
            },
            "individualStatistics": {
                "openingKills": 1800, "openingDeaths": 1200,
                "openingKillRatio": 1.50,
                **{k: 0 for k in ["roundsWithKills", "zeroKillRounds",
                                    "oneKillRounds", "twoKillRounds",
                                    "threeKillRounds", "fourKillRounds",
                                    "fiveKillRounds", "openingKillRating",
                                    "teamWinPercentAfterFirstKill",
                                    "firstKillInWonRounds", "rifleKills",
                                    "sniperKills", "smgKills", "pistolKills",
                                    "grenadeKills", "otherKills"]}
            },
            "matches": [
                {"date": 1714000000000, "map": "Mirage",
                 "team1": {"id": 1, "name": "NaVi"},
                 "team2": {"id": 2, "name": "FaZe"},
                 "kills": 22, "deaths": 14, "rating": 1.35, "mapStatsId": 12}
            ],
        }
        stats = _norm_player_stats(raw, 7998)
        self.assertEqual(stats.player_id, 7998)
        self.assertEqual(stats.ign, "s1mple")
        self.assertAlmostEqual(stats.kd_ratio, 1.33)
        self.assertAlmostEqual(stats.adr, 87.0)
        self.assertAlmostEqual(stats.hs_pct, 38.0)
        self.assertAlmostEqual(stats.rating, 1.30)
        self.assertEqual(stats.opening_kills, 1800)
        self.assertEqual(len(stats.recent_maps), 1)
        self.assertEqual(stats.recent_maps[0].kills, 22)
        self.assertEqual(stats.recent_maps[0].map_name, "Mirage")


# ── CS2: player service (HLTV) ────────────────────────────────────────────────

class TestCS2HLTVPlayerService(unittest.TestCase):
    def _make_stats(self) -> "CS2PlayerStats":
        from services.cs2.models import CS2PlayerStats, CS2PlayerMapEntry
        maps = [
            CS2PlayerMapEntry(map_name="Mirage", kills=22, deaths=14, rating=1.35),
            CS2PlayerMapEntry(map_name="Mirage", kills=18, deaths=15, rating=1.10),
            CS2PlayerMapEntry(map_name="Nuke",   kills=20, deaths=13, rating=1.25),
            CS2PlayerMapEntry(map_name="Inferno", kills=16, deaths=17, rating=0.95),
        ]
        return CS2PlayerStats(
            player_id=7998,
            ign="s1mple",
            kd_ratio=1.33,
            adr=87.0,
            hs_pct=38.0,
            rating=1.30,
            opening_kills=1800,
            opening_deaths=1200,
            opening_kill_ratio=1.50,
            recent_maps=maps,
        )

    def test_build_player_summary_has_stats(self):
        from services.cs2.cs2_player_service import build_player_summary
        stats = self._make_stats()
        summary = build_player_summary(stats)
        self.assertTrue(summary["has_stats"])
        self.assertAlmostEqual(summary["kd_ratio"], 1.33)
        self.assertAlmostEqual(summary["adr"], 87.0)

    def test_kills_per_map_from_history(self):
        from services.cs2.cs2_player_service import kills_per_map
        stats = self._make_stats()
        kpm = kills_per_map(stats)
        self.assertAlmostEqual(kpm, (22 + 18 + 20 + 16) / 4, places=5)

    def test_avg_kills_on_map_filters_correctly(self):
        from services.cs2.cs2_player_service import avg_kills_on_map
        stats = self._make_stats()
        mirage_avg = avg_kills_on_map(stats, "Mirage")
        self.assertAlmostEqual(mirage_avg, 20.0, places=5)

    def test_build_player_sample_dict_for_projection(self):
        from services.cs2.cs2_player_service import build_player_sample_dict
        stats = self._make_stats()
        sample = build_player_sample_dict(stats)
        self.assertTrue(sample["has_stats"])
        self.assertEqual(sample["sample_size"], 4)
        self.assertAlmostEqual(sample["kills_avg"], (22 + 18 + 20 + 16) / 4)
        self.assertEqual(len(sample["maps"]), 4)

    def test_sample_for_map_filters(self):
        from services.cs2.cs2_player_service import build_player_sample_dict, sample_for_map
        stats = self._make_stats()
        sample = build_player_sample_dict(stats)
        mirage_maps = sample_for_map(sample, "Mirage")
        self.assertEqual(len(mirage_maps), 2)
        self.assertTrue(all(m["map"] == "Mirage" for m in mirage_maps))


# ── CS2: team service helpers ─────────────────────────────────────────────────

class TestCS2HLTVTeamService(unittest.TestCase):
    def _make_results(self) -> list:
        from services.cs2.models import CS2MatchResult, CS2Team
        return [
            CS2MatchResult(
                team1=CS2Team(id=1, name="Alpha"),
                team2=CS2Team(id=2, name="Beta"),
                score_t1=2, score_t2=1,
            ),
            CS2MatchResult(
                team1=CS2Team(id=1, name="Alpha"),
                team2=CS2Team(id=3, name="Gamma"),
                score_t1=0, score_t2=2,
            ),
            CS2MatchResult(
                team1=CS2Team(id=4, name="Delta"),
                team2=CS2Team(id=1, name="Alpha"),
                score_t1=1, score_t2=2,
            ),
        ]

    def test_form_string(self):
        from services.cs2.cs2_team_service import form_string
        results = self._make_results()
        s = form_string(results, team_id=1, team_name="Alpha", n=5)
        self.assertEqual(s, "W L W")

    def test_win_rate(self):
        from services.cs2.cs2_team_service import win_rate
        results = self._make_results()
        wr = win_rate(results, team_id=1, team_name="Alpha")
        # win_rate rounds to 2 decimal places → 0.67
        self.assertAlmostEqual(wr, round(2 / 3, 2), places=2)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
