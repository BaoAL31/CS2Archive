"""Which HLTV events count as Clip Observation fixtures.

BLAST Open/Premier/Bounty, EWC, Majors (incl. RMR), big IEM, ESL Pro League,
PGL/StarLadder majors. Not CCT, Challenger, academy, or open/closed qualifiers.
"""

from __future__ import annotations

import re

_EXCLUDE = re.compile(
    r"qualifier|challenger|academy|showmatch|cct\b|epl-open|closed-qualifier|"
    r"open-qualifier|female|saqu|dust2\.us|rising-event|circuit-x|"
    r"2027|2028|2029",
    re.I,
)
_INCLUDE = re.compile(
    r"major|blast-open|blast-premier|blast-bounty|blast-tv|"
    r"esports-world-cup|\bewc\b|"
    r"\biem-|"
    r"esl-pro-league|"
    r"pgl-|starladder|star-ladder|\brmr\b",
    re.I,
)


def is_popular_event(slug: str, name: str = "") -> bool:
    text = f"{slug} {name}".strip()
    if not text:
        return False
    if _EXCLUDE.search(text):
        return False
    return bool(_INCLUDE.search(text))
