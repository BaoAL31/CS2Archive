"""Stage additional Allstar clips against *existing* cached action timelines.

No demos are downloaded and no rendering happens: the aligner reads the
preserved Allstar probe payloads plus the already-built
``data/shorts-foundation/_timelines`` caches and stages extra
``clips/<clip_id>/`` folders (raw payload, match, alignment, events).

Only **exact** alignments are staged, and exactness now requires *all* of:

1. the clip's implied kill count (title or Allstar kind, e.g. ``4k``/``ace``)
   equals the player's demo kills in that round;
2. the clip title names a map (``map_slug_from_title``) **and** that map agrees
   with the candidate timeline's map after normalisation (``de_``/``dust_2``/
   ``dust 2`` all fold to ``dust2``);
3. exactly **one** cached timeline qualifies.

Rule 3 exists because a multi-map series or a split demo (p1/p2) can contain a
round with the same number and the same kill count on a different map or part.
Picking "the first hit" is how 31 clips were previously aligned to the wrong map,
so ambiguity is now rejected instead of resolved. A title without a map is never
stamped ``exact``.

``state.parquet`` is not written (it needs demo parsing); alignment records that
gap explicitly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from cs2archive.shorts.social.safety import dumps, sha256_text
from cs2archive.shorts.join_clips_to_demos import map_slug_from_title

TIMELINE_VERSION_MIN = 3
EVENT_KEYS = ("player_death", "player_hurt", "weapon_fire")
KIND_KILLS_RE = re.compile(r"^([1-5])k$")


@dataclass
class StageStats:
    staged: int = 0
    skipped_existing: int = 0
    rejected_no_expected: int = 0
    rejected_no_round: int = 0
    rejected_no_exact: int = 0
    rejected_no_map: int = 0
    rejected_map_mismatch: int = 0
    rejected_ambiguous: int = 0
    candidates: int = 0
    timelines_matched: int = 0
    by_clip_kills: dict | None = None

    def as_dict(self) -> dict:
        return {**self.__dict__, "by_clip_kills": self.by_clip_kills or {}}


def normalize_map(value) -> str:
    """Fold ``de_dust2`` / ``dust_2`` / ``Dust 2`` / ``dust2`` onto one key."""
    text = str(value or "").strip().lower()
    if text.startswith("de_"):
        text = text[3:]
    elif text.startswith("de "):
        text = text[3:]
    return re.sub(r"[\s_\-]+", "", text)


def title_map_slug(title) -> str | None:
    """Map slug from a title, tolerating ``dust_2``-style separators."""
    text = str(title or "")
    slug = map_slug_from_title(text) or map_slug_from_title(re.sub(r"[_]+", " ", text))
    return normalize_map(slug) if slug else None


def expected_kills(clip: dict) -> int | None:
    """Implied kill count from the title first, then the Allstar kinds."""
    title = str(clip.get("title") or "")
    kinds = [str(kind) for kind in clip.get("kinds") or []]
    if re.search(r"\bace\b", title, re.I) or "ace" in kinds:
        return 5
    match = re.search(r"\b([1-5])\s*k\b", title, re.I)
    if match:
        return int(match.group(1))
    for kind in kinds:
        match = KIND_KILLS_RE.match(kind)
        if match:
            return int(match.group(1))
    return None


def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def timeline_index(timeline_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in sorted(Path(timeline_dir).glob("*.json")):
        match = re.match(r"(\d+)-", path.name)
        if match:
            index.setdefault(match.group(1), []).append(path)
    return index


def _window(round_info: dict, kill_ticks: list[int]) -> tuple[int, int]:
    start, end = int(round_info["start_tick"]), int(round_info["end_tick"])
    start = max(0, start - 320)
    end = end + 320
    if kill_ticks:
        start = max(start, kill_ticks[0] - 320)
        end = min(end, kill_ticks[-1] + 128)
    return start, end


def _events_window(timeline: dict, start: int, end: int,
                   kills: list[dict], deaths: list[dict]) -> dict:
    events = {"kills": kills, "deaths": deaths}
    raw = timeline.get("raw_events") or {}
    for key in EVENT_KEYS:
        rows = (raw.get(key) or {}).get("rows") or []
        events[key] = [row for row in rows
                       if start <= int(row.get("tick", -1)) <= end]
    return events


def _probe_rows(store_path: Path):
    with Path(store_path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def stage_additional_clips(store_path: Path, foundation_dir: Path, *,
                           max_new: int = 600) -> StageStats:
    foundation_dir = Path(foundation_dir)
    index = timeline_index(foundation_dir / "_timelines")
    clips_dir = foundation_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    stats = StageStats(by_clip_kills={})
    timelines: dict[Path, dict] = {}
    rejected_ids: set[str] = set()

    candidates: list[dict] = []
    for row in _probe_rows(Path(store_path)):
        match_id = str(row.get("match_id") or "")
        paths = index.get(match_id)
        if not paths:
            continue
        for clip in row.get("clips") or []:
            if not isinstance(clip, dict) or not clip.get("clip_id"):
                continue
            if expected_kills(clip) is None:
                stats.rejected_no_expected += 1
                continue
            wanted_map = title_map_slug(clip.get("title"))
            if wanted_map is None:
                # No map in the title => the map cannot be verified => not exact.
                stats.rejected_no_map += 1
                continue
            clip_id = str(clip["clip_id"])
            if clip_id in rejected_ids:
                continue
            candidates.append({"clip": clip, "paths": paths,
                               "match_id": match_id, "row": row,
                               "wanted_map": wanted_map})
    stats.candidates = len(candidates)
    stats.timelines_matched = len(index)

    # Prefer the most recent observation, then deterministic id ordering.
    candidates.sort(key=lambda item: str(item["clip"]["clip_id"]))
    candidates.sort(key=lambda item: (
        str(item["clip"].get("observed_at") or item["row"].get("scraped_at") or "")),
        reverse=True)

    for item in candidates:
        if stats.staged >= max_new:
            break
        clip = item["clip"]
        clip_id = str(clip["clip_id"])
        out = clips_dir / clip_id
        if out.exists():
            stats.skipped_existing += 1
            continue
        want = expected_kills(clip)
        wanted_map = item["wanted_map"]
        stat = str(clip.get("steamid") or "")
        round_no = int(clip.get("round") or 0)
        matches: list[tuple] = []
        saw_round = False
        saw_map_mismatch = False
        for path in item["paths"]:
            timeline = timelines.get(path)
            if timeline is None:
                timeline = _load_json(path)
                timelines[path] = timeline
            if int(timeline.get("timeline_version") or 0) < TIMELINE_VERSION_MIN:
                continue
            if normalize_map(timeline.get("map")) != wanted_map:
                saw_map_mismatch = True
                continue  # wrong map: never a candidate for this clip
            bounds = {int(r["round"]): r for r in timeline.get("rounds") or []
                      if r.get("end_tick")}
            if round_no not in bounds:
                continue
            saw_round = True
            kills = [k for k in timeline.get("kills_all") or []
                     if str(k.get("attacker_steam_id")) == stat
                     and int(k.get("round") or 0) == round_no]
            if len(kills) == want and want > 0:
                matches.append((path, timeline, bounds[round_no], kills))
        if not matches:
            # Distinguish the reasons for the report instead of guessing.
            if saw_round:
                stats.rejected_no_exact += 1
            elif saw_map_mismatch:
                stats.rejected_map_mismatch += 1
            else:
                stats.rejected_no_round += 1
            rejected_ids.add(clip_id)
            continue
        if len(matches) > 1:
            # Same round + same kill count on two timelines (split demo p1/p2,
            # or the same map twice in a series): ambiguous, so refuse.
            stats.rejected_ambiguous += 1
            rejected_ids.add(clip_id)
            continue

        path, timeline, round_info, kills = matches[0]
        kill_ticks = sorted(int(k["tick"]) for k in kills)
        start, end = _window(round_info, kill_ticks)
        deaths = [k for k in timeline.get("kills_all") or []
                  if str(k.get("victim_steam_id")) == stat
                  and int(k.get("round") or 0) == round_no]
        events = _events_window(timeline, start, end, kills, deaths)
        out.mkdir(parents=True, exist_ok=True)
        (out / "raw_allstar.json").write_text(
            json.dumps(clip, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "match.json").write_text(json.dumps({
            "match_id": item["match_id"], "demo": timeline.get("demo_path"),
            "map": timeline.get("map"), "stage": clip.get("stage"),
            "timeline": str(path),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "alignment.json").write_text(json.dumps({
            "clip_id": clip_id, "demo": timeline.get("demo_path"),
            "match_id": item["match_id"], "steamid": stat,
            "player": clip.get("player"), "round": round_no,
            "tick_start": start, "tick_end": end,
            "round_tick_start": int(round_info["start_tick"]) - 320,
            "round_tick_end": int(round_info["end_tick"]) + 320,
            "kill_ticks": kill_ticks,
            "title": clip.get("title"), "views": clip.get("views"),
            "explicit_kills": want, "demo_kills": len(kills),
            "confidence": "exact",
            "map_verified": True,
            "title_map": wanted_map,
            "timeline_map": timeline.get("map"),
            "method": "steamid+kind/title-kills+title-map+round+cached-timeline",
            "timeline": str(path),
            "state_missing": True,
            "kinds": clip.get("kinds") or [],
            "evidence_hash": sha256_text(dumps({
                "clip_id": clip_id, "round": round_no, "kills": kill_ticks,
                "timeline": str(path)})),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        pd.DataFrame([{"key": k, "payload": json.dumps(v, ensure_ascii=False)}
                      for k, v in events.items()]).to_parquet(
            out / "events.parquet", index=False)
        stats.staged += 1
        stats.by_clip_kills[want] = stats.by_clip_kills.get(want, 0) + 1

    return stats
