"""Detect a POV player's org (and opponent) from a CS2 demo itself.

We do NOT trust memory or rosters. Everything is derived from the demo:

  * The two org names come from the HLTV folder name (``A-vs-B``).
  * CS2 keeps the two team entities in FIXED slots: TERRORIST entity is slot
    2, CT entity is slot 3 (validated at tick level on many demos). Which ORG
    sits in which slot swaps at halftime and per map — both teams play both
    sides — so a single early record can never decide anything.
  * Players carry the same slot in ``CCSPlayerController.m_iTeamNum``.
  * ``demoparser2.parse_ticks`` exposes, per tick, each player's controller
    slot AND each team entity's clantag. Taking the LAST values (end-of-demo
    state) pairs slot <-> org unambiguously: whoever holds the "Team X" tag
    in slot N is the org of every player whose controller slot is N.

This replaces the old raw-bytes heuristics, which were wrong in every
direction: the ``CT\\x00<name>``/``TERRORIST\\x00<name>`` side tags are not
reliable side markers (some demos emit both orgs under the same tag; the last
record is not always the final state), slug matching was case-sensitive
(``fut`` never matched ``FUT Esports``), and "first steamID bytes after a
name" is meaningless because player steamIDs occur all over the demo. Those
produced inverted titles (e.g. donk/tN1R labelled "vs Spirit" when they play
FOR Spirit) — this is the 4th such report.

Usage:
    from scripts.shorts.detect_team import detect_pov_opponent
    pov_org, opp_org = detect_pov_opponent("path/to/demo.dem", "7656119...")
"""

from __future__ import annotations

import re
from pathlib import Path

# slug-substring -> (canonical display, longer string that appears in the demo)
# Order matters: longer / more specific slugs first.
_ALIASES = [
    ("the-mongolz", "MongolZ", "The MongolZ"),
    ("mongolz", "MongolZ", "The MongolZ"),
    ("natus-vincere", "NAVI", "Natus Vincere"),
    ("natus", "NAVI", "Natus Vincere"),
    ("vincere", "NAVI", "Natus Vincere"),
    ("spirit", "Spirit", "Team Spirit"),
    ("fut", "FUT", "FUT Esports"),
    ("big", "BIG", "BIG"),
    ("b8", "B8", "B8 Esports"),
]

_TICK_FIELDS = [
    "CCSPlayerController.m_steamID",
    "CCSPlayerController.m_iTeamNum",
    "CCSTeam.m_iTeamNum",
    "CCSTeam.m_szClanTeamname",
]


def _slug_to_org(slug: str) -> tuple[str, str]:
    for sub, disp, raw in _ALIASES:
        if sub in slug:
            return (disp, raw)
    disp = slug.replace("-", " ").title()
    return (disp, slug.replace("-", " "))


_MAP_WORDS = [
    "cache", "nuke", "anubis", "ancient", "mirage", "dust2", "overpass",
    "inferno", "train", "vertigo", "office", "italy", "mills", "thera",
    "basalt", "edan", "pool_day", "arcade",
]


def orgs_from_folder(demo) -> list[tuple[str, str]]:
    """Return ``[(display, raw_search), ...]`` for the two teams from the folder."""
    stem = Path(demo).stem.lower()
    stem = re.sub(r"-m\d+-[a-z0-9]+$", "", stem)  # strip "-mN-mapname"
    stem = re.sub(r"-(p\d)$", "", stem)            # strip "-p1/-p2" split demos
    for w in _MAP_WORDS:                              # strip trailing map (single-map folders)
        if stem.endswith("-" + w):
            stem = stem[: -(len(w) + 1)]
            break
    if "vs" not in stem:
        return []
    a, b = stem.split("vs", 1)
    return [_slug_to_org(a.strip("-").strip()), _slug_to_org(b.strip("-").strip())]


def _fuzzy_hit(org_name: str, folder_token: str) -> bool:
    """Case-insensitive containment either way, or high similarity.

    Demo clantags are often abbreviated (``PVISION`` for Parivision) or
    suffixed (``K27 Esports`` for K27), so we accept containment OR a
    difflib similarity >= 0.8 on stripped names.
    """
    import difflib

    org_name = re.sub(r"[^a-z0-9]", "", org_name.strip().lower())
    folder_token = re.sub(r"[^a-z0-9]", "", folder_token.strip().lower())
    if not org_name or not folder_token:
        return False
    if org_name in folder_token or folder_token in org_name:
        return True
    return difflib.SequenceMatcher(None, org_name, folder_token).ratio() >= 0.8


def _detect_data(demo, sid: int) -> tuple[dict[int, str] | None, int | None]:
    """One demoparser pass: return ``(entity_slot -> clantag, pov_slot)``.

    All values come from the SAME tick snapshot (the last tick whose rows
    carry both team-entity clantags and player controller slots). Entity
    slot numbers and player controller slots swap together at halftime, so
    pairing "last row per column" across different update ticks is wrong —
    pairing within one snapshot is unambiguous (2 = TERRORIST, 3 = CT).
    """
    try:
        from demoparser2 import DemoParser
    except Exception:
        return (None, None)
    try:
        df = DemoParser(str(demo)).parse_ticks(_TICK_FIELDS)
    except Exception:
        return (None, None)
    if df.empty:
        return (None, None)

    slots: dict[int, str] = {}
    pov_slot = None
    for tick in df["tick"].unique()[::-1]:  # newest first
        snp = df[df["tick"] == tick]
        ent = snp.dropna(subset=["CCSTeam.m_iTeamNum", "CCSTeam.m_szClanTeamname"])
        pl = snp.dropna(subset=["CCSPlayerController.m_iTeamNum", "steamid"])
        # Need BOTH team entities and the POV player in this snapshot.
        if len(ent) < 2:
            continue
        if pl["steamid"].astype(str).eq(str(sid)).sum() == 0:
            continue
        slots = {
            int(k): str(v).strip()
            for k, v in ent.groupby("CCSTeam.m_iTeamNum")[
                "CCSTeam.m_szClanTeamname"].last().to_dict().items()
        }
        row = pl[pl["steamid"].astype(str) == str(sid)].iloc[-1]
        slot = int(row["CCSPlayerController.m_iTeamNum"])
        pov_slot = slot if slot in (2, 3) else None
        break
    return (slots if slots else None, pov_slot)


def detect_pov_opponent(demo, pov_steam_id) -> tuple[str | None, str | None]:
    """Return ``(pov_org_display, opponent_org_display)`` for a POV player.

    Method: pair the end-of-demo team-entity clantags with their fixed slots,
    then read the POV player's final controller slot. Both halves are handled
    because we only ever use the end-of-demo state.
    """
    try:
        sid = int(pov_steam_id)
    except (TypeError, ValueError):
        return (None, None)

    orgs = orgs_from_folder(demo)
    if len(orgs) != 2:
        return (None, None)

    slots, pov_slot = _detect_data(demo, sid)
    if not slots or pov_slot is None:
        return (None, None)

    # Map each entity clantag to its folder org (case-insensitive).
    slot2idx: dict[int, int] = {}
    matched: set[int] = set()
    for slot, clantag in slots.items():
        idx = next(
            (i for i, (disp, raw) in enumerate(orgs)
             if _fuzzy_hit(clantag, disp) or _fuzzy_hit(clantag, raw)),
            None,
        )
        if idx is None:
            continue
        if idx in matched:
            # Both entity slots claim the same folder org -> ambiguous, bail.
            return (None, None)
        matched.add(idx)
        slot2idx[slot] = idx
    if not slot2idx or pov_slot not in slot2idx:
        return (None, None)

    i = slot2idx[pov_slot]
    return (orgs[i][0], orgs[1 - i][0])