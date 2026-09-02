"""Clip Observations from factory clips (Allstar Trending, later TO Shorts)."""

from __future__ import annotations

import re

from hltv_ranking import RANKING_TEAM_NAMES, TEAM_ALIASES

_CLUTCH = (
    (re.compile(r"\b1\s*v\s*5\b", re.I), "1v5_won"),
    (re.compile(r"\b1\s*v\s*4\b", re.I), "1v4_won"),
    (re.compile(r"\b1\s*v\s*3\b", re.I), "1v3_won"),
    (re.compile(r"\b2\s*v\s*[3-5]\b", re.I), "2vx_won"),
)
_MULTIKILL = (
    (re.compile(r"\bace\b|\b5\s*k\b", re.I), "ace"),
    (re.compile(r"\b4\s*k\b|\bquad(?:ro)?\b", re.I), "4k"),
    (re.compile(r"\bnearly\b|\balmost\b", re.I), "nearly"),
    (re.compile(r"\b3\s*k\b|\btriple\b", re.I), "3k"),
)
_STACK = (
    (re.compile(r"\bflick\b", re.I), "flick"),
    (re.compile(r"\bperfect\b.*\bshots?\b", re.I), "perfect_shots"),
    (re.compile(r"\bwallbang\b", re.I), "wallbang"),
    (re.compile(r"\bknife\b|\bzeus\b", re.I), "knife"),
    (re.compile(r"\bdefuse\b", re.I), "defuse"),
)
_HIGHLIGHT_BOX = re.compile(r"^M\d+R\d+\s*\|", re.I)


def _norm_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def _ranking_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for name in RANKING_TEAM_NAMES:
        lookup[_norm_team(name)] = name
    aliases = dict(TEAM_ALIASES)
    try:
        from update_team_demand import HIGHLIGHT_ALIASES
        aliases.update(HIGHLIGHT_ALIASES)
    except Exception:
        pass
    for alias, canon in aliases.items():
        target = lookup.get(_norm_team(canon), canon)
        lookup[_norm_team(alias)] = target
    return lookup


def canonical_ranking_name(name: str | None, lookup: dict[str, str] | None = None) -> str | None:
    if not name or not str(name).strip():
        return None
    lookup = lookup if lookup is not None else _ranking_lookup()
    return lookup.get(_norm_team(name))


def _match_team(segment: str, lookup: dict[str, str]) -> str | None:
    haystack = _norm_team(segment)
    if not haystack:
        return None
    found: list[tuple[int, str]] = []
    for key, canon in lookup.items():
        if key and key in haystack:
            found.append((len(key), canon))
    if not found:
        return None
    found.sort(key=lambda item: -item[0])
    return found[0][1]


def fixture_teams(match: dict | None) -> tuple[str, str] | None:
    if not match:
        return None
    lookup = _ranking_lookup()
    raw = match.get("teams")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        t1 = canonical_ranking_name(raw[0], lookup)
        t2 = canonical_ranking_name(raw[1], lookup)
        if t1 and t2 and t1 != t2:
            return t1, t2
    slug = str(match.get("slug") or "")
    if "-vs-" not in slug:
        return None
    left, right = slug.split("-vs-", 1)
    t1 = _match_team(left, lookup)
    t2 = _match_team(right, lookup)
    if t1 and t2 and t1 != t2:
        return t1, t2
    return None


def opponent_of_cut(short: dict, orgs: list[str] | None = None) -> str | None:
    """Opponent org for a detector cut: explicit hint, else the other fixture side."""
    hint = short.get("opponent")
    if isinstance(hint, str) and hint.strip():
        return canonical_ranking_name(hint)
    names = [n for n in (orgs or []) if n]
    if len(names) != 2:
        return None
    t1 = canonical_ranking_name(names[0])
    t2 = canonical_ranking_name(names[1])
    if not t1 or not t2 or t1 == t2:
        return None
    pov = short.get("pov_team")
    pov_canon = canonical_ranking_name(pov if isinstance(pov, str) else None)
    if pov_canon == t1:
        return t2
    if pov_canon == t2:
        return t1
    return None


def opponent_from_fixture(hint: str | None, match: dict | None) -> str | None:
    teams = fixture_teams(match)
    if not teams:
        return None
    hint_canon = canonical_ranking_name(hint)
    if hint_canon in teams:
        return hint_canon
    return None


def parse_stage(text: str | None) -> str | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    if "grand final" in raw:
        return "grand_final"
    if any(token in raw for token in ("quarter", "semi", "playoff", "final")):
        return "playoff"
    if any(token in raw for token in ("swiss", "opening", "group")):
        return "group"
    return None


_CLUTCH_FROM_CUT = {
    "1v5": "1v5_won",
    "1v4": "1v4_won",
    "1v3": "1v3_won",
    "2v5": "2vx_won",
    "2v4": "2vx_won",
    "2v3": "2vx_won",
}


def kinds_from_cut(short: dict) -> tuple[str, ...]:
    """Map a detector cut into Kind categories (same closed set as factory labels)."""
    st = str(short.get("short_type") or "").lower()
    clutch_raw = str(short.get("clutch_initial_count") or "").lower().replace(" ", "")
    nkill = len(short.get("kill_ticks") or [])
    kinds: list[str] = []
    clutch_kind = _CLUTCH_FROM_CUT.get(clutch_raw) or _CLUTCH_FROM_CUT.get(st)
    if clutch_kind:
        kinds.append(clutch_kind)
    if nkill >= 5:
        kinds.append("ace")
    elif nkill == 4:
        kinds.append("4k")
    for stack in ("flick", "perfect_shots", "wallbang", "knife", "defuse"):
        if st == stack:
            kinds.append(stack)
    if short.get("perfect_shots") and "perfect_shots" not in kinds:
        kinds.append("perfect_shots")
    return tuple(kinds)


def parse_kinds(label: str) -> tuple[str, ...]:
    text = label or ""
    kinds: list[str] = []
    for pattern, kind in _CLUTCH:
        if pattern.search(text):
            kinds.append(kind)
            break
    for pattern, kind in _MULTIKILL:
        if pattern.search(text):
            kinds.append(kind)
            break
    for pattern, kind in _STACK:
        if pattern.search(text):
            kinds.append(kind)
    return tuple(kinds)


def observation_from_allstar(clip: dict, match: dict | None = None) -> dict | None:
    if not isinstance(clip, dict):
        return None
    title = str(clip.get("title") or clip.get("label") or "")
    if _HIGHLIGHT_BOX.match(title.strip()):
        return None
    steamid = str(clip.get("steamid") or "").strip() or None
    player = str(clip.get("player") or "").strip() or None
    if not steamid and not player:
        return None
    label = str(clip.get("label") or clip.get("title") or "")
    match_id = str(clip.get("match_id") or "") or None
    if match and not match_id:
        match_id = str(match.get("match_id") or "") or None
    hint = clip.get("opponent_team")
    return {
        "source": "allstar",
        "clip_id": str(clip.get("clip_id") or ""),
        "steamid": steamid,
        "player": player,
        "match_id": match_id,
        "kinds": parse_kinds(label),
        "opponent": opponent_from_fixture(hint if isinstance(hint, str) else None, match),
        "stage": parse_stage(match.get("stage") if match else None),
        "label": label,
        "views": clip.get("views"),
        "title": str(clip.get("title") or ""),
        "round": clip.get("round"),
        "published_at": (match or {}).get("scraped_at") or (match or {}).get("published_at"),
    }


def observations_from_match_row(row: dict) -> list[dict]:
    match = {
        "match_id": row.get("match_id"),
        "slug": row.get("slug"),
        "stage": row.get("match_stage") or row.get("stage"),
        "teams": row.get("teams"),
        "scraped_at": row.get("scraped_at"),
        "published_at": row.get("published_at") or row.get("scraped_at"),
    }
    out: list[dict] = []
    for clip in row.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        obs = observation_from_allstar(clip, match)
        if obs:
            out.append(obs)
    return out


TO_CHANNELS = {
    "BLAST CS2 Highlights": "blast_highlights",
    "ESL CS2 Highlights": "esl_highlights",
    "PGL CS2 Highlights": "pgl_highlights",
    "StarLadder CS2 Highlights": "starladder_highlights",
    "EWC Extra": "ewc_extra",
    "BLAST": "blast_main",
}
_RECAP = re.compile(
    r"\bin\s+3\s+mins?\b|\bin\s+3\s+minutes\b|\bbest[\s-]?of\b",
    re.I,
)
_TALK = re.compile(
    r"\binterview\b|\brumou?rs?\b|\btalent\b|\bbts\b|\bbehind the scenes\b",
    re.I,
)


def observation_from_to(
    video: dict,
    match: dict | None = None,
    *,
    player_hint: str | None = None,
    opponent_hint: str | None = None,
    recognised: dict[str, str] | None = None,
) -> dict | None:
    """Clip Observation from an allowlist TO Short, or None if recap/talk/off-list."""
    if not isinstance(video, dict):
        return None
    title = str(video.get("title") or "")
    channel = str(video.get("channel") or "")
    source = TO_CHANNELS.get(channel)
    if not source:
        return None
    if _RECAP.search(title) or _TALK.search(title):
        return None
    if source == "ewc_extra" and "#cs2" not in title.lower():
        return None
    kinds = parse_kinds(title)
    if source == "blast_main" and not kinds:
        return None
    views = video.get("views")
    try:
        views_n = int(views)
    except (TypeError, ValueError):
        return None
    recognised = recognised or {}
    nick = (player_hint or "").strip()
    steamid = None
    player = None
    if nick:
        for name, sid in recognised.items():
            if str(name).casefold() == nick.casefold():
                player = str(name)
                steamid = str(sid)
                break
    hint = opponent_hint
    if isinstance(hint, str) and hint.startswith("@"):
        hint = None
    return {
        "source": source,
        "clip_id": str(video.get("video_id") or ""),
        "steamid": steamid,
        "player": player,
        "match_id": str((match or {}).get("match_id") or "") or None,
        "kinds": kinds,
        "opponent": opponent_from_fixture(hint, match),
        "stage": parse_stage((match or {}).get("stage") if match else None),
        "label": title,
        "views": views_n,
    }
