"""Match top-N Allstar shorts: nominate, exact-verify, render. Nothing else.

For one HLTV match: take the top-N Allstar clips by views (the Allstar
section covers the entire match, all maps), locate each moment in its map
demo (steamid + round + kill list), and render it. No heuristic detector,
no view-prediction model, no cross-match ranking, no upload.

Every nomination must match EXACTLY (explicit kill count from the Allstar
title == demo kills for that player+round). Anything else is a hard fail
(exit 2) naming the clip, so the calling agent knows the scan system needs
a fix. Render failures exit 1.

Usage:
    python -m cs2archive.shorts.render_match_top 2398739 [--top 3] [--no-render]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fail(code: str, message: str, **extra) -> int:
    print("[MATCH_TOP_ERROR] " + json.dumps(
        {"error": True, "code": code, "message": message, **extra}), flush=True)
    return 2


def _resolve_match_folder(match_id: str) -> Path | None:
    root = ROOT / "demos" / "hltv"
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.split("-", 1)[0] == match_id:
            return child
    return None


def nominate_match_top(match_id: str, top_n: int, staging: Path) -> tuple[list, list]:
    """Top-N Allstar nominations for a match, exact-verified.

    Returns (nominated, failures). nominated items hold demo/short/timeline
    paths plus clip identity. failures is empty on success; any failure is
    fatal to the caller (hard fail, no partial renders).
    """
    from cs2archive.highlights import build_action_timeline as atl
    from cs2archive.shorts.build_foundation import (
        _explicit_kills, clips_for_match, short_type_for_kinds,
    )
    from cs2archive.shorts.build_short_timeline import (
        _POST_KILL_TICK_MARGIN, _PRE_KILL_TICK_MARGIN,
    )
    from cs2archive.shorts.join_clips_to_demos import demos_for_map, map_slug_from_title

    folder = _resolve_match_folder(match_id)
    if folder is None:
        raise LookupError(f"no demos/hltv folder for match {match_id}")
    clips = clips_for_match(match_id)
    if not clips:
        raise LookupError(f"no stored Allstar clips for match {match_id}")
    clips = sorted(clips, key=lambda c: -float(c.get("views") or 0))[:top_n]
    if len(clips) < top_n:
        raise LookupError(
            f"match {match_id} has only {len(clips)} Allstar clips, need {top_n}")

    staging = Path(staging)
    nominated = []
    failures = []
    for rank, clip in enumerate(clips, 1):
        cid = str(clip.get("clip_id") or "?")
        title = str(clip.get("title") or "")
        sid = str(clip.get("steamid") or "")
        try:
            rnd = int(clip.get("round") or 0)
        except (TypeError, ValueError):
            rnd = 0
        ident = {"rank": rank, "clip_id": cid, "title": title,
                 "player": clip.get("player"), "round": rnd, "views": clip.get("views")}
        slug = map_slug_from_title(title)
        if not slug:
            failures.append({**ident, "reason": "no-map-in-title"})
            continue
        if not sid or rnd <= 0:
            failures.append({**ident, "reason": "no-steamid-or-round"})
            continue
        want = _explicit_kills(title)
        if want is None:
            failures.append({**ident, "reason": "no-explicit-kill-count-in-title"})
            continue
        demos = demos_for_map(folder, slug)
        if not demos:
            failures.append({**ident, "reason": f"no-demo-for-map-{slug}"})
            continue
        # Find the demo whose kills contain this exact moment.
        hit = None
        for demo in demos:
            tmp = staging / "_timelines" / f"{folder.name}__{demo.stem}.json"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tl_path = atl.ensure_action_timeline(demo, output=tmp)
            if not tl_path:
                continue
            tl = json.loads(Path(tl_path).read_text(encoding="utf-8"))
            roster: set[str] = set()
            for members in (tl.get("teams") or {}).values():
                roster.update(str(s) for s in members)
            if sid not in roster:
                continue
            kills = [k for k in tl.get("kills_all") or []
                     if str(k.get("attacker_steam_id")) == sid and k.get("round") == rnd]
            if kills:
                if hit is not None:
                    hit = None
                    failures.append({**ident, "reason": "moment-in-multiple-demos"})
                    break
                hit = (demo, tl, sorted(int(k["tick"]) for k in kills))
        if hit is None:
            if not any(f.get("clip_id") == cid for f in failures):
                failures.append({**ident, "reason": "moment-not-found-in-any-demo"})
            continue
        demo, tl, ticks = hit
        if len(ticks) != want:
            failures.append({**ident, "reason": "kill-count-mismatch",
                             "demo": demo.name, "demo_kills": len(ticks),
                             "title_kills": want, "kill_ticks": ticks})
            continue
        bounds = {r["round"]: (r["start_tick"], r.get("end_tick"))
                  for r in tl.get("rounds") or []}
        if rnd not in bounds or not bounds[rnd][1]:
            failures.append({**ident, "reason": "no-round-bounds", "demo": demo.name})
            continue
        rstart, rend = bounds[rnd]
        start = max(rstart, ticks[0] - _PRE_KILL_TICK_MARGIN)
        end = min((rend or ticks[-1] + _POST_KILL_TICK_MARGIN),
                  ticks[-1] + _POST_KILL_TICK_MARGIN)
        short_type, clutch_count = short_type_for_kinds(list(clip.get("kinds") or []))
        short = {
            "short_type": short_type,
            "pov_steam_id": sid,
            "pov_nick": clip.get("player"),
            "start_tick": start,
            "end_tick": end,
            "kill_ticks": ticks,
            "round": rnd,
            "pov_team": "",
            "opponent": clip.get("opponent") or "",
            "map": tl.get("map") or "",
            "source": "allstar_top",
            "allstar": {"clip_id": cid, "views": clip.get("views"),
                        "title": title, "confidence": "exact"},
        }
        if clutch_count:
            short["clutch_initial_count"] = clutch_count
        nominated.append({"demo": demo, "timeline": tl, "short": short, **ident})
    return nominated, failures


def persist_nominations(nominated: list) -> list[Path]:
    """Write each nomination under its POV folder. Returns timeline paths."""
    from cs2archive._backlog_common import (
        card_nicks_for_demo, load_accounts_by_steam, persist_shorts_grouped,
    )
    from cs2archive.paths import pov_dir, shorts_base
    from cs2archive.shorts.build_short_timeline import _build_short_slug, short_json_payload

    try:
        accounts = load_accounts_by_steam()
    except Exception:
        accounts = {}
    out_paths = []
    for item in nominated:
        demo = Path(item["demo"])
        short = dict(item["short"])
        sid = str(short.get("pov_steam_id") or "")
        nicks = card_nicks_for_demo(demo)
        nick = nicks.get(sid, "")
        if not nick:
            nick = str((accounts.get(sid) or {}).get("nickname") or "").strip()
        if not nick:
            nick = (short.get("pov_nick") or "").strip()
        if not nick:
            raise RuntimeError(f"no nick for POV {sid} ({demo.name})")
        short["pov_nick"] = nick
        timeline = {"demo_path": str(demo), "map": short.get("map"),
                    "shorts": [], "source": "allstar_top"}
        written = persist_shorts_grouped(demo, timeline, [short], {sid: nick})
        if written != 1:
            raise RuntimeError(f"persist wrote {written} timelines for {demo.name}")
        slug = _build_short_slug(short)
        path = shorts_base(pov_dir(demo.stem, nick)) / f"shorts-{slug}" / "short_timeline.json"
        if not path.is_file():
            raise RuntimeError(f"persisted timeline missing: {path}")
        out_paths.append(path)
    return out_paths


def main(argv: list[str] | None = None) -> int:
    from cs2archive.config import settings

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_id", help="HLTV numeric match id, e.g. 2398739")
    ap.add_argument("--top", type=int, default=3, help="How many Allstar clips (default 3)")
    ap.add_argument("--no-render", action="store_true",
                    help="Nominate + persist only, skip CSDM renders")
    ap.add_argument("--staging", type=Path, default=settings.shorts_foundation_dir)
    args = ap.parse_args(argv)

    try:
        nominated, failures = nominate_match_top(args.match_id, args.top, args.staging)
    except (LookupError, RuntimeError) as exc:
        return _fail("NOMINATE_FAILED", str(exc), match_id=args.match_id)
    if failures:
        for item in nominated:
            print(f"  [ok] #{item['rank']} {item['title']} -> {Path(item['demo']).name} "
                  f"R{item['round']} kills={item['short']['kill_ticks']}", flush=True)
        for fail in failures:
            print(f"  [FAIL] #{fail['rank']} {fail['title']}: {fail['reason']}", flush=True)
        return _fail("EXACT_MATCH_FAILED",
                     f"{len(failures)}/{args.top} top clips did not match exactly",
                     match_id=args.match_id, failures=failures)
    try:
        timelines = persist_nominations(nominated)
    except (RuntimeError, OSError) as exc:
        return _fail("PERSIST_FAILED", str(exc), match_id=args.match_id)
    for item, path in zip(nominated, timelines):
        print(f"  [OK] #{item['rank']} {item['title']} ({item['views']} views) "
              f"-> {path}", flush=True)
    if args.no_render:
        return 0
    import subprocess
    rendered = 0
    for path in timelines:
        print(f"=== {path.relative_to(ROOT)} ===", flush=True)
        proc = subprocess.run(
            [sys.executable, "cs2archive/shorts/render_shorts.py", str(path)],
            cwd=str(ROOT))
        if proc.returncode != 0:
            print("[MATCH_TOP_ERROR] " + json.dumps(
                {"error": True, "code": "RENDER_FAILED",
                 "message": f"render_shorts exited {proc.returncode}",
                 "timeline": str(path)}), flush=True)
            return 1
        rendered += 1
    print(f"Done: {rendered}/{len(timelines)} rendered, no upload", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
