"""Generate ``upload_meta_shorts.json`` for a rendered CS2 short.

Team + opponent are detected from the demo itself (scripts.shorts.detect_team),
never from memory. Composes a YouTube-Shorts-ready title/description and writes
the meta file next to the short.

Usage:
    python scripts/shorts/make_short_meta.py renders/shorts/shorts-*/shorts-*
    python scripts/shorts/make_short_meta.py renders/shorts/shorts-spirit-vs-big-m1-cache/shorts-4k_multikill-faveN-t81755
"""

from __future__ import annotations

import hashlib
import json
import os
import glob
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "scripts"), os.path.join(_ROOT, "scripts", "faceit")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts.shorts.detect_team import detect_pov_opponent
except ModuleNotFoundError:
    from shorts.detect_team import detect_pov_opponent

_TAGS = ["Shorts"]

# Folder event-slug substring -> tournament hashtag base (no '#', no year).
# Matched against the demo's parent folder name (e.g. "...-esports-world-cup").
_EVENT_TAGS = {
    "esports-world-cup": "esportworldcup",
    "blast-open-porto": "blastopenporto",
    "blast-bounty": "blastbounty",
}


def _tournament_hashtag(demo_path: str, year: str = "2026", override: str | None = None) -> str:
    """Return the tournament hashtag, e.g. '#esportworldcup2026'.

    `override` is a full hashtag (with or without '#') to force a value.
    Otherwise the event slug is read from the demo's parent folder name.
    """
    if override:
        return override if override.startswith("#") else f"#{override}"
    folder = os.path.basename(os.path.dirname(str(demo_path))).lower()
    for slug, base in _EVENT_TAGS.items():
        if slug in folder:
            return f"#{base}{year}"
    return ""


def _resolve_demo(demo_path: str) -> str:
    """Timeline paths are repo-relative; resolve them against the project root."""
    p = Path(demo_path)
    if p.is_file():
        return str(p)
    alt = Path(_ROOT) / demo_path
    if alt.is_file():
        return str(alt)
    return demo_path


def _map_name(demo_map: str) -> str:
    return demo_map.replace("de_", "").replace("_", " ").title()


def _pick(key: str, options: tuple) -> object:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return options[int(digest, 16) % len(options)]


# Grammatical slots, not a banned-word list. Swap freely if the sentence still
# parses. "going crazy" and "going insane" are the same skeleton; "filthy 4K"
# is a different slot (pre-kind). Do not put kind-slot words in going-slot
# ("going filthy") or vice versa ("insane 4K" is ok-ish; "nuclear 4K" is not).
_ADJ = {
    "going": (
        "crazy", "insane", "nuclear", "unhinged", "wild", "ballistic",
        "feral", "mental", "ham", "postal",
    ),
    "kind": (
        "filthy", "nasty", "disgusting", "ridiculous", "absurd", "sick",
        "clean", "cold",
    ),
    "looking": (
        "unhinged", "insane", "crazy", "feral", "filthy", "possessed",
    ),
}


def _adj(key: str, slot: str) -> str:
    """Pick an adjective for a grammatical slot, hashed independently of format."""
    return _pick(key + "|adj|" + slot, _ADJ[slot])


def _is_top10_opponent(opp: str | None) -> bool:
    """True when the opposing org is currently (or listed as) HLTV top 10."""
    if not opp:
        return False
    try:
        from hltv_ranking import TEAM_ALIASES, RANKING_TEAM_NAMES, _load_cache
    except ModuleNotFoundError:
        return False
    canon = TEAM_ALIASES.get(opp, opp)
    cached = _load_cache() or {}
    rank = cached.get(canon) or cached.get(opp)
    if rank is None:
        names = {n.lower(): i + 1 for i, n in enumerate(RANKING_TEAM_NAMES)}
        rank = names.get(canon.lower()) or names.get(opp.lower())
    return rank is not None and int(rank) <= 10


def _vs_bit(opp: str | None, mname: str, key: str) -> str:
    """Opponent/map phrasing. Mixes vs/against and with/without map for A/B."""
    if not opp:
        return f"on {mname}"
    return _pick(key + "|vs", (
        f"vs {opp}",
        f"against {opp}",
        f"vs {opp} on {mname}",
        f"against {opp} on {mname}",
    ))


def _gun(punch_tags: list[str] | None) -> str:
    if not punch_tags:
        return ""
    return punch_tags[0].replace("_punch_up", "").upper()


def _make_title(nick: str, short_type: str, opp: str | None, mname: str, tournament_tag: str = "", clutch: str | None = None, kills: int = 0, punch_tags: list[str] | None = None, start_tick: int = 0) -> tuple[str, str]:
    # Format hash ignores the live opponent string so filling in a top-10 org
    # does not reshuffle the skeleton (onic stays hyphen-hook, Vitality is spliced in).
    key = f"{nick}|{short_type}|{clutch}|{kills}|None|{mname}|{punch_tags}|{start_tick}"
    vs = _vs_bit(opp, mname, key)
    gun = _gun(punch_tags)
    gun_sp = f"{gun} " if gun else ""
    clutch_s = clutch or "1v3"
    nk = f"{kills}K" if kills else ""

    if "clutch" in short_type.lower():
        if opp:
            formats = (
                ("hook-rest", f"{clutch_s} against {opp}? No problem for {nick}"),
            )
        else:
            rest = f"{nick} {nk} clutch on {mname}" if nk else f"{nick} clutch on {mname}"
            formats = (("hook-rest", f"{clutch_s}? {rest}"),)
    elif short_type == "wallbang":
        formats = (
            ("kind-lead", f"{nick} wallbang {vs}"),
            ("possessive", f"{nick}'s wallbang {vs}"),
        )
    elif short_type == "knife":
        formats = (
            ("kind-lead", f"{nick} knife kill {vs}"),
            ("possessive", f"{nick}'s knife kill {vs}"),
        )
    elif short_type == "defuse":
        formats = (
            ("kind-lead", f"{nick} clutch defuse {vs}"),
            ("possessive", f"{nick}'s clutch defuse {vs}"),
        )
    elif short_type == "perfect_shots":
        formats = (
            ("kind-lead", f"{nick} perfect shots {vs}"),
            ("possessive", f"{nick}'s perfect shots {vs}"),
        )
    elif short_type == "flick":
        formats = (
            ("kind-lead", f"{nick} flick {vs}"),
            ("possessive", f"{nick}'s flick {vs}"),
        )
    elif kills >= 5:
        ace = f"{gun} ACE" if gun else "ACE"
        going, looking, kadj = _adj(key, "going"), _adj(key, "looking"), _adj(key, "kind")
        hook = _pick(key + "|hookshape", (
            f"{nick} going {going} - {ace} {vs}",
            f"{nick} looking {looking} - {ace} {vs}",
        ))
        formats = (
            ("casual-drop", f"{nick} casually drops an {ace} {vs}"),
            ("gerund", f"{nick} dropping an {ace} {vs}"),
            ("possessive", f"{nick}'s {ace} {vs}"),
            ("kind-lead", f"{kadj} {ace} from {nick} {vs}"),
            ("hyphen-hook", hook),
            ("they-couldnt", f"{opp} couldn't stop {nick}'s ACE" if opp else f"{nick}'s {ace} {vs}"),
        )
    else:
        kind = f"{gun_sp}4K".strip()
        going, looking, kadj = _adj(key, "going"), _adj(key, "looking"), _adj(key, "kind")
        hook = _pick(key + "|hookshape", (
            f"{nick} going {going} - {kind} {vs}",
            f"{nick} looking {looking} - {kind} {vs}",
        ))
        formats = (
            ("possessive", f"{nick}'s {kind} {vs}"),
            ("gerund", f"{nick} dropping a {kind} {vs}"),
            ("kind-lead", f"{kadj} {kind} from {nick} {vs}"),
            ("casual-drop", f"{nick} casually drops a {kind} {vs}"),
            ("hyphen-hook", hook),
            ("hook-emoji", f"{kind}? Easy for {nick} {vs}"),
        )

    fmt, line = _pick(key, formats)
    line = " ".join(line.split())
    if opp and _is_top10_opponent(opp) and opp.lower() not in line.lower():
        line = f"{line} vs {opp}"
    base = f"{line} #cs2 #counterstrike"
    title = f"{base} {tournament_tag}".strip() if tournament_tag else base
    return title, fmt


def _make_description(nick: str, pov_org: str | None, opp: str | None, mname: str, short_type: str, clutch: str | None = None, kills: int = 0, punch_tags: list[str] | None = None) -> str:
    if "clutch" in short_type.lower():
        kind = f"{clutch or '1v3'} clutch" if clutch else "1v3 clutch"
    elif short_type == "wallbang":
        kind = "wallbang"
    elif short_type == "knife":
        kind = "knife kill"
    elif short_type == "defuse":
        kind = "clutch defuse"
    elif short_type == "perfect_shots":
        kind = "perfect shots"
    elif short_type == "flick":
        kind = "flick"
    else:
        kind = "ACE" if kills >= 5 else "4K"
        if punch_tags:
            # list punch weapons for flavor
            weapons = ", ".join(t.replace("_punch_up","").upper() for t in punch_tags)
            kind = f"{kind} ({weapons} punch-up)" if weapons else kind
    vs = opp if opp else "the opponent"
    org = f" ({pov_org})" if pov_org else ""
    return f"Esports World Cup 2026 highlight - {nick}{org} with the {kind} against {vs} on {mname}."


def make_meta(folder: str, tournament: str | None = None, year: str = "2026") -> dict:
    tl_path = os.path.join(folder, "short_timeline.json")
    tl = json.load(open(tl_path, encoding="utf-8"))
    s = tl["shorts"][0]
    demo = _resolve_demo(tl["demo_path"])
    sid = s["pov_steam_id"]
    nick = s["pov_nick"]
    mname = _map_name(tl.get("map", ""))
    short_type = s.get("short_type", "4k")

    pov_org, opp_org = detect_pov_opponent(demo, sid)
    pov_org = pov_org or None
    opp_org = opp_org or None
    tournament_tag = _tournament_hashtag(demo, year=year, override=tournament)

    vids = sorted(f for f in os.listdir(folder) if f.endswith(".mp4"))
    video = os.path.abspath(os.path.join(folder, vids[-1])) if vids else ""
    clutch = s.get("clutch_initial_count")
    kills = len(s.get("kill_ticks", []))
    punch_tags = s.get("punch_up_tags") or []
    title, title_format = _make_title(
        nick, short_type, opp_org, mname, tournament_tag,
        clutch=clutch, kills=kills, punch_tags=punch_tags,
        start_tick=s.get("start_tick", 0),
    )

    meta = {
        "video_path": video,
        "title": title,
        "title_format": title_format,
        "description": _make_description(nick, pov_org, opp_org, mname, short_type, clutch=clutch, kills=kills, punch_tags=punch_tags),
        "privacy": "private",
        "publish_at": "auto",
        "tags": _TAGS,
        "upload_status": "pending",
    }
    out = os.path.join(folder, "upload_meta_shorts.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def main(argv):
    folders = argv[1:] or glob.glob("renders/shorts/shorts-*/shorts-*")
    for f in sorted(folders):
        if not os.path.isdir(f) or not os.path.exists(os.path.join(f, "short_timeline.json")):
            continue
        meta = make_meta(f)
        print(f"{os.path.basename(f)}")
        print(f"   [{meta.get('title_format', '?')}] {meta['title']}")
        print(f"   video: {os.path.basename(meta['video_path'])}  org={meta['description'].split('(')[1].split(')')[0] if '(' in meta['description'] else '?'}")

if __name__ == "__main__":
    main(sys.argv)
