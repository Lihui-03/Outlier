"""Splits: home/away, W/L, rest-day buckets."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from utils.dates import bucket_rest_days, order_rest_buckets

from .stats import ensure_stat_column


def _split_means(df: pd.DataFrame, stat_col: str, by: str) -> pd.DataFrame:
    if df is None or df.empty or by not in df.columns or stat_col not in df.columns:
        return pd.DataFrame(columns=[by, "games", "mean", "median"])
    sub = df[[by, stat_col]].copy()
    sub[stat_col] = pd.to_numeric(sub[stat_col], errors="coerce")
    grouped = sub.groupby(by, dropna=False)[stat_col].agg(
        games="count", mean="mean", median="median"
    )
    return grouped.reset_index()


def home_away_split(df: pd.DataFrame, stat_col: str) -> pd.DataFrame:
    """Return a 2-row DataFrame with Home vs Away averages for the stat."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["location", "games", "mean", "median"])
    g = _split_means(df, stat_col, "IS_HOME")
    if g.empty:
        return pd.DataFrame(columns=["location", "games", "mean", "median"])
    g["location"] = g["IS_HOME"].map({True: "Home", False: "Away"}).fillna("Unknown")
    return g[["location", "games", "mean", "median"]]


def win_loss_split(df: pd.DataFrame, stat_col: str) -> pd.DataFrame:
    """Return a 2-row DataFrame with Win vs Loss averages."""
    if df is None or df.empty or "IS_WIN" not in df.columns:
        return pd.DataFrame(columns=["result", "games", "mean", "median"])
    g = _split_means(df, stat_col, "IS_WIN")
    if g.empty:
        return pd.DataFrame(columns=["result", "games", "mean", "median"])
    g["result"] = g["IS_WIN"].map({True: "Win", False: "Loss"}).fillna("Unknown")
    return g[["result", "games", "mean", "median"]]


def rest_day_split(df: pd.DataFrame, stat_col: str) -> pd.DataFrame:
    """Return a DataFrame of averages grouped by rest-day bucket."""
    if df is None or df.empty or "REST_DAYS" not in df.columns:
        return pd.DataFrame(columns=["rest", "games", "mean", "median"])

    sub = df[["REST_DAYS", stat_col]].copy() if stat_col in df.columns else pd.DataFrame()
    if sub.empty:
        return pd.DataFrame(columns=["rest", "games", "mean", "median"])

    sub[stat_col] = pd.to_numeric(sub[stat_col], errors="coerce")
    sub["rest"] = sub["REST_DAYS"].apply(bucket_rest_days)
    g = sub.groupby("rest", dropna=False)[stat_col].agg(
        games="count", mean="mean", median="median"
    ).reset_index()

    # Consistent ordering.
    order = list(order_rest_buckets())
    g["__order"] = g["rest"].apply(lambda x: order.index(x) if x in order else len(order))
    g = g.sort_values("__order").drop(columns="__order").reset_index(drop=True)
    return g


def opponent_history(df: pd.DataFrame, stat_col: str, opponent_abbr: str) -> dict[str, Any]:
    """Return a small summary of the player's history vs a specific opponent."""
    if df is None or df.empty or not opponent_abbr or "OPPONENT" not in df.columns:
        return {"games": 0, "mean": float("nan"), "median": float("nan"), "recent": []}

    mask = df["OPPONENT"].fillna("").str.upper() == opponent_abbr.upper()
    sub = df.loc[mask]
    values = ensure_stat_column(sub, stat_col)
    recent = []
    if not sub.empty and "GAME_DATE" in sub.columns and stat_col in sub.columns:
        for _, r in sub.head(5).iterrows():
            recent.append({
                "date": r["GAME_DATE"],
                "matchup": r.get("MATCHUP"),
                "value": r.get(stat_col),
                "minutes": r.get("MIN"),
            })
    return {
        "games": int(values.dropna().shape[0]),
        "mean": float(values.mean()) if not values.empty and not values.isna().all() else float("nan"),
        "median": float(values.median()) if not values.empty and not values.isna().all() else float("nan"),
        "recent": recent,
    }
