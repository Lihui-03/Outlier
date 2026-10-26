"""Public URLs for NBA team marks used in charts (no local asset bundling).

Raster PNGs load reliably in Plotly ``layout.images`` across browsers.
"""

from __future__ import annotations


def nba_team_logo_url_png(opponent_abbr: str | None) -> str | None:
    """Return an ESPN CDN logo URL for a team tricode, or ``None`` if unknown."""
    if opponent_abbr is None:
        return None
    abbr = str(opponent_abbr).strip().lower()
    if not abbr or abbr == "nan":
        return None
    return f"https://a.espncdn.com/i/teamlogos/nba/500/{abbr}.png"
