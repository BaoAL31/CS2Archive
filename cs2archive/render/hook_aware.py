"""Hook detection + retry for CSDM/HLAE renders.

CS2 demo renders rely on HLAE hooking the CS2 process so it records real
gameplay. Sometimes the hook silently fails and CS2 opens as the *vanilla*
demo viewer instead — no video sequences are ever produced, but the process
runs to completion. Every render that spawns CS2/HLAE must detect this and
retry, otherwise it produces garbage (or nothing) with no error.

This module provides one reusable wrapper, ``run_csdm_hook_aware``, plus the
process-kill helpers it needs. It is used by the POV renderer, the thumbnail
generator, and the util-cam flight renderer.

Long-running Steam-online flake (symptoms, mitigations, unknown):
``docs/bugs/hlae-steam-online-hook.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Image names of every process a CSDM/HLAE render spawns. Killing the whole
# tree (taskkill /t) is essential — csdm launches HLAE which launches ffmpeg,
# and a plain /im kill leaves the children (especially ffmpeg) running.
RENDER_PROCESS_NAMES = (
    "cs2.exe",
    "HLAE.exe",
    "ffmpeg.exe",
    "csdm.exe",
    "csdm.cmd",
)

# Steam often starts a *second* unhooked cs2.exe (stock demo viewer) after HLAE
# injects or after we kill the hooked process. Reap those so they don't sit on
# the desktop. Do not touch steam.exe.
STEAM_RESPAWN_REAP_S = 8.0
# Stage-A inject grace: AfxHookSource2 must show up in some cs2.exe within this
# long after launch. It lands in ~5-10s when healthy; absence means the loader
# failed or CS2 went vanilla -> fail fast instead of watching a dead demo.
# Stage-B: mirv recording starts ffmpeg. AfxHook without a *new* ffmpeg PID is
# still vanilla-ish (demo plays, no record). 45s after first AfxHook is enough
# for HLAE config-file windows; skip-from-demo-start without ffmpeg is a fail.
HOOK_INJECT_GRACE = 60.0
FFMPEG_GRACE = 45.0
# CR-10: how many consecutive UNKNOWN process queries the hook poll tolerates before aborting with a
# reason that says "could not determine", never "no hook". With the 3x retry inside _query_process
# this is up to 15 failed tasklist calls in a row, so a genuinely transient failure cannot trip it.
UNKNOWN_POLL_CAP = 5
# ffmpeg appeared but stalled (no real video). Kill ALL cs2 and retry.
STRAY_CS2_GRACE = 20.0
# Dead-loader fail-fast: healthy inject lands in ~5-10s. If after this long
# there is STILL no AfxHook in any cs2.exe AND no new ffmpeg PID, every cs2
# on the box is a stray/vanilla squatter (Steam respawn, previous-attempt
# leftover) holding the game lock. Kill them all immediately and fail the
# attempt fast instead of staring at a dead demo for the full 60s.

# Error markers that mean "retrying won't help" (fatal, not a hook failure).
# NOTE: "game error" / "hlae error" are deliberately NOT fatal: they alternate
# across identical runs (transient loader/game-side flakes), and our own
# timeout kills also print them into the attempt log. Retries are bounded by
# hook_retries, so a genuinely broken config still terminates.
_FATAL_MARKERS = (
    "steam is not running",
    "raw files not found",
    "unknown demo source",
)

# Min size for a real encoded sequence/clip. A partially-written file under this
# threshold is treated as not-yet-engaged.
_MIN_VIDEO_BYTES = 1_048_576  # 1 MB

# Steam-online HLAE latch: overlay inject / steam://run relaunch drops AfxHookSource2.
CS2_APP_ID = "730"
STEAM_DIR = Path(r"D:\Steam")
CS2_GAME_DIR = STEAM_DIR / "steamapps" / "common" / "Counter-Strike Global Offensive"
CSDM_SETTINGS = Path.home() / ".csdm" / "settings.json"
_OVERLAY_DLLS = (
    "GameOverlayRenderer64.dll",
    "GameOverlayRenderer.dll",
    "SteamOverlayVulkanLayer64.dll",
    "SteamOverlayVulkanLayer.dll",
)
# -nominidumps / -nobreakpad / -nocrashdialog: Steam-online often treats an HLAE
# inject as a crash and steam://-relaunches a vanilla +playdemo. Offline never
# does that handshake. 45s ffmpeg fail-fast is unchanged.
_STEAM_LAUNCH_FLAGS = (
    "-steam",
    "-insecure",
    "-allow_third_party_software",
    "-nominidumps",
    "-nobreakpad",
    "-nocrashdialog",
)
_APPID_BYTES = b"730\n"
_APPID_OK = {b"730", b"730\n", b"730\r\n"}
_STEAM_OVERLAY_ENV = {
    "DISABLE_VK_LAYER_VALVE_steam_overlay_1": "1",
    "SteamAppId": CS2_APP_ID,
    "SteamGameId": CS2_APP_ID,
}


@dataclass(frozen=True)
class ProcessQuery:
    """The result of asking tasklist about an image.

    Three outcomes, kept distinct (CR-10). Folding "could not measure" into "absent" is how a
    failed tasklist made the HLAE poll loop abort a render that was hooking fine, and how a
    stalled hook was reported as "no hook in 20s".

    ``known=False`` means the query failed after the bounded retries; it is NOT evidence of
    absence, and no caller may treat it as such.
    """

    known: bool
    pids: frozenset[int] = frozenset()

    @property
    def present(self) -> bool:
        """True only for a positive observation. Unknown is not present."""
        return self.known and bool(self.pids)

    @property
    def absent(self) -> bool:
        """True only for a positive absence. Unknown is not absent."""
        return self.known and not self.pids


UNKNOWN = ProcessQuery(known=False)


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


def _query_process(image_name: str, *, attempts: int = 3, backoff: float = 0.25) -> ProcessQuery:
    """PIDs for an image, or ``UNKNOWN`` when tasklist cannot be trusted.

    Retried a bounded number of times first: tasklist under render load usually fails
    transiently, and an honest UNKNOWN is worth one retry to avoid. Never returns an empty set
    for a failed query.
    """
    last_error: str | None = None
    for attempt in range(max(1, attempts)):
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:                       # noqa: BLE001 - reported below
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(backoff * (attempt + 1))
            continue
        if r.returncode != 0:
            last_error = f"tasklist exit {r.returncode}"
            time.sleep(backoff * (attempt + 1))
            continue
        pids: set[int] = set()
        needle = image_name.lower()
        for line in (r.stdout or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) < 2 or parts[0].lower() != needle:
                continue
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
        return ProcessQuery(known=True, pids=frozenset(pids))
    print(f"  [WARN] could not query processes for {image_name} "
          f"({last_error}) — process state UNKNOWN, not absent", flush=True)
    return UNKNOWN


def _query_afx(*, attempts: int = 3, backoff: float = 0.25) -> ProcessQuery:
    """CS2 PIDs that have AfxHookSource2.dll loaded (a hooked HLAE cs2)."""
    last_error: str | None = None
    for attempt in range(max(1, attempts)):
        try:
            r = subprocess.run(
                ["tasklist", "/M", "AfxHookSource2.dll", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:                       # noqa: BLE001 - reported below
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(backoff * (attempt + 1))
            continue
        if r.returncode != 0:
            last_error = f"tasklist exit {r.returncode}"
            time.sleep(backoff * (attempt + 1))
            continue
        pids: set[int] = set()
        for line in (r.stdout or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) < 2 or parts[0].lower() != "cs2.exe":
                continue
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
        return ProcessQuery(known=True, pids=frozenset(pids))
    print(f"  [WARN] could not query AfxHookSource2 ({last_error}) — "
          f"hook state UNKNOWN, not unhooked", flush=True)
    return UNKNOWN


def _query_ffmpeg() -> ProcessQuery:
    """ffmpeg PIDs, or UNKNOWN when tasklist cannot be trusted (CR-10)."""
    return _query_process("ffmpeg.exe")


def _clear_all_cs2(*, why: str) -> int:
    """Kill every cs2.exe we can positively see. Returns how many we cleared.

    CR-10: an unknown query clears nothing and says so, instead of printing "cleared 0 cs2.exe" and
    reading as success. NOTE this is still a blanket kill driven by a GLOBAL query — with two renders
    alive it can kill the other render's cs2. That is an ownership problem tracked as CR-18; the fix
    is for each render to reap only its own child pids, which is pipeline-scope.
    """
    q = _query_process("cs2.exe")
    if not q.known:
        print(f"  [WARN] {why}: cs2.exe state UNKNOWN — cleared nothing", flush=True)
        return 0
    for pid in sorted(q.pids):
        if not _taskkill_pid(pid):
            print(f"  [WARN] taskkill failed for cs2.exe pid={pid}", flush=True)
    return len(q.pids)


def process_running(image_name: str) -> bool | None:
    """True/False when known, None when the process state could not be determined."""
    q = _query_process(image_name)
    return bool(q.pids) if q.known else None


def _taskkill_pid(pid: int) -> bool:
    """Kill one pid. Returns True only on a confirmed kill.

    CR-10: the old version swallowed the failure, so a kill that never happened read as success —
    the upstream cause of ``kill_stale_processes`` reporting a false "clean".
    """
    try:
        r = subprocess.run(
            ["taskkill", "/f", "/t", "/pid", str(pid)],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


@dataclass(frozen=True)
class HookPollVerdict:
    """Outcome of classifying one hook poll (CR-10).

    ``fail_reason`` is None while the poll should continue. ``first_sighting`` marks the poll where
    AfxHookSource2 became visible. ``unknown_streak`` carries the consecutive-unknown counter.
    """

    fail_reason: str | None = None
    first_sighting: bool = False
    unknown_streak: int = 0


def classify_hook_poll(
    afx: ProcessQuery,
    *,
    unknown_streak: int,
    hooked_at: float | None,
    elapsed: float,
    inject_grace: float = HOOK_INJECT_GRACE,
    unknown_cap: int = UNKNOWN_POLL_CAP,
) -> HookPollVerdict:
    """Pure decision for one poll of the HLAE hook loop.

    Extracted so the crux of CR-10 is testable: an unmeasurable hook must never be reported as an
    absent hook, and the unknown streak must reset on ANY known reading (a known-absent reading is
    just as conclusive as a known-present one).
    """
    if afx.known:
        unknown_streak = 0
    if afx.present:
        return HookPollVerdict(first_sighting=hooked_at is None, unknown_streak=0)
    if not afx.known:
        streak = unknown_streak + 1
        if streak >= unknown_cap:
            return HookPollVerdict(
                fail_reason=(f"could not determine process state "
                             f"(tasklist failed {streak} polls in a row)"),
                unknown_streak=streak,
            )
        return HookPollVerdict(unknown_streak=streak)
    if hooked_at is None and elapsed >= inject_grace:
        return HookPollVerdict(fail_reason=f"no HLAE hook in {inject_grace:.0f}s")
    return HookPollVerdict()


def missing_ffmpeg_after_hook(
    hooked_at: float | None,
    now: float,
    *,
    new_ffmpeg: bool,
    grace: float = FFMPEG_GRACE,
) -> bool:
    """True when AfxHook was seen but no new ffmpeg PID appeared in ``grace`` s."""
    if hooked_at is None or new_ffmpeg:
        return False
    return (now - hooked_at) >= grace


def kill_unhooked_cs2() -> list[int]:
    """Kill cs2.exe that is *not* HLAE-injected (Steam's vanilla demo viewer).

    HLAE ``-customLoader`` starts one hooked cs2. Steam-online then often
    launches a second cs2.exe with the same ``+playdemo`` line and no
    AfxHookSource2.dll — that is the stock demo player left on screen.
    """
    hooked = _query_afx()
    if not hooked.known:
        # CR-10: cannot tell whether an injected cs2 exists, so killing anything here would be a
        # guess. Refuse, and say so rather than implying there is nothing to kill.
        print("  [HLAE] AfxHookSource2 state UNKNOWN — not killing any cs2.exe", flush=True)
        return []
    if not hooked.pids:
        # Inject is not visible yet. Killing now murders the HLAE cs2 before
        # AfxHookSource2 shows up in tasklist (that was the 10s Game error).
        return []
    killed: list[int] = []
    known = _query_process("cs2.exe")
    if not known.known:
        print("  [HLAE] cs2.exe state UNKNOWN — not killing any cs2.exe", flush=True)
        return []
    for pid in sorted(known.pids):
        if pid in hooked.pids:
            continue
        print(f"  [HLAE] killing vanilla cs2.exe pid={pid} (no AfxHookSource2)", flush=True)
        if _taskkill_pid(pid):
            killed.append(pid)
        else:
            # CR-10: a failed kill used to be indistinguishable from success, which then let the
            # caller's verification pass report a false "clean".
            print(f"  [WARN] taskkill failed for cs2.exe pid={pid}", flush=True)
    return killed


def reap_steam_respawned_cs2(seconds: float = STEAM_RESPAWN_REAP_S) -> int:
    """After a render CS2 is killed, Steam may start a vanilla one. Keep killing it."""
    if seconds <= 0:
        return 0
    started = time.time()
    deadline = started + seconds
    n = 0
    quiet_since = started
    while time.time() < deadline:
        q = _query_process("cs2.exe")
        if not q.known:
            # CR-10: previously an unknown query looked like "no cs2", which ended the reap early
            # while a Steam-respawned viewer was still alive and holding the game lock.
            time.sleep(0.4)
            continue
        pids = q.pids
        if pids:
            for pid in pids:
                _taskkill_pid(pid)
                n += 1
            print(f"  [HLAE] reaped Steam-respawned cs2.exe {sorted(pids)}", flush=True)
            quiet_since = time.time()
        elif n == 0 and time.time() - started >= 1.5:
            break
        elif n > 0 and time.time() - quiet_since >= 1.0:
            break
        time.sleep(0.4)
    return n


def _steam_appid_dirs(game_dir: Path) -> list[Path]:
    return [
        game_dir / "game" / "bin" / "win64",
        game_dir / "game" / "csgo",
        game_dir / "game",
    ]


def ensure_steam_appid(game_dir: Path = CS2_GAME_DIR) -> list[Path]:
    """Write steam_appid.txt (730) next to cs2.exe so SteamAPI will not relaunch.

    Compared as raw bytes: a trailing NUL (CS2/SteamAPI C-string write) is not
    ``730`` and must be rewritten. ``read_text`` can hide that NUL.
    """
    written: list[Path] = []
    for folder in _steam_appid_dirs(game_dir):
        if not folder.is_dir():
            continue
        path = folder / "steam_appid.txt"
        try:
            current = path.read_bytes() if path.is_file() else b""
        except OSError:
            current = b""
        if current in _APPID_OK:
            continue
        try:
            path.write_bytes(_APPID_BYTES)
        except OSError as e:
            print(f"  [WARN] steam_appid.txt {path}: {e}", flush=True)
            continue
        written.append(path)
    return written


def _overlay_search_dirs(steam_dir: Path) -> list[Path]:
    return [steam_dir, steam_dir / "bin"]


def _live_overlay_dlls(steam_dir: Path) -> list[str]:
    live: list[str] = []
    for folder in _overlay_search_dirs(steam_dir):
        for name in _OVERLAY_DLLS:
            if (folder / name).is_file():
                live.append(name)
    return live


def block_steam_overlay(steam_dir: Path = STEAM_DIR) -> list[Path]:
    """Rename Steam overlay DLLs so they cannot inject into cs2.exe."""
    blocked: list[Path] = []
    if sys.platform != "win32" or not steam_dir.is_dir():
        return blocked
    for folder in _overlay_search_dirs(steam_dir):
        if not folder.is_dir():
            continue
        for name in _OVERLAY_DLLS:
            src = folder / name
            dst = folder / (name + ".blocked")
            if not src.is_file():
                continue
            try:
                if dst.is_file():
                    dst.unlink()
                src.replace(dst)
            except OSError as e:
                print(f"  [WARN] could not block {src}: {e}", flush=True)
                continue
            blocked.append(dst)
    return blocked


def ensure_csdm_steam_launch(settings_path: Path = CSDM_SETTINGS) -> bool:
    """Keep HLAE CS2 flags on CSDM playback.launchParameters (appended to -cmdLine)."""
    if not settings_path.is_file():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    playback = data.setdefault("playback", {})
    existing = str(playback.get("launchParameters") or "")
    tokens = existing.split()
    changed = False
    for flag in _STEAM_LAUNCH_FLAGS:
        if flag not in tokens:
            tokens.append(flag)
            changed = True
    if "+sv_lan" not in tokens:
        tokens.extend(["+sv_lan", "1"])
        changed = True
    if not changed:
        return False
    playback["launchParameters"] = " ".join(tokens)
    try:
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"  [WARN] CSDM settings.json: {e}", flush=True)
        return False
    return True


def _csdm_launch_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(_STEAM_OVERLAY_ENV)
    return env


def _warn_rtss() -> None:
    states = {name: process_running(name) for name in ("RTSS.exe", "MSIAfterburner.exe")}
    running = [n for n, s in states.items() if s]
    if running:
        print(
            "  [WARN] RTSS/Afterburner is running — RTSSHooks64.dll hooks DXGI "
            "Present and fights HLAE (listed in trustedlaunch.cfg)",
            flush=True,
        )
    elif any(s is None for s in states.values()):
        # CR-10: a failed tasklist used to drop this warning silently, so the user was told nothing
        # and a black capture had no hint. Say we could not check.
        print(
            "  [WARN] could not check for RTSS/Afterburner — if capture is black or silent, "
            "close them manually",
            flush=True,
        )


def prepare_steam_hlae(
    *,
    game_dir: Path = CS2_GAME_DIR,
    steam_dir: Path = STEAM_DIR,
    settings_path: Path = CSDM_SETTINGS,
) -> None:
    """Stop Steam-online overlay/relaunch from dropping the HLAE hook."""
    appids = ensure_steam_appid(game_dir)
    overlay = block_steam_overlay(steam_dir)
    launch = ensure_csdm_steam_launch(settings_path)
    still_live = _live_overlay_dlls(steam_dir) if steam_dir.is_dir() else []
    bits = []
    if appids:
        bits.append(f"steam_appid.txt x{len(appids)}")
    if overlay:
        bits.append("overlay DLLs blocked")
    if launch:
        bits.append("CSDM -steam -insecure -nominidumps +sv_lan 1")
    if still_live:
        bits.append(f"STILL LIVE {', '.join(still_live)}")
        print(
            f"  [WARN] Steam overlay DLL still present (injects when Steam is online): "
            f"{', '.join(still_live)}",
            flush=True,
        )
    if not bits:
        bits.append("ok")
    print(f"  [HLAE] Steam preflight: {', '.join(bits)}", flush=True)
    _warn_rtss()


def kill_stale_processes() -> None:
    """Kill every CS2/HLAE/ffmpeg/csdm process left over from any render.

    Kills the whole process tree for each render binary, then polls tasklist
    and re-kills any survivors until they are gone (or a few retries elapse).
    ffmpeg and HLAE often linger after a crashed batch, so they are explicitly
    included and verified.
    """
    for name in RENDER_PROCESS_NAMES:
        q = _query_process(name)
        if not q.known:
            # CR-10 narrow guard: never blanket-kill an image we could not observe. This does NOT
            # fix concurrent-render collisions (that needs pid ownership, CR-18) — it only stops a
            # blind kill on an unknown reading.
            print(f"  [HLAE] not killing {name}: state UNKNOWN", flush=True)
            continue
        if q.pids:
            print(f"  [HLAE] killing {name} {sorted(q.pids)}", flush=True)
            _taskkill_tree(name)

    unverified: set[str] = set()
    for _ in range(6):
        states = {n: _query_process(n) for n in RENDER_PROCESS_NAMES}
        survivors = [n for n, q in states.items() if q.present]
        unverified = {n for n, q in states.items() if not q.known}
        if not survivors:
            break
        time.sleep(0.5)
        for n in survivors:
            _taskkill_tree(n)

    final = {n: _query_process(n) for n in RENDER_PROCESS_NAMES}
    remaining = [n for n, q in final.items() if q.present]
    unverified = {n for n, q in final.items() if not q.known}
    if remaining:
        print(f"  [WARN] could not kill: {', '.join(remaining)}")
    if unverified:
        # CR-10: this branch is the whole point — a failed query must never be reported as "clean".
        print(f"  [WARN] could not VERIFY cleanup for: {', '.join(sorted(unverified))} "
              f"(process state unknown, not proven absent)", flush=True)
    # Steam restarts unhooked CS2 after the hooked process dies. Reap it so
    # the vanilla demo viewer is not left on the desktop.
    reap_steam_respawned_cs2()


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


def _await_no_cs2(timeout_s: float = 60.0) -> bool:
    """True once no cs2.exe remains (polls tasklist, re-killing survivors).

    A lingering corpse that still holds AfxHookSource2 poisons the next
    launch deterministically: the hook check sees the corpse's DLL, the
    per-poll vanilla-killer murders the fresh game before its own hook
    lands, no ffmpeg ever appears, and every retry burns into the same wall.
    Waiting out a slow death (or aborting loudly on an immortal one) is the
    only correct pre-launch state.
    """
    deadline = time.time() + max(1.0, timeout_s)
    while True:
        q = _query_process("cs2.exe")
        if not q.known:
            # CR-10: this function's own docstring describes how a lingering corpse poisons the
            # next launch deterministically. Treating an unknown query as "dead" walked straight
            # into that. Unknown means keep waiting, and abort loudly at the deadline.
            if time.time() >= deadline:
                print("  [WARN] could not confirm cs2.exe death (process state unknown) — "
                      "refusing to launch", flush=True)
                return False
            time.sleep(2.0)
            continue
        if not q.pids:
            return True
        if time.time() >= deadline:
            print(f"  [WARN] {len(q.pids)} cs2.exe survivor(s) refused to die: "
                  f"{sorted(q.pids)}", flush=True)
            return False
        for pid in sorted(q.pids):
            if not _taskkill_pid(pid):
                print(f"  [WARN] taskkill failed for cs2.exe pid={pid}", flush=True)
        time.sleep(2.0)


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
    print(f"  [{label}] pre-launch cleanup of stale render processes...", flush=True)
    kill_stale_processes()
    if not _await_no_cs2():
        print(f"  [ERROR] [{label}] lingering cs2.exe survived taskkill — "
              f"launching into it guarantees the vanilla-viewer doom loop "
              f"(hook attaches to the corpse, fresh game gets murdered, no "
              f"ffmpeg ever). Aborting instead of burning attempts: reboot, "
              f"bounce Steam, or close the game if playing.", flush=True)
        return None
    for attempt in range(1, hook_retries + 2):
        suffix = f" (attempt {attempt}/{hook_retries + 1})" if hook_retries else ""
        print(f"  [{label}]{suffix}...", end=" ", flush=True)
        t0 = time.time()
        if attempt > 1:
            kill_stale_processes()
            if not _await_no_cs2():
                print(f"  [ERROR] [{label}] cs2.exe will not die between "
                      f"attempts — aborting (see above).", flush=True)
                return None
        prepare_steam_hlae()

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
        # CR-10: keep the whole query, not just the set. If the baseline itself is unknown, the
        # new-ffmpeg comparison below must not run at all rather than compare against an empty set.
        ffmpeg_baseline = _query_ffmpeg()
        unknown_polls = 0

        log_path = output_dir / f".csdm_hook_attempt_{attempt}.log"
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, text=True,
                    env=_csdm_launch_env(),
                )

                engaged = False
                fail_reason: str | None = None
                hooked_at: float | None = None
                ffmpeg_seen = False
                # CR-10: recomputed EVERY iteration inside the loop. It used to be set once and never
                # cleared, so a single transient unknown suppressed the "no ffmpeg" verdicts for the
                # rest of the attempt (council review).
                ffmpeg_delta_unknown = False
                poll_start = time.time()
                while time.time() - poll_start < hook_timeout:
                    if new_video_appeared(output_dir, before, min_video_bytes):
                        engaged = True
                        break
                    if proc.poll() is not None:
                        if new_video_appeared(output_dir, before, min_video_bytes):
                            engaged = True
                        break
                    afx = _query_afx()
                    verdict = classify_hook_poll(
                        afx, unknown_streak=unknown_polls, hooked_at=hooked_at,
                        elapsed=time.time() - poll_start,
                    )
                    unknown_polls = verdict.unknown_streak
                    if verdict.first_sighting:
                        hooked_at = time.time()
                        print(
                            f"hooked (AfxHookSource2 in {hooked_at - poll_start:.0f}s)",
                            flush=True,
                        )
                    elif not afx.known and unknown_polls == 1:
                        # CR-10, the crux: an unmeasurable hook is NOT an absent hook, and the abort
                        # reason below is deliberately distinct so the log never claims "no HLAE
                        # hook" when we simply could not look.
                        print("process query UNKNOWN (tasklist unreliable) — not treating "
                              "this as 'no hook'", flush=True)
                    if verdict.fail_reason is not None:
                        fail_reason = verdict.fail_reason
                        break
                    # Record when we first spotted a ffmpeg PID so we
                    # can tell "ffmpeg launched but stalled" from "no
                    # ffmpeg at all" (the latter = stray cs2 holding the
                    # game lock).
                    # CR-10: only a KNOWN set difference counts as evidence. An unknown query used
                    # to yield an empty difference, i.e. "no new ffmpeg", which fed
                    # missing_ffmpeg_after_hook() and reported a working hook as failed. When either
                    # side is unknown we skip the delta for this poll and do not touch ffmpeg_seen.
                    now_ffmpeg = _query_ffmpeg()
                    if ffmpeg_baseline.known and now_ffmpeg.known:
                        new_ffmpeg = bool(now_ffmpeg.pids - ffmpeg_baseline.pids)
                        ffmpeg_delta_unknown = False
                    else:
                        new_ffmpeg = False
                        ffmpeg_delta_unknown = True
                    if new_ffmpeg and not ffmpeg_seen:
                        ffmpeg_seen = True
                        ffmpeg_first_seen = time.time()
                        print(
                            f"ffmpeg pid in {time.time() - (hooked_at or poll_start):.0f}s",
                            flush=True,
                        )
                    # ffmpeg appeared but no real video after its grace.
                    if (ffmpeg_seen and not engaged and ffmpeg_first_seen is not None
                            and time.time() - ffmpeg_first_seen >= FFMPEG_GRACE):
                        # ffmpeg PID is dead/stalled: HLAE couldn't write.
                        # Kill ALL cs2 — a stale vanilla demo viewer is
                        # holding the game lock and blocking the record.
                        cleared = _clear_all_cs2(why="ffmpeg stalled")
                        print(
                            f"ffmpeg stalled: cleared {cleared} cs2.exe, retrying",
                            flush=True,
                        )
                        fail_reason = "ffmpeg stalled (stray cs2 cleared)"
                        break
                    # HLAE hooked (AfxHookSource2 visible) but ffmpeg
                    # never materialised after its grace. Stray vanilla
                    # cs2 is holding the game lock -> clear ALL cs2.
                    if (hooked_at is not None and not new_ffmpeg and not ffmpeg_delta_unknown
                            and time.time() - hooked_at >= FFMPEG_GRACE):
                        cleared = _clear_all_cs2(why="hooked but no ffmpeg")
                        print(
                            f"hooked but no ffmpeg in {FFMPEG_GRACE:.0f}s - "
                            f"cleared {cleared} cs2.exe, retrying",
                            flush=True,
                        )
                        fail_reason = "hooked but no ffmpeg (stray cs2 cleared)"
                        break
                    if not ffmpeg_delta_unknown and missing_ffmpeg_after_hook(
                        hooked_at, time.time(), new_ffmpeg=new_ffmpeg,
                    ):
                        fail_reason = f"no ffmpeg in {FFMPEG_GRACE:.0f}s"
                        break
                    kill_unhooked_cs2()
                    if (hooked_at is None and not new_ffmpeg and not ffmpeg_delta_unknown
                            and time.time() - poll_start >= STRAY_CS2_GRACE):
                        cleared = _clear_all_cs2(why="no hook/ffmpeg")
                        print(
                            f"no hook/ffmpeg in {STRAY_CS2_GRACE:.0f}s - "
                            f"cleared {cleared} stray cs2.exe, retrying",
                            flush=True,
                        )
                        fail_reason = (
                            f"no hook/ffmpeg in {STRAY_CS2_GRACE:.0f}s "
                            "(stray cs2 cleared)"
                        )
                        break
                    time.sleep(1)

                fatal = False
                if not engaged:
                    if fail_reason is not None or proc.poll() is None:
                        if fail_reason is None:
                            fail_reason = f"no video in {hook_timeout:.0f}s"
                        print(f"HOOK-FAIL ({fail_reason}) - killing and retrying")
                        kill_stale_processes()
                        # CR-09: a failed kill used to be silent, and the surviving process then
                        # held the game lock into the next attempt — the same shape as CR-10's
                        # false "clean". Report it instead of assuming the process died.
                        try:
                            if proc.poll() is None:
                                proc.kill()
                        except Exception as exc:
                            print(f"  [WARN] could not kill the CSDM process "
                                  f"({type(exc).__name__}: {exc}) — it may still hold the game "
                                  f"lock", flush=True)
                        try:
                            proc.wait(timeout=30)
                        except Exception:
                            still = proc.poll()
                            if still is None:
                                print("  [WARN] CSDM process did not exit within 30s", flush=True)
                        continue
                    log_tail = ""
                    try:
                        log_tail = log_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                    last_err = log_tail
                    if any(m in log_tail.lower() for m in _FATAL_MARKERS):
                        fatal = True
                    else:
                        print("HOOK-FAIL (exited early, no video) - killing and retrying")
                        try:
                            proc.wait(timeout=14400)
                        except Exception:
                            # CR-09: swallowing this timeout made the caller believe the process had
                            # exited when it may still be alive.
                            if proc.poll() is None:
                                print("  [WARN] CSDM process still running after the wait — "
                                      "proceeding (kill_stale_processes will clear it)", flush=True)
                        kill_stale_processes()
                        continue

                if fatal:
                    try:
                        proc.wait(timeout=14400)
                    except Exception:
                        if proc.poll() is None:
                            print("  [WARN] CSDM process still running after the fatal wait",
                                  flush=True)
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
            reap_steam_respawned_cs2()
            return newest

    print(f"[ERROR] CS2 failed to hook after {hook_retries + 1} attempt(s) "
          f"(no video produced in {hook_timeout:.0f}s).")
    if last_err:
        print(last_err[-800:])
    kill_stale_processes()
    return None
