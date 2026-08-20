"""Hook detection + retry for CSDM/HLAE renders.

CS2 demo renders rely on HLAE hooking the CS2 process so it records real
gameplay. Sometimes the hook silently fails and CS2 opens as the *vanilla*
demo viewer instead — no video sequences are ever produced, but the process
runs to completion. Every render that spawns CS2/HLAE must detect this and
retry, otherwise it produces garbage (or nothing) with no error.

This module provides one reusable wrapper, ``run_csdm_hook_aware``, plus the
process-kill helpers it needs. It is used by the POV renderer, the thumbnail
generator, and the util-cam flight renderer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

# Image names of every process a CSDM/HLAE render spawns. Killing the whole
# tree (taskkill /t) is essential — csdm launches HLAE which launches ffmpeg,
# and a plain /im kill leaves the children (especially ffmpeg) running.
RENDER_PROCESS_NAMES = ("cs2.exe", "HLAE.exe", "ffmpeg.exe", "csdm.exe", "csdm.cmd")

# Error markers that mean "retrying won't help" (fatal, not a hook failure).
_FATAL_MARKERS = (
    "steam is not running",
    "raw files not found",
    "unknown demo source",
    "game error",
)

# Min size for a real encoded sequence/clip. A partially-written file under this
# threshold is treated as not-yet-engaged.
_MIN_VIDEO_BYTES = 1_048_576  # 1 MB


def _taskkill_tree(image_name: str) -> bool:
    """Force-kill a process image and its entire tree. Returns True if it ran."""
    try:
        r = subprocess.run(
            ["taskkill", "/f", "/t", "/im", image_name],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _process_running(image_name: str) -> bool:
    """True if any process with this image name is still alive."""
    try:
        r = subprocess.run(
            ["tasklist", "/fi", f"IMAGENAME eq {image_name}"],
            capture_output=True, text=True, timeout=15,
        )
        out = r.stdout or ""
        return image_name.lower() in out.lower() and "no tasks" not in out.lower()
    except Exception:
        return True  # assume alive on failure so we retry the kill


def kill_stale_processes() -> None:
    """Kill every CS2/HLAE/ffmpeg/csdm process left over from any render.

    Kills the whole process tree for each render binary, then polls tasklist
    and re-kills any survivors until they are gone (or a few retries elapse).
    ffmpeg and HLAE often linger after a crashed batch, so they are explicitly
    included and verified.
    """
    for name in RENDER_PROCESS_NAMES:
        _taskkill_tree(name)

    for _ in range(6):
        survivors = [n for n in RENDER_PROCESS_NAMES if _process_running(n)]
        if not survivors:
            break
        time.sleep(0.5)
        for n in survivors:
            _taskkill_tree(n)

    remaining = [n for n in RENDER_PROCESS_NAMES if _process_running(n)]
    if remaining:
        print(f"  [WARN] could not kill: {', '.join(remaining)}")
    time.sleep(1)


def list_videos(output_dir: Path, min_video_bytes: int = _MIN_VIDEO_BYTES) -> set[str]:
    """Identifiers of all complete (>= ``min_video_bytes``) .mp4 videos under output_dir.

    Handles every CSDM output layout:
      - New (3.20+):  N-sequence/video.mp4 directories under output_dir.
      - Old:          sequence-{i}-tick-{A}-to-{B}.mp4 in output_dir root.
      - Single clips: clip_name.mp4 in output_dir (thumbnail, flight clips).

    Returns a set of relative-path identifiers for videos >= ``min_video_bytes``,
    so a newly appearing (growing) file is seen as a change vs a captured
    ``before`` set.
    """
    found: set[str] = set()
    for video in output_dir.rglob("*.mp4"):
        if video.is_file() and video.stat().st_size >= min_video_bytes:
            found.add(str(video.relative_to(output_dir)))
    return found


def new_video_appeared(output_dir: Path, before: set[str], min_video_bytes: int = _MIN_VIDEO_BYTES) -> bool:
    """True if any new >= ``min_video_bytes`` .mp4 appeared since `before`."""
    return list_videos(output_dir, min_video_bytes) - before != set()


def _purge_partial_sequences(output_dir: Path) -> None:
    """Remove partial sequence outputs from a killed/aborted attempt.

    Deletes N-sequence/video.mp4 dirs (new format) and sequence-*.mp4 (old
    format). These are the raw CSDM outputs that would otherwise be mistaken
    for newly-engaged work on the next retry (same relative path), and would
    mask a genuine hook failure. Already-finalized videos are left untouched.
    """
    for seq_dir in output_dir.glob("*-sequence"):
        if seq_dir.is_dir():
            shutil.rmtree(seq_dir, ignore_errors=True)
    for p in output_dir.glob("sequence-*-tick-*-to-*.mp4"):
        try:
            p.unlink()
        except OSError:
            pass


def run_csdm_hook_aware(
    cmd: list[str],
    label: str,
    output_dir: Path,
    *,
    hook_timeout: float = 120.0,
    hook_retries: int = 2,
    on_attempt_start=None,
    on_attempt_end=None,
    pick_output=None,
    min_video_bytes: int = _MIN_VIDEO_BYTES,
) -> Path | None:
    """Run a CSDM/HLAE command with hook detection + retry.

    Watches ``output_dir`` for a new >= 1 MB .mp4 (a hooked CS2 produces one
    within a round or two; the vanilla-viewer hook failure produces nothing).
    If none appears within ``hook_timeout`` seconds, or the process exits early
    without producing a video, the CS2/HLAE tree is killed and the command
    re-run up to ``hook_retries`` more times.

    ``hook_retries`` counts *extra* attempts after the first, so hook_retries=2
    means up to 3 total launches. Returns the newest produced video Path on
    success, or None after exhausting all retries.

    ``on_attempt_start`` / ``on_attempt_end`` are optional callables invoked
    around each attempt (e.g. to start/stop a camera-inject poll thread that
    must not outlive a killed attempt). on_attempt_end is always called, even
    on a failed/killed attempt.

    ``pick_output`` optionally selects the produced video given (output_dir,
    before_set) and returns the Path to return, or None to mean "not yet". This
    lets callers prefer a specific --output-file-name clip or exclude
    intermediate flight clips. When omitted, the newest >= 1 MB .mp4 wins.
    """
    last_err = ""
    for attempt in range(1, hook_retries + 2):
        suffix = f" (attempt {attempt}/{hook_retries + 1})" if hook_retries else ""
        print(f"  [{label}]{suffix}...", end=" ", flush=True)
        t0 = time.time()

        if on_attempt_start:
            try:
                on_attempt_start()
            except Exception as e:
                print(f"WARN on_attempt_start: {e}", flush=True)

        # Each attempt renders from a clean slate: purge any partial sequences
        # left by a killed/aborted attempt, then re-baseline what counts as
        # "new". Already-finalized clips are preserved.
        _purge_partial_sequences(output_dir)
        before = list_videos(output_dir, min_video_bytes)

        log_path = output_dir / f".csdm_hook_attempt_{attempt}.log"
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, text=True,
                )

                # Poll for the hook to engage (a new video appears) or the
                # process to exit on its own.
                engaged = False
                poll_start = time.time()
                while time.time() - poll_start < hook_timeout:
                    if proc.poll() is not None:
                        break  # csdm exited on its own
                    if new_video_appeared(output_dir, before, min_video_bytes):
                        engaged = True
                        break
                    time.sleep(5)

                fatal = False
                if not engaged:
                    if proc.poll() is not None:
                        # Process exited early without producing a video —
                        # usually a hook failure too (e.g. CS2 opened the
                        # vanilla viewer and exited). Retry, unless fatal.
                        log_tail = ""
                        try:
                            log_tail = log_path.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            pass
                        last_err = log_tail
                        if any(m in log_tail.lower() for m in _FATAL_MARKERS):
                            fatal = True  # report accurate error, don't retry
                        else:
                            print("HOOK-FAIL (exited early, no video) - killing and retrying")
                            proc.wait(timeout=14400)
                            kill_stale_processes()
                            continue
                    else:
                        # Still running but no video -> hook failed (vanilla viewer).
                        print(f"HOOK-FAIL (no video in {hook_timeout:.0f}s) - killing and retrying")
                        kill_stale_processes()
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        proc.wait()
                        continue

                if fatal:
                    proc.wait(timeout=14400)
                    err = ""
                    try:
                        err = log_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                    print(f"FAILED ({time.time() - t0:.0f}s, {err[-300:].strip()})")
                    if on_attempt_end:
                        try:
                            on_attempt_end()
                        except Exception:
                            pass
                    return None

                proc.wait(timeout=14400)
        finally:
            if on_attempt_end:
                try:
                    on_attempt_end()
                except Exception:
                    pass

        # Determine the produced video.
        if pick_output is not None:
            newest = pick_output(output_dir, before)
        else:
            videos = list_videos(output_dir, min_video_bytes) - before
            newest = None
            if videos:
                newest = max(
                    (output_dir / v for v in videos),
                    key=lambda p: p.stat().st_mtime,
                )
        if newest is not None:
            mb = newest.stat().st_size / 1e6
            print(f"OK ({time.time() - t0:.0f}s, {mb:.0f} MB)")
            return newest

    print(f"[ERROR] CS2 failed to hook after {hook_retries + 1} attempt(s) "
          f"(no video produced in {hook_timeout:.0f}s).")
    if last_err:
        print(last_err[-800:])
    return None
