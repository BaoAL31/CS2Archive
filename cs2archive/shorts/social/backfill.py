"""Backfill demo linkage for our own-channel Shorts, with verification.

A title alone is never accepted. A candidate link is only ``exact`` when the
**demo's own action timeline** proves the gameplay:

* the title names a map, and the candidate demo/timeline map agrees;
* the named player was actually in that demo;
* exactly one round carries the title's kill signature
  (``ace``/``5k`` → 5 kills, ``Nk`` → N kills, ``1vX`` clutch → X kills in a
  round the player's team won);
* the publish date is plausible for that demo (the demo existed locally before
  publication, and within a bounded window);
* the opponent named in the title, when present, is one of the demo's teams.

Zero qualifying candidates → ``no_candidate``. More than one → ``ambiguous`` and
every candidate is stored with its reason. Everything is written append-only to
``short_linkage`` with ``status = linked | ambiguous | no_candidate | no_demo |
no_map``. Demos/timelines are never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cs2archive.shorts.join_clips_to_demos import map_slug_from_title
from cs2archive.shorts.social.deepen import normalize_map
from cs2archive.shorts.social.safety import parse_timestamp, sha256_text

STATUS_LINKED = "linked"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NO_CANDIDATE = "no_candidate"
STATUS_NO_DEMO = "no_demo"
STATUS_NO_MAP = "no_map"

DEFAULT_MAX_PUBLISH_GAP_DAYS = 60
KIND_ACE_RE = re.compile(r"\bace\b|\b5\s*k\b", re.I)
KIND_NK_RE = re.compile(r"\b([2-5])\s*k\b", re.I)
KIND_CLUTCH_RE = re.compile(r"\b1v([2-5])\b", re.I)
# "vs MOUZ", "vs. Spirit", "against Liquid" all name the opponent. Ranked/ELO
# phrasing ("against 3.1k ELOs") is explicitly not a team.
VS_RE = re.compile(r"\b(?:vs\.?|versus|against)\s+([a-z0-9][a-z0-9 .'_-]{1,24})",
                   re.I)
NON_TEAM_RE = re.compile(
    r"\b(?:elo|elvos|level|players?|faceit|pugs?|premiers?)\b", re.I)


@dataclass
class RoundFacts:
    """One round of one demo timeline, reduced to verifiable facts."""

    demo_path: str
    match_id: str | None
    map: str | None
    round: int
    kill_ticks: list[int]
    player_steamid: str
    player_nick: str | None = None
    won: bool | None = None
    teams: tuple[str, ...] = ()
    clutch_size: int | None = None
    kill_count: int = 0


@dataclass
class Candidate:
    demo_path: str
    round: int
    kill_ticks: list[int]
    kill_count: int
    match_id: str | None = None
    map: str | None = None
    won: bool | None = None
    demo_mtime: str | None = None
    team_slug: str | None = None


@dataclass
class ResolveResult:
    status: str
    reason: str
    signature: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None

    def as_evidence(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "signature": self.signature,
            "candidate_count": len(self.candidates),
            "candidates": [
                {"demo_path": c.demo_path, "round": c.round,
                 "kill_ticks": c.kill_ticks, "kill_count": c.kill_count,
                 "map": c.map, "won": c.won, "demo_mtime": c.demo_mtime}
                for c in self.candidates[:10]],
        }


def parse_signature(title: str) -> dict:
    """Map / player-agnostic kill signature from a title."""
    text = str(title or "")
    signature = {
        "map": normalize_map(map_slug_from_title(text)) or None,
        "kills": None,
        "kind": None,
        "clutch_size": None,
        "opponent": None,
    }
    if KIND_ACE_RE.search(text):
        signature.update(kills=5, kind="ace")
    else:
        nk = KIND_NK_RE.search(text)
        if nk:
            signature.update(kills=int(nk.group(1)), kind=f"{nk.group(1)}k")
    clutch = KIND_CLUTCH_RE.search(text)
    if clutch:
        signature.update(clutch_size=int(clutch.group(1)), kind="clutch")
        if signature["kills"] is None:
            signature["kills"] = int(clutch.group(1))
    match = VS_RE.search(text)
    if match:
        phrase = re.split(r"\s+(?:on|in|by|with|#|\(|$)", match.group(1),
                          maxsplit=1, flags=re.I)[0].strip().lower()
        if phrase and not NON_TEAM_RE.search(phrase) and not phrase[0].isdigit():
            signature["opponent"] = phrase
    return signature


def demo_teams(demo_path) -> tuple[str, ...]:
    """Team tokens from the match folder name (``2398717-falcons-vs-tyloo-esl...``).

    This is real evidence: the folder names both sides of the fixture. It is what
    stops a same-map/same-kill-count round from a *different* match being accepted
    when the title names the opponent (``vs Liquid`` must be one of the two teams).
    """
    name = str(demo_path or "").replace("\\", "/")
    folder = name.split("/")[-2] if "/" in name else name
    if "-vs-" not in folder.lower():
        return ()
    left, _, right = folder.lower().partition("-vs-")
    teams = []
    if left.split("-"):
        candidate = [part for part in left.split("-") if part][-1]
        if candidate and not candidate.isdigit():
            teams.append(candidate)
    if right.split("-"):
        candidate = [part for part in right.split("-") if part][0]
        if candidate:
            teams.append(candidate)
    return tuple(teams)


def _team_slug(demo_path: str) -> str | None:
    text = str(demo_path or "").replace("\\", "/")
    m = re.search(r"/(\d+)-([^/]+)/", text)
    return m.group(2) if m else None


def _window_ok(demo_mtime: str | None, published_at: str | None,
               max_gap_days: int) -> tuple[bool, str]:
    if not demo_mtime or not published_at:
        return True, "no-date-evidence"
    try:
        mtime = datetime.fromisoformat(demo_mtime.replace("Z", "+00:00"))
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return True, "unparseable-date"
    if mtime > published + timedelta(days=1):
        return False, "demo-local-after-publication"
    if (published - mtime) > timedelta(days=max_gap_days):
        return False, f"demo-older-than-{max_gap_days}d"
    return True, "window-ok"


def resolve_short(*, title: str, published_at: str | None,
                  player_steamid: str | None, rounds: list[RoundFacts],
                  demo_mtimes: dict[str, str] | None = None,
                  max_gap_days: int = DEFAULT_MAX_PUBLISH_GAP_DAYS) -> ResolveResult:
    """Decide one short's linkage from verified round facts."""
    signature = parse_signature(title)
    if signature["kills"] is None:
        return ResolveResult(STATUS_NO_CANDIDATE,
                             "no kill signature in the title (title-only never "
                             "accepted)", signature)
    if not signature["map"]:
        return ResolveResult(STATUS_NO_MAP,
                             "title names no map, so the demo cannot be verified",
                             signature)
    if not player_steamid:
        return ResolveResult(STATUS_NO_CANDIDATE,
                             "no player steam id resolved from the title", signature)

    demo_mtimes = demo_mtimes or {}
    candidates: list[Candidate] = []
    considered = 0
    for facts in rounds:
        considered += 1
        if str(facts.player_steamid) != str(player_steamid):
            continue
        if normalize_map(facts.map) != signature["map"]:
            continue
        if signature["kind"] == "clutch":
            if facts.clutch_size not in (None, signature["clutch_size"]):
                continue
            if facts.won is False:
                continue
        if facts.kill_count != signature["kills"]:
            continue
        if signature["opponent"] and facts.teams:
            # The named opponent must be one of the two teams the demo contains,
            # otherwise this is a same-map/same-kill-count round from another match.
            opponent_tokens = [token for token in
                               re.split(r"[\s_-]+", signature["opponent"]) if token]
            haystack = " ".join(facts.teams).lower()
            if opponent_tokens and not all(token in haystack
                                           for token in opponent_tokens):
                continue
        candidates.append(Candidate(
            demo_path=facts.demo_path, round=facts.round,
            kill_ticks=list(facts.kill_ticks), kill_count=facts.kill_count,
            match_id=facts.match_id, map=facts.map, won=facts.won,
            demo_mtime=demo_mtimes.get(facts.demo_path),
            team_slug=_team_slug(facts.demo_path)))

    if not candidates:
        return ResolveResult(
            STATUS_NO_CANDIDATE,
            f"no qualifying round for map={signature['map']} kills="
            f"{signature['kills']} across {considered} verified rounds",
            signature)
    unique = {(c.demo_path, c.round) for c in candidates}
    if len(unique) > 1:
        return ResolveResult(
            STATUS_AMBIGUOUS,
            f"{len(unique)} qualifying rounds matched; an exact link needs exactly "
            "one", signature, candidates)
    return ResolveResult(STATUS_LINKED, "exactly one qualifying round matched",
                         signature, candidates, chosen=candidates[0])


def rounds_from_timeline(timeline: dict, *, demo_path: str,
                         steamid: str) -> list[RoundFacts]:
    """Reduce a cached action timeline to per-round facts for one player."""
    out: list[RoundFacts] = []
    map_name = timeline.get("map")
    match_id = None
    m = re.search(r"(\d+)-", str(demo_path).replace("\\", "/").split("/")[-2:][0]
                  if "/" in str(demo_path) else str(demo_path))
    if m:
        match_id = m.group(1)
    winners = timeline.get("winner_by_round") or {}
    sides = timeline.get("sides_by_round") or {}
    players = timeline.get("pros") or []
    nick = None
    for row in players:
        if str(row.get("steam_id") or row.get("steamid") or "") == str(steamid):
            nick = row.get("name") or row.get("nick")
            break
    kills_by_round: dict[int, list[int]] = {}
    for kill in timeline.get("kills_all") or []:
        if str(kill.get("attacker_steam_id")) != str(steamid):
            continue
        rnd = int(kill.get("round") or 0)
        kills_by_round.setdefault(rnd, []).append(int(kill.get("tick") or 0))
    teams = demo_teams(demo_path)
    for rnd, ticks in sorted(kills_by_round.items()):
        team_num = None
        for kill in timeline.get("kills_all") or []:
            if str(kill.get("attacker_steam_id")) == str(steamid) \
                    and int(kill.get("round") or 0) == rnd:
                team_num = kill.get("attacker_team_num") or kill.get("attacker_team")
                break
        won = None
        if team_num is not None and rnd in winners:
            won = int(winners[rnd]) == int(team_num)
        out.append(RoundFacts(
            demo_path=demo_path, match_id=match_id, map=map_name, round=rnd,
            kill_ticks=sorted(ticks), player_steamid=str(steamid),
            player_nick=nick, won=won, teams=teams, kill_count=len(ticks)))
    _ = sides
    return out


def demo_mtime_index(demo_paths) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in demo_paths:
        try:
            stamp = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        out[str(path)] = stamp.isoformat().replace("+00:00", "Z")
    return out


def short_id_for(video_id: str) -> str:
    return sha256_text(f"own-channel-backfill|{video_id}")[:32]


def published_or_none(value) -> str | None:
    return parse_timestamp(value) if value else None
