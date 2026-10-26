"""ESPN CDN URLs for WNBA team marks (parallel to ``utils/nba_logos``)."""

from __future__ import annotations


def wnba_team_logo_url_png(team_abbr: str | None) -> str | None:
    """Return a logo URL for a WNBA tricode, or ``None`` if unknown."""
    if team_abbr is None:
        return None
    abbr = str(team_abbr).strip().lower()
    if not abbr or abbr == "nan":
        return None
    return f"https://a.espncdn.com/i/teamlogos/wnba/500/{abbr}.png"
