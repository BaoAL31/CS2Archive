"""Generate ``upload_meta_shorts.json`` for a rendered CS2 short.

Team + opponent are detected from the demo itself (scripts.shorts.detect_team),
never from memory. Composes a YouTube-Shorts-ready title/description and writes
the meta file next to the short.

Usage:
    python scripts/shorts/make_short_meta.py renders/shorts/shorts-*/shorts-*
    python scripts/shorts/make_short_meta.py renders/shorts/shorts-spirit-vs-big-m1-cache/shorts-4k_multikill-faveN-t81755
"""

from __future__ import annotations

import json
import os
import glob
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from scripts.shorts.detect_team import detect_pov_opponent
except ModuleNotFoundError:
    from shorts.detect_team import detect_pov_opponent

_TAGS = ["Shorts"]

# Folder event-slug substring -> tournament hashtag base (no '#', no year).
# Matched against the demo's parent folder name (e.g. "...-esports-world-cup").
_EVENT_TAGS = {
    "esports-world-cup": "esportworldcup",
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


def _map_name(demo_map: str) -> str:
    return demo_map.replace("de_", "").replace("_", " ").title()


def _make_title(nick: str, short_type: str, opp: str | None, mname: str, tournament_tag: str = "", clutch: str | None = None, kills: int = 0, punch_tags: list[str] | None = None) -> str:
    opp_s = opp if opp else "opponent"
    if "clutch" in short_type.lower():
        clutch_s = clutch or "1v3"
        base = f"{nick} pulls off a {clutch_s} CLUTCH vs {opp_s} on {mname} #cs2 #counterstrike"
    else:
        if punch_tags:
            weapon = punch_tags[0].replace("_punch_up", "").upper()
            if kills >= 5:
                base = f"{nick}'s INSANE {weapon} ACE (5K) vs {opp_s} on {mname} #cs2 #counterstrike"
            else:
                base = f"{nick}'s {weapon} 4K vs {opp_s} on {mname} #cs2 #counterstrike"
        else:
            if kills >= 5:
                base = f"{nick}'s INSANE ACE (5K) vs {opp_s} on {mname} #cs2 #counterstrike"
            else:
                base = f"{nick}'s 4K vs {opp_s} on {mname} #cs2 #counterstrike"
    return f"{base} {tournament_tag}".strip() if tournament_tag else base


def _make_description(nick: str, pov_org: str | None, opp: str | None, mname: str, short_type: str, clutch: str | None = None, kills: int = 0, punch_tags: list[str] | None = None) -> str:
    if "clutch" in short_type.lower():
        kind = f"{clutch or '1v3'} clutch" if clutch else "1v3 clutch"
    else:
        kind = "ACE (5K)" if kills >= 5 else "4K"
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
    demo = tl["demo_path"]
    sid = s["pov_steam_id"]
    nick = s["pov_nick"]
    mname = _map_name(tl.get("map", ""))
    short_type = s.get("short_type", "4k")

    pov_org, opp_org = detect_pov_opponent(demo, sid)
    tournament_tag = _tournament_hashtag(demo, year=year, override=tournament)

    vids = sorted(f for f in os.listdir(folder) if f.endswith(".mp4"))
    video = os.path.abspath(os.path.join(folder, vids[-1])) if vids else ""
    clutch = s.get("clutch_initial_count")
    kills = len(s.get("kill_ticks", []))
    punch_tags = s.get("punch_up_tags") or []

    meta = {
        "video_path": video,
        "title": _make_title(nick, short_type, opp_org, mname, tournament_tag, clutch=clutch, kills=kills, punch_tags=punch_tags),
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
        print(f"   {meta['title']}")
        print(f"   video: {os.path.basename(meta['video_path'])}  org={meta['description'].split('(')[1].split(')')[0] if '(' in meta['description'] else '?'}")

if __name__ == "__main__":
    main(sys.argv)
