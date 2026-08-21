"""Render pending shorts — queue-aware, won't intercept active CSDM/CS2 sessions.

Finds every shorts-*/short_timeline.json under renders/ that still needs
rendering (final 1080x1920 mp4 missing or <1MB or wrong res), then renders
them one-by-one, WAITING until no other CSDM/HLAE/CS2 render is active.

This is the shorts equivalent of pipeline_chain / upload_pending: run it
whenever, it will drain the queue without stealing CS2 from a running POV
overlay concat, highlight reel, or another shorts batch. Only one CSDM
instance ever runs.

Usage:
    python scripts/shorts/render_pending_shorts.py              # render all pending, auto CPU/GPU
    python scripts/shorts/render_pending_shorts.py --dry-run    # list pending only
    python scripts/shorts/render_pending_shorts.py --once       # render one pending then exit
    python scripts/shorts/render_pending_shorts.py --cpu        # force CPU
    python scripts/shorts/render_pending_shorts.py --gpu        # force GPU
    python scripts/shorts/render_pending_shorts.py --loop       # daemon: poll forever

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
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

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
    st = short["short_type"]
    nick = short.get("pov_nick","unknown")
    tick = short.get("start_tick",0)
    if st=="4k":
        base = f"{len(short.get('kill_ticks',[]))}k_multikill-{nick}-t{tick}"
    elif st=="clutch":
        cnt = short.get("clutch_initial_count","XvX")
        base = f"{cnt}_clutch-{nick}-t{tick}"
    else:
        base = f"{st}-{nick}-t{tick}"
    tags = short.get("punch_up_tags") or []
    if tags: base = f"{base}_{'_'.join(tags)}"
    return out_dir / f"{base}.mp4"


def _is_pending(tl_path: Path) -> bool:
    try:
        tl = json.loads(tl_path.read_text(encoding="utf-8"))
    except: return False
    shorts = tl.get("shorts",[])
    if not shorts: return False
    out_dir = tl_path.parent
    for s in shorts:
        dst = _short_output_path(out_dir, s)
        if not dst.exists() or dst.stat().st_size < MIN_BYTES:
            return True
        w,h = _probe_res(dst)
        if (w,h) != (OUT_W, OUT_H):
            return True
    return False


def find_pending() -> list[Path]:
    renders = _PROJECT_ROOT / "renders"
    tls = sorted(renders.rglob("short_timeline.json"))
    return [p for p in tls if _is_pending(p)]


def render_one(tl: Path, extra_args: list[str]) -> int:
    print(f"\n=== {tl.relative_to(_PROJECT_ROOT)} ===", flush=True)
    cmd = [sys.executable, "scripts/shorts/render_shorts.py", str(tl), *extra_args]
    print(f"  cmd: {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Render pending shorts (queue-aware, won't steal CSDM)")
    ap.add_argument("--dry-run", action="store_true", help="List pending timelines and exit")
    ap.add_argument("--once", action="store_true", help="Render one pending short then exit")
    ap.add_argument("--loop", action="store_true", help="Poll forever: render pending, wait, repeat")
    ap.add_argument("--poll-secs", type=int, default=30, help="Seconds between blocking checks (default 30)")
    ap.add_argument("--cpu", action="store_true", help="Force CPU (libx264)")
    ap.add_argument("--gpu", action="store_true", help="Force GPU (h264_nvenc)")
    ap.add_argument("--no-auto", action="store_true", help="Disable auto GPU-busy detection in render_shorts")
    ap.add_argument("--batches", type=int, default=0, help="Pass --batches to render_shorts")
    ap.add_argument("--composite-only", action="store_true", help="Pass --composite-only to render_shorts")
    args, unknown = ap.parse_known_args()

    # extra args forwarded to render_shorts
    forward = []
    if args.cpu: forward.append("--cpu")
    if args.gpu: forward.append("--gpu")
    if args.no_auto: forward.append("--no-auto")
    if args.batches: forward.extend(["--batches", str(args.batches)])
    if args.composite_only: forward.append("--composite-only")
    forward.extend(unknown)

    def do_pass() -> int:
        pending = find_pending()
        if not pending:
            print("No pending shorts.", flush=True)
            return 0
        print(f"Pending: {len(pending)} timeline(s)", flush=True)
        for p in pending:
            print(f"  - {p.relative_to(_PROJECT_ROOT)}", flush=True)
        if args.dry_run:
            return 0
        rendered = 0
        for tl in pending:
            # wait for any blocking CS2/HLAE/CSDM to clear (ffmpeg parallel safe)
            while True:
                blocking = _any_blocking()
                if not blocking:
                    break
                print(f"  [wait] CSDM busy ({', '.join(blocking)}) — waiting {args.poll_secs}s...", flush=True)
                time.sleep(args.poll_secs)
            rc = render_one(tl, forward)
            if rc != 0:
                print(f"  [FAIL] {tl} rc={rc} — continuing to next", flush=True)
            rendered += 1
            if args.once:
                break
        print(f"\nDone. Rendered {rendered}/{len(pending)} pending.", flush=True)
        return 0

    if args.loop:
        while True:
            do_pass()
            if not find_pending():
                print(f"[loop] all done — sleeping {args.poll_secs}s...", flush=True)
            time.sleep(args.poll_secs)
    else:
        return do_pass()

if __name__ == "__main__":
    sys.exit(main())
