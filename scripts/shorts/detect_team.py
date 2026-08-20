"""Detect a POV player's org (and opponent) from a CS2 demo itself.

We do NOT trust memory or rosters. The demo's embedded matchinfo carries the
real team names and each team's player steamIDs. So:

  PRIMARY (matchinfo intervals):
    1. Derive the two org names from the HLTV folder name (``A-vs-B``).
    2. Locate each org's name string in the raw demo (matchinfo).
    3. Locate the POV player's 64-bit steamID bytes in the raw demo.
    4. The POV player belongs to whichever team's name-preceded interval
       contains their steamID.

  CROSS-CHECK (side + CT label):
    * POV ``team_number`` from demoparser2 ``parse_player_info`` is the CT/T
      *side* (it swaps between maps, so it is NEVER an org by itself).
    * The raw matchinfo marks one team ``CT\x00<org>``; in these demos the CT
      side is ``team_number == 2``. So POV org = CT org iff team_number == 2.

This is the single source of truth for "what team are they on". It replaces the
old manual/memory-based guessing that produced wrong titles (e.g. faveN/gr1ks
labelled "vs BIG" when they play FOR BIG).

Usage:
    from scripts.shorts.detect_team import detect_pov_opponent
    pov_org, opp_org = detect_pov_opponent("path/to/demo.dem", "7656119...")
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

# slug-substring -> (display name, exact raw string in the demo's matchinfo)
# Order matters: longer / more specific slugs first.
_ALIASES = [
    ("the-mongolz", "MongolZ", "The MongolZ"),
    ("mongolz", "MongolZ", "The MongolZ"),
    ("natus-vincere", "NAVI", "Natus Vincere"),
    ("natus", "NAVI", "Natus Vincere"),
    ("vincere", "NAVI", "Natus Vincere"),
    ("spirit", "Spirit", "Team Spirit"),
    ("big", "BIG", "BIG"),
]

_CT_TEAM_NUMBER = 2  # validated: CT side == team_number 2 across observed demos


def _slug_to_org(slug: str) -> tuple[str, str]:
    for sub, disp, raw in _ALIASES:
        if sub in slug:
            return (disp, raw)
    disp = slug.replace("-", " ").title()
    return (disp, slug.replace("-", " "))


def orgs_from_folder(demo) -> list[tuple[str, str]]:
    """Return ``[(display, raw_search), ...]`` for the two teams from the folder."""
    stem = Path(demo).stem.lower()
    stem = re.sub(r"-m\d+-[a-z0-9]+$", "", stem)  # strip "-mN-mapname"
    if "vs" not in stem:
        return []
    a, b = stem.split("vs", 1)
    return [_slug_to_org(a.strip("-").strip()), _slug_to_org(b.strip("-").strip())]


def _read_raw(demo) -> bytes:
    return Path(demo).read_bytes()


def _side_method(demo, sid: int, orgs) -> str | None:
    """Cross-check via team_number + CT==2 rule."""
    try:
        from demoparser2 import DemoParser
    except Exception:
        return None
    try:
        p = DemoParser(str(demo))
        tn = None
        for row in p.parse_player_info().to_dict("records"):
            if str(row.get("steamid")) == str(sid):
                tn = int(row.get("team_number"))
                break
        if tn is None:
            return None
    except Exception:
        return None
    ct_org = _ct_org(_read_raw(demo), orgs)
    if ct_org is None:
        ct_org = orgs[0][0] if orgs else None
    return ct_org if tn == _CT_TEAM_NUMBER else (
        next((d for d, _ in orgs if d != ct_org), None)
    )
    """Matchinfo-interval method: which team's name-preceded block holds sid."""
    positions = {}
    for disp, raw in orgs:
        p = demo_bytes.find(raw.encode("latin-1"))
        if p != -1:
            positions[disp] = p
    if len(positions) < 2:
        return None
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    (d0, p0), (d1, p1) = ordered
    sbytes = struct.pack("<Q", sid)
    sidx = demo_bytes.find(sbytes, p0)  # first steamID at/after first team name
    if sidx == -1 or sidx < p0:
        return None
    return d0 if sidx < p1 else d1


def _ct_org(demo_bytes: bytes, orgs) -> str | None:
    """Find which folder org is the CT side, from raw `CT\x00<org>` labels.

    Some demos length-prefix the org (e.g. `CT\x00\x02BIG`); strip leading
    non-letter bytes before matching.
    """
    candidates = set()
    for disp, raw in orgs:
        candidates.add(raw)
        candidates.add(disp)
    text = demo_bytes.decode("latin-1", errors="ignore")
    for m in re.finditer(r"CT\x00([^\x00]{1,24})", text):
        cand = m.group(1).lstrip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f \x80\x81\x82\x83\x84\x85\x86\x87")
        cand = cand.strip()
        if not cand:
            continue
        for disp, raw in orgs:
            if cand in raw or raw in cand or cand.lower() in disp.lower():
                return disp
        for known in ("Team Spirit", "BIG", "Natus Vincere", "The MongolZ",
                      "Spirit", "NAVI", "MongolZ"):
            if cand == known:
                # map known -> folder display if possible
                for disp, raw in orgs:
                    if known in raw or known.lower() in disp.lower():
                        return disp
    return None


def detect_pov_opponent(demo, pov_steam_id) -> tuple[str | None, str | None]:
    """Return ``(pov_org_display, opponent_org_display)`` for a POV player.

    Primary: side (team_number) + CT label parsed from the demo. Fallback:
    matchinfo-interval (whichever team's name-preceded block holds the POV
    steamID). Both are derived entirely from the demo itself.
    """
    try:
        sid = int(pov_steam_id)
    except (TypeError, ValueError):
        return (None, None)

    data = _read_raw(demo)
    orgs = orgs_from_folder(demo)
    if not orgs:
        return (None, None)

    primary = _side_method(demo, sid, orgs)
    if primary is None:
        primary = _first_interval(data, sid, orgs)
    if primary is None:
        return (None, None)

    opponent = next((d for d, _ in orgs if d != primary), None)
    return (primary, opponent)
