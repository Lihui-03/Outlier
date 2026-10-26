"""CS2 (Counter-Strike 2) research services — completely separate from the NBA
service namespace.

Architecture
------------
Provider priority: GRID > HLTV > Mock.

    grid_provider.py    → GRID Open Access client (GraphQL + REST end-state).
                          Set GRID_API_KEY to activate.  Default primary source.
    hltv_provider.py    → gigobyte/hltv Node.js bridge: fallback when GRID is
                          unconfigured, or enrichment (CS2_ENABLE_HLTV_ENRICHMENT).
                          Run ``npm install`` in hltv_bridge/ before first use.
    mock_provider.py    → Static demo data for offline / development.
    cs2_cache_service.py→ Shared file-system JSON cache (grid: and hltv: keys).
    models.py           → Internal dataclasses (CS2Team, CS2Player, CS2Tournament…).
    cs2_data_provider.py→ Unified facade: resolves provider, normalises raw data.
    cs2_capabilities.py → Registry of which features are supported per provider.
    cs2_match_service.py→ Form, H2H, map-pool summaries.
    cs2_team_service.py → Team profile + map win-rate aggregation.
    cs2_player_service.py → Player profile + per-match stats summary.
    cs2_map_service.py  → Cross-team aggregation per map.
    cs2_projection_service.py → Pure projection engine (no Streamlit, no I/O).

PandaScore (pandascore_client.py / pandascore_provider.py) is retained as an
optional schedule supplement when a free API token is configured.
"""

from __future__ import annotations

# Convenience re-exports for call sites. Keep this list tight.
__all__ = [
    "grid_provider",
    "hltv_provider",
    "mock_provider",
    "cs2_cache_service",
    "models",
    "cs2_data_provider",
    "cs2_capabilities",
    "cs2_match_service",
    "cs2_team_service",
    "cs2_player_service",
    "cs2_map_service",
    "cs2_projection_service",
    # Optional PandaScore supplement:
    "pandascore_client",
    "pandascore_provider",
]
