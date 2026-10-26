"""Mock CS2 data provider.

Used when:
- HLTV bridge is not installed
- HLTV is in cooldown with no cached data
- ``CS2_USE_MOCK_FALLBACK=true`` is set in the environment
- Development / testing without a network connection

All data is static and clearly labelled as mock so the UI can surface
a "Demo data" banner.  Nothing in this module touches the network.
"""

from __future__ import annotations

import time
from typing import Any

# ---------------------------------------------------------------------------
# Sentinel so callers can detect mock data
# ---------------------------------------------------------------------------
MOCK_SOURCE = "mock"

# ---------------------------------------------------------------------------
# Static mock data mirroring the shapes returned by hltv_provider
# ---------------------------------------------------------------------------

_NOW_MS = int(time.time() * 1000)
_DAY_MS = 86_400_000

MOCK_MATCHES: list[dict] = [
    {
        "id": 9900001,
        "team1": {"id": 4608,  "name": "Natus Vincere", "logo": ""},
        "team2": {"id": 6665,  "name": "FaZe Clan",     "logo": ""},
        "stars": 5,
        "format": "bo3",
        "event": {"id": 7148, "name": "IEM Katowice 2025"},
        "date": _NOW_MS + _DAY_MS,
        "live": False,
        "_source": MOCK_SOURCE,
    },
    {
        "id": 9900002,
        "team1": {"id": 6137,  "name": "Astralis",  "logo": ""},
        "team2": {"id": 5973,  "name": "G2 Esports", "logo": ""},
        "stars": 3,
        "format": "bo3",
        "event": {"id": 7200, "name": "BLAST Premier Spring 2025"},
        "date": _NOW_MS + 2 * _DAY_MS,
        "live": False,
        "_source": MOCK_SOURCE,
    },
]

MOCK_RESULTS: list[dict] = [
    {
        "id": 9800001,
        "date": _NOW_MS - _DAY_MS,
        "team1": {"name": "Natus Vincere", "logo": ""},
        "team2": {"name": "FaZe Clan",     "logo": ""},
        "result": {"team1": 2, "team2": 1},
        "stars": 5,
        "format": "bo3",
        "_source": MOCK_SOURCE,
    },
    {
        "id": 9800002,
        "date": _NOW_MS - 2 * _DAY_MS,
        "team1": {"name": "G2 Esports",   "logo": ""},
        "team2": {"name": "Astralis",     "logo": ""},
        "result": {"team1": 2, "team2": 0},
        "stars": 3,
        "format": "bo3",
        "_source": MOCK_SOURCE,
    },
    {
        "id": 9800003,
        "date": _NOW_MS - 3 * _DAY_MS,
        "team1": {"name": "Vitality",     "logo": ""},
        "team2": {"name": "Cloud9",       "logo": ""},
        "result": {"team1": 1, "team2": 2},
        "stars": 2,
        "format": "bo3",
        "_source": MOCK_SOURCE,
    },
    {
        "id": 9800004,
        "date": _NOW_MS - 4 * _DAY_MS,
        "team1": {"name": "MOUZ",         "logo": ""},
        "team2": {"name": "Heroic",       "logo": ""},
        "result": {"team1": 2, "team2": 1},
        "stars": 2,
        "format": "bo3",
        "_source": MOCK_SOURCE,
    },
]

MOCK_PLAYER_STATS: dict[str, Any] = {
    "id": 7998,
    "ign": "s1mple",
    "name": "Oleksandr Kostyliev",
    "image": None,
    "age": 27,
    "country": {"name": "Ukraine", "code": "ua"},
    "team": {"id": 4608, "name": "Natus Vincere"},
    "overviewStatistics": {
        "kills": 12480,
        "headshots": 38.4,
        "deaths": 9340,
        "kdRatio": 1.34,
        "damagePerRound": 87.2,
        "mapsPlayed": 720,
        "roundsPlayed": 18200,
        "killsPerRound": 0.71,
        "assistsPerRound": 0.08,
        "deathsPerRound": 0.53,
        "rating2": 1.31,
    },
    "individualStatistics": {
        "openingKills": 1840,
        "openingDeaths": 1220,
        "openingKillRatio": 1.51,
        "openingKillRating": 1.42,
        "teamWinPercentAfterFirstKill": 73.2,
        "firstKillInWonRounds": 68.1,
        "roundsWithKills": 8940,
        "zeroKillRounds": 9260,
        "oneKillRounds": 7210,
        "twoKillRounds": 2380,
        "threeKillRounds": 840,
        "fourKillRounds": 180,
        "fiveKillRounds": 22,
        "rifleKills": 7120,
        "sniperKills": 3860,
        "smgKills": 640,
        "pistolKills": 780,
        "grenadeKills": 42,
        "otherKills": 38,
    },
    "matches": [
        {"date": _NOW_MS - i * _DAY_MS, "map": "Mirage",
         "team1": {"id": 4608, "name": "NaVi"},
         "team2": {"id": 6665, "name": "FaZe"},
         "kills": 22 - i, "deaths": 14 + i // 2,
         "rating": round(1.30 - i * 0.04, 2), "mapStatsId": 90000 + i}
        for i in range(10)
    ],
    "_source": MOCK_SOURCE,
}

MOCK_TEAM: dict[str, Any] = {
    "id": 4608,
    "name": "Natus Vincere",
    "logo": "",
    "country": {"name": "Ukraine", "code": "ua"},
    "ranking": 3,
    "players": [
        {"id": 7998, "name": "s1mple",   "ign": "s1mple"},
        {"id": 8918, "name": "electroNic","ign": "electroNic"},
        {"id": 9624, "name": "b1t",       "ign": "b1t"},
        {"id": 7552, "name": "perfecto",  "ign": "perfecto"},
        {"id": 7669, "name": "npl",       "ign": "npl"},
    ],
    "_source": MOCK_SOURCE,
}

MOCK_TEAM_STATS: dict[str, Any] = {
    "id": 4608,
    "overview": {
        "mapsPlayed": 142,
        "wins": 84,
        "draws": 2,
        "losses": 56,
        "winRate": 59.2,
        "killsPerRound": 0.69,
        "assistsPerRound": 0.10,
        "deathsPerRound": 0.60,
        "roundsPerMatch": 24.3,
    },
    "maps": [
        {"map": "Mirage",  "wins": 22, "losses": 12, "winRate": 64.7},
        {"map": "Inferno", "wins": 18, "losses": 10, "winRate": 64.3},
        {"map": "Nuke",    "wins": 15, "losses": 8,  "winRate": 65.2},
        {"map": "Ancient", "wins": 10, "losses": 9,  "winRate": 52.6},
        {"map": "Anubis",  "wins": 9,  "losses": 8,  "winRate": 52.9},
        {"map": "Dust2",   "wins": 7,  "losses": 6,  "winRate": 53.8},
        {"map": "Train",   "wins": 3,  "losses": 3,  "winRate": 50.0},
    ],
    "_source": MOCK_SOURCE,
}


# ---------------------------------------------------------------------------
# Provider functions (same signatures as hltv_provider for easy swapping)
# ---------------------------------------------------------------------------


def get_matches() -> list[dict]:
    return list(MOCK_MATCHES)


def get_results(stars: int = 0, limit: int = 30,
                team_ids: list[int] | None = None) -> list[dict]:
    results = [r for r in MOCK_RESULTS
               if stars == 0 or r.get("stars", 0) >= stars]
    if team_ids:
        tid_set = set(team_ids)
        results = [r for r in results
                   if (r.get("team1") or {}).get("id") in tid_set
                   or (r.get("team2") or {}).get("id") in tid_set]
    return results[:limit]


def get_player_stats(_player_id: int) -> dict:
    return dict(MOCK_PLAYER_STATS)


def get_team(_team_id: int) -> dict:
    return dict(MOCK_TEAM)


def get_team_stats(_team_id: int) -> dict:
    return dict(MOCK_TEAM_STATS)


def get_team_ranking() -> list[dict]:
    return [
        {"rank": i + 1, "id": 4608 + i,
         "name": ["NaVi", "FaZe", "G2", "Astralis", "Vitality"][i]}
        for i in range(5)
    ]
