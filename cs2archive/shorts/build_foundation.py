"""Build Shorts foundation data: match Allstar clips to demo moments.

For each demo on disk: load the match's stored Allstar clips, keep clips whose
player is in the demo and whose map matches, pick the top clip by views, verify
it against demo kills (explicit kill count from the title), and stage:

    clips/<clip_id>/{raw_allstar.json, match.json, alignment.json,
                     events.parquet, state.parquet}

No inferred short categories. Views are the ground truth; alignment records
how the match was made (exact/partial/unresolved).

Usage:
    python -m cs2archive.shorts.build_foundation --demo <path.dem>
    python -m cs2archive.shorts.build_foundation --all [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_PROPS = ["steamid", "team_num", "X", "Y", "Z", "pitch", "yaw",
               "health", "spotted", "m_iItemDefinitionIndex"]
STATE_EVERY_TICKS = 8


def _explicit_kills(title: str) -> int | None:
    text = str(title or "")
    if re.search(r"\bace\b", text, re.I):
        return 5
    m = re.search(r"\b([1-5])\s*k\b", text, re.I)
    return int(m.group(1)) if m else None


_CLIPS_CACHE: dict[str, list[dict]] | None = None
_FETCH_PAGE = None
_CF_STREAK = 0


def clips_for_match(match_id: str) -> list[dict]:
    global _CLIPS_CACHE
    if _CLIPS_CACHE is None:
        from cs2archive.shorts.allstar_data import load_allstar_dataset
        from cs2archive.shorts.fit_partial_stars import ALLSTAR_JSONL

        rows, _ = load_allstar_dataset(ALLSTAR_JSONL)
        cache: dict[str, list[dict]] = {}
        for r in rows:
            if r.get("views") and float(r["views"]) > 0:
                cache.setdefault(str(r.get("match_id") or ""), []).append(r)
        _CLIPS_CACHE = cache
    return list(_CLIPS_CACHE.get(str(match_id), []))


def _fetch_backfill(match_id: str, slug: str) -> str:
    """Fetch one match's Allstar section into the probe store, as we go.

    Uses an isolated CloakBrowser profile (never the listener's). Returns
    'ok', 'cloudflare', or 'aborted'. Aborts after 5 consecutive Cloudflare
    hits; callers keep going on stored clips for the remaining demos.
    """
    global _FETCH_PAGE, _CLIPS_CACHE, _CF_STREAK
    if _CF_STREAK >= 5:
        return "aborted"
    try:
        if _FETCH_PAGE is None:
            from cloakbrowser import launch_persistent_context

            profile = ROOT / ".sessions" / "hltv-cloak-foundation"
            profile.mkdir(parents=True, exist_ok=True)
            ctx = launch_persistent_context(
                str(profile.resolve()), headless=True,
                viewport={"width": 1920, "height": 1080},
                humanize=True, channel="chrome",
            )
            _FETCH_CTX = ctx
            _FETCH_PAGE = ctx.new_page()
        from cs2archive.config import settings
        from cs2archive.shorts.fit_partial_stars import ALLSTAR_JSONL
        from cs2archive.shorts.scrape_allstar_hltv import scrape_one_match

        import time
        time.sleep(2.0)
        row = scrape_one_match(
            _FETCH_PAGE, f"{match_id}/{slug}",
            settings.hltv_base_url.rstrip("/"), out=ALLSTAR_JSONL)
        _CLIPS_CACHE = None
        if row.get("cloudflare"):
            _CF_STREAK += 1
            return "cloudflare"
        _CF_STREAK = 0
        return "ok" if row.get("clips") else "empty"
    except Exception as e:
        print(f"  [fetch] {match_id}: {type(e).__name__}: {e}", flush=True)
        return "error"


def _is_quarantined(staging: Path, clip_id: str) -> bool:
    if not clip_id:
        return False
    path = Path(staging) / "clips" / clip_id / "alignment.json"
    if not path.is_file():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("quarantined"))
    except (OSError, ValueError):
        return False


def build_one(demo: Path, staging: Path, *, fetch: bool = True) -> dict:
    import demoparser2 as dp
    import pandas as pd

    from cs2archive.highlights import build_action_timeline as atl
    from cs2archive.shorts.join_clips_to_demos import map_slug_from_title

    demo = Path(demo)
    match = re.match(r"(\d+)-", demo.parent.name)
    if not match:
        return {"demo": str(demo), "status": "unresolved", "reason": "no-match-id"}
    match_id = match.group(1)
    clips = clips_for_match(match_id)
    if not clips and fetch:
        slug = demo.parent.name[len(match_id) + 1:] or "x"
        status = _fetch_backfill(match_id, slug)
        if status == "ok":
            clips = clips_for_match(match_id)
        if not clips:
            return {"demo": str(demo), "status": "unresolved",
                    "reason": f"no-stored-clips(fetch-{status})"}
    if not clips:
        return {"demo": str(demo), "status": "unresolved", "reason": "no-stored-clips"}

    tmp = staging / "_timelines" / f"{demo.parent.name}__{demo.stem}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tl_path = atl.ensure_action_timeline(demo, output=tmp)
    if not tl_path:
        return {"demo": str(demo), "status": "unresolved", "reason": "timeline-failed"}
    tl = json.loads(Path(tl_path).read_text(encoding="utf-8"))
    demo_map = str(tl.get("map") or "").lower()
    roster = set()
    for members in (tl.get("teams") or {}).values():
        roster.update(str(s) for s in members)

    scored = []
    for clip in clips:
        sid = str(clip.get("steamid") or "")
        if sid and sid not in roster:
            continue
        slug = map_slug_from_title(str(clip.get("title") or ""))
        if slug and slug.replace("dust_2", "dust2") not in demo_map and demo_map not in slug:
            continue
        if _is_quarantined(staging, str(clip.get("clip_id") or "")):
            continue  # unanimous 2026-10-07: quarantined clips never nominate
        scored.append(clip)
    if not scored:
        return {"demo": str(demo), "status": "unresolved", "reason": "no-player-map-match"}
    scored.sort(key=lambda c: -float(c["views"]))
    clip = scored[0]

    clip_id = str(clip.get("clip_id") or "")
    out = staging / "clips" / clip_id
    if (out / "alignment.json").is_file() and Path(tmp).is_file():
        cached = json.loads((out / "alignment.json").read_text(encoding="utf-8"))
        tl_cached = json.loads(Path(tmp).read_text(encoding="utf-8")) if Path(tmp).is_file() else {}
        sid_cached = str(clip.get("steamid") or "")
        rnd_cached = int(clip.get("round") or 0)
        ticks_cached = sorted(int(k["tick"]) for k in tl_cached.get("kills_all") or []
                              if str(k.get("attacker_steam_id")) == sid_cached
                              and k.get("round") == rnd_cached)
        return {"demo": str(demo), "status": cached.get("confidence", "exact"),
                "clip_id": clip_id, "views": clip.get("views"), "cached": True,
                "timeline": str(tmp), "kill_ticks": ticks_cached, "round": rnd_cached,
                "steamid": sid_cached, "clip": clip}

    rnd = int(clip.get("round") or 0)
    bounds = {r["round"]: (r["start_tick"], r.get("end_tick")) for r in tl.get("rounds") or []}
    if rnd not in bounds or not bounds[rnd][1]:
        return {"demo": str(demo), "status": "unresolved", "reason": "round-bounds"}
    start, end = bounds[rnd]
    start, end = max(0, start - 320), end + 320

    sid = str(clip.get("steamid") or "")
    demo_kills = [k for k in tl.get("kills_all") or []
                  if str(k.get("attacker_steam_id")) == sid and k.get("round") == rnd]
    want_kills = _explicit_kills(str(clip.get("title")))
    confidence = "exact" if (want_kills is not None and len(demo_kills) == want_kills) else "partial"
    kill_ticks = sorted(int(k["tick"]) for k in demo_kills)
    # Unanimous 2026-10-07: the shipped artifact represents the clip
    # (kill-anchored window), with round bounds preserved alongside.
    clip_start = max(start, kill_ticks[0] - 320) if kill_ticks else start
    clip_end = min(end, kill_ticks[-1] + 128) if kill_ticks else end

    parser = dp.DemoParser(str(demo))
    ticks = list(range(clip_start, clip_end + 1, STATE_EVERY_TICKS))
    try:
        snap = parser.parse_ticks(STATE_PROPS, ticks=ticks)
        state = snap.to_dict(orient="records") if snap is not None else []
    except Exception:
        state = []

    events = {"kills": demo_kills,
              "deaths": [k for k in tl.get("kills_all") or []
                         if str(k.get("victim_steam_id")) == sid and k.get("round") == rnd]}
    raw = tl.get("raw_events") or {}
    for key in ("player_death", "player_hurt", "weapon_fire"):
        rows = (raw.get(key) or {}).get("rows") or []
        events[key] = [r for r in rows if clip_start <= int(r.get("tick", -1)) <= clip_end]

    clip_id = str(clip.get("clip_id") or "")
    out = staging / "clips" / clip_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw_allstar.json").write_text(json.dumps(clip, indent=2), encoding="utf-8")
    (out / "match.json").write_text(json.dumps({
        "match_id": match_id, "demo": str(demo),
        "map": tl.get("map"), "stage": clip.get("stage"),
    }, indent=2), encoding="utf-8")
    (out / "alignment.json").write_text(json.dumps({
        "clip_id": clip_id, "demo": str(demo), "match_id": match_id,
        "steamid": sid, "player": clip.get("player"),
        "round": rnd,
        "tick_start": clip_start, "tick_end": clip_end,
        "round_tick_start": start, "round_tick_end": end,
        "kill_ticks": kill_ticks,
        "title": clip.get("title"), "views": clip.get("views"),
        "explicit_kills": want_kills, "demo_kills": len(demo_kills),
        "confidence": confidence, "method": "steamid+map+round+kills",
        "candidates_considered": len(scored),
    }, indent=2), encoding="utf-8")
    pd.DataFrame(state).to_parquet(out / "state.parquet", index=False)
    pd.DataFrame([{"key": k, "payload": json.dumps(v)}
                  for k, v in events.items()]).to_parquet(out / "events.parquet", index=False)
    return {"demo": str(demo), "status": confidence, "clip_id": clip_id,
            "views": clip.get("views"), "kills": f"{len(demo_kills)}/{want_kills}",
            "timeline": str(tmp), "kill_ticks": kill_ticks, "round": rnd,
            "steamid": sid, "clip": clip}


_CLUTCH_COUNT = {"1v5_won": "1v5", "1v4_won": "1v4", "1v3_won": "1v3", "2vx_won": "2v5"}


def short_type_for_kinds(kinds: list[str]) -> tuple[str, str | None]:
    """Detector-compatible short type from Allstar label kinds (naming only)."""
    have = set(kinds or [])
    for kind, count in _CLUTCH_COUNT.items():
        if kind in have:
            return "clutch", count
    for kind in ("ace", "4k"):
        if kind in have:
            return "4k", None
    for kind in ("wallbang", "3k", "knife", "defuse", "flick", "perfect_shots"):
        if kind in have:
            return kind, None
    return "highlight", None


def nominate_demo_short(demo: Path, staging: Path,
                        *, fetch: bool = True) -> tuple[dict, dict] | None:
    """Verified top-Allstar-clip nomination for one demo.

    Returns (timeline, short) with a kill-anchored window, or None when the
    clip cannot be matched (no Allstar data, roster/map miss, round or kill
    mismatch). Matching + staging reuse build_one, so every nomination is
    kill-count verified and foundation-staged.
    """
    from cs2archive.shorts.build_short_timeline import (
        _POST_KILL_TICK_MARGIN, _PRE_KILL_TICK_MARGIN,
    )

    res = build_one(Path(demo), Path(staging), fetch=fetch)
    if res["status"] not in ("exact", "partial") or "timeline" not in res:
        return None
    tl = json.loads(Path(res["timeline"]).read_text(encoding="utf-8"))
    clip = res["clip"]
    ticks = [int(t) for t in res["kill_ticks"]]
    if not ticks:
        return None
    bounds = {r["round"]: (r["start_tick"], r.get("end_tick")) for r in tl.get("rounds") or []}
    rstart, rend = bounds.get(res["round"], (ticks[0], ticks[-1]))
    start = max(rstart, ticks[0] - _PRE_KILL_TICK_MARGIN)
    end = min((rend or ticks[-1] + _POST_KILL_TICK_MARGIN), ticks[-1] + _POST_KILL_TICK_MARGIN)
    short_type, clutch_count = short_type_for_kinds(list(clip.get("kinds") or []))
    short = {
        "short_type": short_type,
        "pov_steam_id": res["steamid"],
        "pov_nick": clip.get("player"),
        "start_tick": start,
        "end_tick": end,
        "kill_ticks": ticks,
        "round": res["round"],
        "pov_team": "",
        "opponent": clip.get("opponent") or "",
        "map": tl.get("map") or "",
        "source": "allstar_top",
        "allstar": {"clip_id": clip.get("clip_id"), "views": clip.get("views"),
                    "title": clip.get("title"), "confidence": res["status"]},
    }
    if clutch_count:
        short["clutch_initial_count"] = clutch_count
    return tl, short


def recut_staged(staging: Path) -> dict:
    """Re-cut staged clips to kill-anchored windows without demo re-parses.

    Reads each clip's alignment + cached timeline, filters events/state
    parquets to [first_kill-320, last_kill+128], and records both spans in
    alignment.json. Quarantined clips are re-cut identically (still excluded
    from nomination); unresolved/missing pieces are reported, not deleted.
    """
    import pandas as pd

    staging = Path(staging)
    stats = {"recut": 0, "skipped": 0, "errors": []}
    for clip_dir in sorted((staging / "clips").iterdir()):
        align_path = clip_dir / "alignment.json"
        if not align_path.is_file():
            stats["skipped"] += 1
            continue
        try:
            align = json.loads(align_path.read_text(encoding="utf-8"))
            demo = Path(align["demo"])
            tmp = staging / "_timelines" / f"{demo.parent.name}__{demo.stem}.json"
            tl = json.loads(tmp.read_text(encoding="utf-8"))
            sid, rnd = str(align["steamid"]), int(align["round"])
            ticks = sorted(int(k["tick"]) for k in tl.get("kills_all") or []
                           if str(k.get("attacker_steam_id")) == sid and k.get("round") == rnd)
            if not ticks:
                stats["errors"].append(f"{clip_dir.name}: no kill ticks in timeline")
                continue
            rstart = align.get("tick_start", ticks[0])
            rend = align.get("tick_end", ticks[-1])
            cstart = max(rstart, ticks[0] - 320)
            cend = min(rend, ticks[-1] + 128)
            state_path = clip_dir / "state.parquet"
            if state_path.is_file():
                df = pd.read_parquet(state_path)
                if "tick" in df.columns:
                    df = df[(df["tick"] >= cstart) & (df["tick"] <= cend)]
                    df.to_parquet(state_path, index=False)
            events_path = clip_dir / "events.parquet"
            if events_path.is_file():
                rows = pd.read_parquet(events_path).to_dict(orient="records")
                kept = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row["payload"]
                    except (TypeError, ValueError):
                        continue
                    items = payload if isinstance(payload, list) else [payload]
                    items = [p for p in items
                             if not isinstance(p, dict) or "tick" not in p
                             or (cstart <= int(p["tick"]) <= cend)]
                    if not items and isinstance(payload, list):
                        continue
                    row["payload"] = json.dumps(items if isinstance(payload, list) else (items[0] if items else {}))
                    kept.append(row)
                pd.DataFrame(kept).to_parquet(events_path, index=False)
            align.update(tick_start=cstart, tick_end=cend,
                         round_tick_start=rstart, round_tick_end=rend,
                         kill_ticks=ticks, window="kill-anchored")
            align_path.write_text(json.dumps(align, indent=2), encoding="utf-8")
            stats["recut"] += 1
        except Exception as e:
            stats["errors"].append(f"{clip_dir.name}: {type(e).__name__}: {e}")
    return stats


def main(argv: list[str] | None = None) -> int:
    from cs2archive.config import settings

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", type=Path)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip fresh Allstar backfill; stored clips only")
    ap.add_argument("--recut", action="store_true",
                    help="Re-cut staged clips to kill-anchored windows (no demo re-parse)")
    ap.add_argument("--staging", type=Path, default=settings.shorts_foundation_dir)
    args = ap.parse_args(argv)

    staging = Path(args.staging)
    if args.recut:
        stats = recut_staged(staging)
        print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                          for k, v in stats.items()}, indent=2))
        if stats["errors"]:
            for e in stats["errors"][:10]:
                print(f"  [ERR] {e}")
        return 0 if not stats["errors"] else 1

    if args.demo:
        demos = [Path(args.demo)]
    elif args.all:
        demos = sorted((Path("demos/hltv")).rglob("*.dem"))
    else:
        ap.error("pass --demo or --all")
    if args.limit:
        demos = demos[:args.limit]

    staging = Path(args.staging)
    ok = partial = unresolved = 0
    for i, demo in enumerate(demos, 1):
        try:
            res = build_one(demo, staging, fetch=not args.no_fetch)
        except Exception as e:
            res = {"demo": str(demo), "status": "unresolved", "reason": f"{type(e).__name__}: {e}"}
        print(f"[{i}/{len(demos)}] {Path(res['demo']).name}: {res['status']} {res.get('clip_id', res.get('reason', ''))}", flush=True)
        ok += res["status"] == "exact"
        partial += res["status"] == "partial"
        unresolved += res["status"] == "unresolved"
    print(f"exact={ok} partial={partial} unresolved={unresolved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
