"""Backfill verified demo linkage for our own-channel Shorts.

Flow per own-channel Short:

1. resolve the player nick → steam id from ``.data/player_accounts.json`` (the
   same registry the pipeline uses) by scanning the title;
2. parse the map + kill signature from the title (``parse_signature``);
3. collect **round facts** from action timelines for local demos restricted by
   map, player and a publish-date plausibility window;
4. ``resolve_short`` accepts only a single qualifying round as ``linked``;
   everything else is stored with its explicit reason
   (``ambiguous`` / ``no_candidate`` / ``no_map`` / ``no_demo``);
5. a ``linked`` result also writes an exact ``clip_identity_links`` row so the
   trainer's verified-only scored set can use it.

Timeline building is bounded by ``build_budget`` because it parses demos. Demos
and timelines are read-only here; nothing is deleted or rewritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cs2archive.shorts.social.backfill import (
    STATUS_AMBIGUOUS, STATUS_LINKED, STATUS_NO_CANDIDATE, STATUS_NO_DEMO,
    STATUS_NO_MAP, ResolveResult, demo_mtime_index, parse_signature,
    resolve_short, rounds_from_timeline, short_id_for,
)
from cs2archive.shorts.social.models import LinkRecord
from cs2archive.shorts.social.safety import iso, utcnow

PLAYER_ACCOUNTS = Path(".data/player_accounts.json")
RENDER_SELECTION_LEDGER = Path("youtube/.shorts_render_selection.json")
OWN_MAX_PUBLISH_GAP_DAYS = 45

_LEDGER_TIMELINE_RE = re.compile(
    r"renders[\\/]pov-(?P<stem>.+)_(?P<player>[^\\/]+?)[\\/]shorts[\\/]"
    r"(?P<short>[^\\/]+?)[\\/]short_timeline\.json$")
_LEDGER_TICK_RE = re.compile(r"-t(\d+)$")


def ledger_entries(path: Path | None = None) -> list[dict]:
    """Surviving render-selection ledger rows, parsed into demo evidence.

    The ledger records the demo stem and the player in the render directory name
    (``renders/pov-<demo-stem>_<player>/shorts/...``), which is authoritative
    provenance - unlike a title. It does **not** record the platform post id, so a
    ledger-derived record is staged ``pending`` until an upload supplies one.
    """
    try:
        payload = json.loads(Path(path or RENDER_SELECTION_LEDGER).read_text(
            encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for key, entry in (payload.get("entries") or {}).items():
        timeline = str(entry.get("timeline") or "")
        match = _LEDGER_TIMELINE_RE.search(timeline)
        if not match:
            continue
        features = entry.get("features") or {}
        kinds = [str(kind) for kind in features.get("kinds") or []]
        signature = parse_signature(" ".join(kinds) + " " + match.group("short"))
        if signature["kills"] is None:
            signature = parse_signature(match.group("short"))
        tick = None
        tick_match = _LEDGER_TICK_RE.search(match.group("short"))
        if tick_match:
            tick = int(tick_match.group(1))
        out.append({
            "ledger_key": key,
            "entry": entry,
            "demo_stem": match.group("stem"),
            "player_nick": match.group("player"),
            "short_token": match.group("short"),
            "player_steamid": str(features.get("steamid") or "") or None,
            "opponent": features.get("opponent"),
            "kinds": kinds,
            "map": _demo_map(Path(match.group("stem") + ".dem")),
            "kills": signature["kills"],
            "kill_kind": signature["kind"],
            "tick": tick,
            "reserved_day": entry.get("reserved_day"),
            "predicted_views": entry.get("predicted_views"),
        })
    return out


def link_ledger(store, *, entries: list[dict], demos_root: Path,
                timeline_dir: Path, run_id: str, build_budget: int = 0,
                log=print) -> dict:
    """Verify ledger entries against local demos and stage them as linkage."""
    stats = {"checked": len(entries), "verified": 0, "no_demo": 0,
             "no_candidate": 0, "ambiguous": 0, "pending_post_id": 0,
             "timelines_built": 0}
    by_stem: dict[str, Path] = {}
    for demo in _demo_files(demos_root):
        by_stem.setdefault(demo.stem, demo)
    for entry in entries:
        demo = by_stem.get(entry["demo_stem"])
        if demo is None:
            stats["no_demo"] += 1
            _stage_ledger(store, entry, status="no_demo", run_id=run_id,
                          reason=f"local demo '{entry['demo_stem']}.dem' is not in "
                                 "the corpus")
            continue
        timeline_path = _timeline_for(demo, timeline_dir)
        if not timeline_path.is_file():
            if stats["timelines_built"] >= max(0, build_budget):
                continue
            if not _build_timeline(demo, timeline_path, log=log):
                continue
            stats["timelines_built"] += 1
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        steam_id = entry["player_steamid"]
        if not steam_id:
            stats["no_candidate"] += 1
            _stage_ledger(store, entry, status="no_candidate", run_id=run_id,
                          reason="ledger has no steam id for this player")
            continue
        facts = rounds_from_timeline(timeline, demo_path=str(demo), steamid=steam_id)
        hits = [f for f in facts if f.kill_count == entry["kills"]
                and (entry["tick"] is None
                     or any(abs(t - entry["tick"]) <= 320 for t in f.kill_ticks))]
        if len(hits) != 1:
            stats["ambiguous" if hits else "no_candidate"] += 1
            _stage_ledger(store, entry,
                          status=("ambiguous" if hits else "no_candidate"),
                          run_id=run_id,
                          reason=(f"{len(hits)} ledger rounds matched the recorded "
                                  "kill count/tick"),
                          demo=str(demo))
            continue
        stats["verified"] += 1
        stats["pending_post_id"] += 1
        chosen = hits[0]
        _stage_ledger(store, entry, status="pending", run_id=run_id,
                      reason=("ledger proves the demo/round; the platform post id is "
                              "not recorded in the ledger"),
                      demo=str(demo), round_no=chosen.round,
                      kill_ticks=chosen.kill_ticks)
    return stats


def _stage_ledger(store, entry: dict, *, status: str, run_id: str, reason: str,
                  demo: str | None = None, round_no: int | None = None,
                  kill_ticks: list | None = None) -> None:
    record = {
        "short_id": short_id_for(
            f"ledger|{entry['ledger_key']}|{demo}|{round_no}"),
        "demo_path": demo or "",
        "match_id": None,
        "map": entry.get("map"),
        "player_steamid": entry.get("player_steamid"),
        "player_nick": entry.get("player_nick"),
        "round": round_no,
        "tick_start": None,
        "tick_end": None,
        "kill_ticks": kill_ticks or [],
        "kill_count": len(kill_ticks or []) or entry.get("kills"),
        "short_type": entry.get("kill_kind"),
        "source": "render_selection_ledger",
        "selection": {"ledger_key": entry["ledger_key"],
                      "predicted_views": entry.get("predicted_views"),
                      "reserved_day": entry.get("reserved_day")},
        "reason": reason,
        "evidence": {
            "ledger_key": entry["ledger_key"],
            "demo_stem": entry["demo_stem"],
            "short_token": entry["short_token"],
            "demo_path": demo,
            "round": round_no,
            "kill_ticks": kill_ticks or [],
            "kinds": entry.get("kinds"),
            "opponent": entry.get("opponent"),
            "reserved_day": entry.get("reserved_day"),
            "status": status,
        },
    }
    store.upsert_short_linkage(record, status=status, run_id=run_id)


def load_player_registry(path: Path | None = None) -> dict[str, str]:
    """lowercased nick → steam id, plus aliases (faceit nickname, steam vanity).

    ``path`` resolves at call time so a caller (or a test) can point the registry
    somewhere else without rebinding the module constant.
    """
    try:
        rows = json.loads(Path(path or PLAYER_ACCOUNTS).read_text(
            encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        steam_id = str(row.get("steam_id") or "")
        if not steam_id:
            continue
        for key in ("nickname", "faceit_nickname"):
            name = str(row.get(key) or "").strip().lower()
            if len(name) >= 3:
                out.setdefault(name, steam_id)
        url = str(row.get("steam_url") or "").rstrip("/")
        if url:
            out.setdefault(url.rsplit("/", 1)[-1].lower(), steam_id)
    return out


def resolve_player(title: str, registry: dict[str, str]) -> tuple[str | None, str | None]:
    """Longest matching nickname in the title (never a substring of another)."""
    text = str(title or "").lower()
    for name in sorted(registry, key=lambda n: -len(n)):
        if len(name) < 3:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text):
            return registry[name], name
    return None, None


def _demo_files(demos_root: Path) -> list[Path]:
    root = Path(demos_root)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.dem"))


def _timeline_for(demo: Path, timeline_dir: Path) -> Path:
    return Path(timeline_dir) / f"{demo.parent.name}__{demo.stem}.json"


def own_videos(store, *, own_channel_id: str) -> list[dict]:
    rows = store.conn.execute(
        "SELECT post_id, caption, published_at, duration_seconds, format_label "
        "FROM posts WHERE platform='youtube' AND creator_id=?", (own_channel_id,))
    return [dict(row) for row in rows]


def backfill_own_channel(store, *, config, limit: int = 0, build_budget: int = 0,
                         demos_root: Path = Path("demos/hltv"),
                         timeline_dir: Path = Path("data/shorts-foundation/_timelines"),
                         prefer_ledger: bool = True, log=print) -> dict:
    own_channel_id = str(config.platform("youtube").extra.get("own_channel_id") or "")
    registry = load_player_registry()
    videos = own_videos(store, own_channel_id=own_channel_id)
    if limit:
        videos = videos[:limit]
    demos = _demo_files(Path(demos_root))
    mtimes = demo_mtime_index(demos)
    run_id = f"own-backfill-{iso(utcnow()).replace(':', '')[:15]}"
    store.ensure_run(run_id, "youtube", mode="backfill")

    ledger_stats = None
    if prefer_ledger:
        # The surviving render ledger is authoritative evidence and is consumed
        # first, before any title-based inference.
        entries = ledger_entries()
        ledger_stats = link_ledger(
            store, entries=entries, demos_root=Path(demos_root),
            timeline_dir=Path(timeline_dir), run_id=run_id,
            build_budget=build_budget, log=log)

    stats = {
        "run_id": run_id, "own_channel_id": own_channel_id, "videos": len(videos),
        "ledger": ledger_stats,
        "demos": len(demos), "registry_players": len(registry),
        "timelines_cached": 0, "timelines_built": 0, "build_budget": build_budget,
        "by_status": {}, "results": [], "no_player": 0,
    }
    needed_maps: dict[str, set[str]] = {}
    prepared: list[dict] = []
    for video in videos:
        title = str(video.get("caption") or "")
        steam_id, nick = resolve_player(title, registry)
        signature = parse_signature(title)
        prepared.append({"video": video, "title": title, "steam_id": steam_id,
                         "nick": nick, "signature": signature})
        if steam_id and signature["map"]:
            needed_maps.setdefault(signature["map"], set()).add(steam_id)

    # Only demos that could matter for some short get a timeline: the map must be
    # needed AND the demo must have been on disk inside the publish-date
    # plausibility envelope (a demo downloaded long after a short published can
    # never be that short's match).
    envelope = _publish_envelope(prepared, OWN_MAX_PUBLISH_GAP_DAYS)
    wanted_demos: list[Path] = []
    for demo in demos:
        # The map lives in the demo FILE name (`...-m1-ancient.dem`), not in the
        # match folder name.
        slug = _demo_map(demo)
        if not slug or slug not in needed_maps:
            continue
        if not _in_envelope(mtimes.get(str(demo)), envelope):
            continue
        wanted_demos.append(demo)
    stats["demos_in_envelope"] = len(wanted_demos)

    rounds_by_demo: dict[str, list] = {}
    for demo in wanted_demos:
        timeline_path = _timeline_for(demo, Path(timeline_dir))
        if timeline_path.is_file():
            stats["timelines_cached"] += 1
        else:
            if stats["timelines_built"] >= max(0, build_budget):
                continue
            if not _build_timeline(demo, timeline_path, log=log):
                continue
            stats["timelines_built"] += 1
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        steamids = set()
        for row in timeline.get("pros") or []:
            sid = str(row.get("steam_id") or row.get("steamid") or "")
            if sid:
                steamids.add(sid)
        sid_candidates = {s for s in needed_maps.get(_demo_map(demo), set())}
        for sid in (steamids & sid_candidates) if sid_candidates else steamids:
            rounds_by_demo.setdefault(str(demo), []).extend(
                rounds_from_timeline(timeline, demo_path=str(demo), steamid=sid))

    all_rounds = [facts for facts in rounds_by_demo.values() for facts in facts]
    for item in prepared:
        if item["steam_id"] is None:
            stats["no_player"] += 1
        result = resolve_short(
            title=item["title"], published_at=item["video"].get("published_at"),
            player_steamid=item["steam_id"], rounds=all_rounds,
            demo_mtimes=mtimes, max_gap_days=OWN_MAX_PUBLISH_GAP_DAYS)
        record = _persist(store, item, result, run_id=run_id)
        stats["by_status"][result.status] = stats["by_status"].get(result.status, 0) + 1
        stats["results"].append(record)
    return stats


def _publish_envelope(prepared: list[dict], gap_days: int):
    """[oldest publish - gap, newest publish + 1d] over the shorts we can link."""
    from datetime import datetime, timedelta

    dates = []
    for item in prepared:
        published = item["video"].get("published_at")
        if not published or not item["steam_id"]:
            continue
        try:
            dates.append(datetime.fromisoformat(str(published).replace("Z", "+00:00")))
        except ValueError:
            continue
    if not dates:
        return None
    return (min(dates) - timedelta(days=gap_days),
            max(dates) + timedelta(days=1))


def _in_envelope(mtime: str | None, envelope) -> bool:
    from datetime import datetime

    if envelope is None or not mtime:
        return True
    try:
        stamp = datetime.fromisoformat(str(mtime).replace("Z", "+00:00"))
    except ValueError:
        return True
    return envelope[0] <= stamp <= envelope[1]


def _demo_map(demo: Path) -> str:
    """Map slug for a demo file (``falcons-vs-tyloo-m1-ancient.dem`` -> anubis...)."""
    from cs2archive.shorts.social.deepen import title_map_slug

    return title_map_slug(Path(demo).stem) or ""


def _build_timeline(demo: Path, output: Path, *, log=print) -> bool:
    """Bounded, non-fatal action-timeline build for one local demo."""
    try:
        from cs2archive.highlights import build_action_timeline as atl

        output.parent.mkdir(parents=True, exist_ok=True)
        return bool(atl.ensure_action_timeline(demo, output=output))
    except Exception as exc:  # noqa: BLE001 - a bad demo must not stop the pass
        log(f"[backfill][WARN] timeline failed for {demo.name}: "
            f"{type(exc).__name__}: {exc}")
        return False


def _persist(store, item: dict, result: ResolveResult, *, run_id: str) -> dict:
    video = item["video"]
    post_id = str(video["post_id"])
    chosen = result.chosen
    evidence = result.as_evidence()
    evidence.update({
        "video_id": post_id,
        "title": item["title"],
        "nick": item["nick"],
        "player_steamid": item["steam_id"],
        "published_at": video.get("published_at"),
    })
    record = {
        "short_id": short_id_for(f"backfill|{post_id}"),
        "platform": "youtube",
        "post_id": post_id,
        "demo_path": chosen.demo_path if chosen else "",
        "match_id": chosen.match_id if chosen else None,
        "map": chosen.map if chosen else item["signature"]["map"],
        "player_steamid": item["steam_id"],
        "player_nick": item["nick"],
        "round": chosen.round if chosen else None,
        "tick_start": None,
        "tick_end": None,
        "kill_ticks": chosen.kill_ticks if chosen else [],
        "kill_count": chosen.kill_count if chosen else None,
        "short_type": item["signature"]["kind"],
        "source": "own-channel-backfill",
        "selection": {"entry": "backfill_own_channel"},
        "reason": result.reason,
        "evidence": evidence,
    }
    store.upsert_short_linkage(record, status=result.status, run_id=run_id)
    status = result.status
    if status == STATUS_LINKED and chosen is not None:
        store.upsert_link(LinkRecord(
            platform="youtube", post_id=post_id,
            foundation_clip_id=f"backfill-{post_id}", match_id=chosen.match_id,
            method="own_channel_demo_backfill", confidence=1.0, ambiguous=False,
            evidence={"demo_path": chosen.demo_path, "round": chosen.round,
                      "kill_ticks": chosen.kill_ticks,
                      "short_type": item["signature"]["kind"]}), run_id=run_id)
        _ = status
    return {
        "video_id": post_id, "title": item["title"][:70], "status": status,
        "reason": result.reason, "map": record["map"],
        "round": record["round"], "demo": (record["demo_path"] or "")[-60:],
        "candidates": len(result.candidates),
    }


STATUSES = (STATUS_LINKED, STATUS_AMBIGUOUS, STATUS_NO_CANDIDATE, STATUS_NO_MAP,
            STATUS_NO_DEMO)
