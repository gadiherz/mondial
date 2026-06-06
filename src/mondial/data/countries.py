"""Team (DB canonical name) -> flag code, for the frontend flag icons.

Codes follow the `flag-icons` file names (ISO 3166-1 alpha-2, plus gb-eng / gb-sct
for the home nations). Flags are bundled locally under web/assets/flags/<code>.svg
(fetched once by scripts/fetch_flags.py) so the site makes NO external requests.
"""
from __future__ import annotations

TEAM_CODES: dict[str, str] = {
    # A
    "Mexico": "mx", "South Africa": "za", "South Korea": "kr", "Czech Republic": "cz",
    # B
    "Canada": "ca", "Bosnia and Herzegovina": "ba", "Qatar": "qa", "Switzerland": "ch",
    # C
    "Brazil": "br", "Morocco": "ma", "Haiti": "ht", "Scotland": "gb-sct",
    # D
    "United States": "us", "Paraguay": "py", "Australia": "au", "Turkey": "tr",
    # E
    "Germany": "de", "Curaçao": "cw", "Ivory Coast": "ci", "Ecuador": "ec",
    # F
    "Netherlands": "nl", "Japan": "jp", "Sweden": "se", "Tunisia": "tn",
    # G
    "Belgium": "be", "Egypt": "eg", "Iran": "ir", "New Zealand": "nz",
    # H
    "Spain": "es", "Cape Verde": "cv", "Saudi Arabia": "sa", "Uruguay": "uy",
    # I
    "France": "fr", "Senegal": "sn", "Iraq": "iq", "Norway": "no",
    # J
    "Argentina": "ar", "Algeria": "dz", "Austria": "at", "Jordan": "jo",
    # K
    "Portugal": "pt", "DR Congo": "cd", "Uzbekistan": "uz", "Colombia": "co",
    # L
    "England": "gb-eng", "Croatia": "hr", "Ghana": "gh", "Panama": "pa",
}


def code_for(team: str) -> str | None:
    """Flag code for a team name, or None if unmapped (frontend omits the flag)."""
    return TEAM_CODES.get(team)
