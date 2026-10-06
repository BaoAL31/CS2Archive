"""Render pending shorts — queue-aware, won't intercept active CSDM/CS2 sessions.

Ranks all pending recognized-pro cuts by predicted typical Allstar views,
then renders at most two successes per Sydney day. The candidate scan is
global, never first-demo-wins. Failed attempts do not consume daily slots.

This is the shorts equivalent of pipeline_chain / upload_pending: run it
whenever, it will drain the queue without stealing CS2 from a running POV
overlay concat, highlight reel, or another shorts batch. Only one CSDM
instance ever runs.

Usage:
    python cs2archive/shorts/render_pending_shorts.py              # globally pick up to two today
    python cs2archive/shorts/render_pending_shorts.py --dry-run    # list pending only
    python cs2archive/shorts/render_pending_shorts.py --once       # render one pending then exit
    python cs2archive/shorts/render_pending_shorts.py --cpu        # force CPU
    python cs2archive/shorts/render_pending_shorts.py --gpu        # force GPU
    python cs2archive/shorts/render_pending_shorts.py --loop       # daemon: poll forever

Polling: before each short, checks BLOCKING_NAMES (cs2.exe, HLAE.exe,
csdm.exe/cmd). If any is running, waits 30s and rechecks. ffmpeg alone
(overlay concat, hl reel) does NOT block — shorts auto-fallback to CPU
libx264 when GPU busy, so ffmpeg//ffmpeg parallel safe.

Resume: render_shorts itself is resume-safe (skips existing 1080x1920 >=1MB
outputs, skips existing segments on --composite-only, etc). So re-running
this script is safe.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from cs2archive.config import settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

FFPROBE = settings.ffprobe_exe  # CR-07: was a hardcoded machine path

RENDER_PROCESS_NAMES = ("cs2.exe", "HLAE.exe", "csdm.exe", "csdm.cmd")
# Only CS2/HLAE/CSDM block — only one CS2 session at a time. ffmpeg
# (overlay concat, hl reel, shorts composite) uses libx264 on CPU when
# GPU busy (render_shorts auto fallback), so ffmpeg//ffmpeg parallel
# is safe — no NVENC contention. Don't block on ffmpeg.exe.
BLOCKING_NAMES = ("cs2.exe", "HLAE.exe", "csdm.exe", "csdm.cmd")

OUT_W, OUT_H = 1080, 1920
MIN_BYTES = 1_048_576


def _proc_running(name: str) -> bool:
    try:
        r = subprocess.run(["tasklist", "/fi", f"IMAGENAME eq {name}"], capture_output=True, text=True, timeout=10)
        out = (r.stdout or "").lower()
        return name.lower() in out and "no tasks" not in out
    except Exception:
        return False


def _any_blocking() -> list[str]:
    return [n for n in BLOCKING_NAMES if _proc_running(n)]


def _probe_res(path: Path) -> tuple[int,int]:
    try:
        r = subprocess.run([FFPROBE, "-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0", str(path)], capture_output=True, text=True, timeout=10)
        if r.returncode!=0: return (0,0)
        a,b = r.stdout.strip().split(",")
        return (int(a),int(b))
    except: return (0,0)


def _short_output_path(out_dir: Path, short: dict) -> Path:
    from cs2archive.shorts.output_paths import short_output_path
    return short_output_path(out_dir, short)


def _complete(path: Path) -> bool:
    return (path.is_file() and path.stat().st_size >= MIN_BYTES
            and _probe_res(path) == (OUT_W, OUT_H))


def render_one(tl: Path, extra_args: list[str]) -> int:
    print(f"\n=== {tl.relative_to(_PROJECT_ROOT)} ===", flush=True)
    cmd = [sys.executable, "cs2archive/shorts/render_shorts.py", str(tl), *extra_args]
    print(f"  cmd: {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return r.returncode


def main() -> int:
    from cs2archive.shorts.shorts_picker import LEDGER
    ap = argparse.ArgumentParser(description="Render pending shorts (queue-aware, won't steal CSDM)")
    ap.add_argument("--dry-run", action="store_true", help="List pending timelines and exit")
    ap.add_argument("--once", action="store_true", help="Render one pending short then exit")
    ap.add_argument("--limit", type=int, choices=(1, 2), default=2, help="At most N successes this pass; daily cap is always two")
    ap.add_argument("--max-age-days", type=int, default=7, help="Expire unselected candidates after N days in the pool")
    ap.add_argument("--ledger", type=Path, default=LEDGER, help="Versioned render-selection state (old extraction ledger is unused)")
    ap.add_argument("--loop", action="store_true", help="Poll forever: render pending, wait, repeat")
    ap.add_argument("--poll-secs", type=int, default=30, help="Seconds between blocking checks (default 30)")
    ap.add_argument("--cpu", action="store_true", help="Force CPU (libx264)")
    ap.add_argument("--gpu", action="store_true", help="Force GPU (h264_nvenc)")
    ap.add_argument("--no-auto", action="store_true", help="Disable auto GPU-busy detection in render_shorts")
    ap.add_argument("--batches", type=int, default=0, help="Pass --batches to render_shorts")
    ap.add_argument("--composite-only", action="store_true", help="Pass --composite-only to render_shorts")
    args, unknown = ap.parse_known_args()
    if args.max_age_days < 1:
        ap.error("max-age-days must be positive")
    if any(token.split("=", 1)[0] in {"--output", "-o", "--name"} for token in unknown):
        ap.error("output/name overrides are incompatible with picker accounting; use render_shorts directly")

    # extra args forwarded to render_shorts
    forward = []
    if args.cpu: forward.append("--cpu")
    if args.gpu: forward.append("--gpu")
    if args.no_auto: forward.append("--no-auto")
    if args.batches: forward.extend(["--batches", str(args.batches)])
    if args.composite_only: forward.append("--composite-only")
    forward.extend(unknown)

    def do_pass() -> int:
        from cs2archive.shorts.demand_gate import load_partial_stars
        from cs2archive.shorts.fit_partial_stars import _recognised_steamids
        from cs2archive.shorts.scrape_allstar_hltv import load_ratings_stages
        from cs2archive.shorts.shorts_picker import (
            PickerState, picker_lock, render_timeline, scan_candidates, sydney_day,
        )
        from cs2archive.shorts.view_prediction import validate_model

        try:
            model = load_partial_stars()
            validate_model(model)
            # Atomic ledger/model snapshots suffice for an advisory dry-run;
            # only real selection takes the exclusive lock and creates files.
            with (nullcontext() if args.dry_run else picker_lock(args.ledger)):
                now = datetime.now(timezone.utc)
                state = PickerState(args.ledger)
                state.reconcile(_complete, now=now, persist=not args.dry_run)
                candidates, stats = scan_candidates(_PROJECT_ROOT / "renders", model,
                                                    _recognised_steamids(), complete=_complete,
                                                    stages=load_ratings_stages())
                from cs2archive.shorts.allstar_selector import rank_candidates_by_allstar
                ranked, sel_report = rank_candidates_by_allstar(candidates)
                candidates = [c for c, _clip in ranked]
                clip_by_key = {c.key: clip for (c, clip) in ranked}
                stats["allstar_selector"] = sel_report
                picked, selection = state.select(candidates, now=now,
                                                 limit=1 if args.once else args.limit,
                                                 max_age_days=args.max_age_days)
                print(f"[picker] Sydney {sydney_day(now)}: scan={stats}, selection={selection}", flush=True)
                picked_ids = {c.key for c in picked}
                for c in candidates:
                    entry = state.entries.get(c.key, {})
                    status = "PICK" if c.key in picked_ids else entry.get("state", "pending")
                    clip = clip_by_key.get(c.key)
                    views = clip.get("views") if clip else None
                    print(f"  [{status}] allstar views={views if views is not None else '?'} "
                          f"{c.short.get('pov_nick')} {c.short.get('short_type')} "
                          f"{c.timeline}", flush=True)
                if args.dry_run:
                    return 0
                state.save()
                rendered = failed = 0
                for c in picked:
                    while _any_blocking():
                        print(f"  [wait] CSDM busy; retry in {args.poll_secs}s", flush=True)
                        time.sleep(args.poll_secs)
                    now = datetime.now(timezone.utc)
                    if state.completed_today(now) >= 2:
                        break
                    state.reserve(c, now=now, model=model)
                    try:
                        rc = render_one(render_timeline(c), forward)
                        if rc != 0 or not _complete(c.video):
                            raise RuntimeError(f"render rc={rc}; final video missing/invalid")
                    except (OSError, RuntimeError) as exc:
                        state.fail(c.key, str(exc))
                        failed += 1
                        print(f"  [FAIL] {c.timeline}: {exc}", flush=True)
                        continue
                    state.finish(c.key, now=datetime.now(timezone.utc))
                    rendered += 1
                print(f"Done: {rendered} successes, {failed} failures (failures do not consume slots)", flush=True)
                return 1 if failed else 0
        except (ValueError, KeyError) as exc:
            print(f"[PICKER_ERROR] {exc}", flush=True)
            return 2  # Invalid model/ledger is fatal, not a transient retry.
        except OSError as exc:
            print(f"[PICKER_ERROR] {exc}", flush=True)
            return 1

    if args.loop:
        while True:
            status = do_pass()
            if args.dry_run:
                return status
            if status == 2:
                return status
            time.sleep(args.poll_secs)
    else:
        return do_pass()

if __name__ == "__main__":
    sys.exit(main())
