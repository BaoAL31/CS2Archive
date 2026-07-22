"""Render Edit Timeline segments using CSDM batch config-file mode.

Reads edit_timeline.json, builds a CSDM config with segments as sequences,
renders them via `csdm video --config-file`, then concatenates + upscales.

Usage:
    python scripts/highlights/render_edit_timeline.py renders/hl-<stem>/edit_timeline.json

    # Render in batches of 5 segments each
    python scripts/highlights/render_edit_timeline.py renders/hl-<stem>/edit_timeline.json --batches 5

    # Render only batch 2
    python scripts/highlights/render_edit_timeline.py renders/hl-<stem>/edit_timeline.json --batches 5 --batch 2
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

CFG_PATH = (_PROJECT_ROOT / "assets" / "cs2_pov.cfg").resolve()

# Same encoding settings as render_pov.py / concat_rounds.py
TARGET_WIDTH = 2560
TARGET_HEIGHT = 1440
TARGET_FRAMERATE = 60

# CSDM base flags (for config file, we set these in the JSON)
# We'll use 64 fps for recording (CSDM internal), output will be 60
CSDM_RECORD_FRAMERATE = 64


def _dbg(label: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] [{label}] {msg}", flush=True)


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0.0
    data = json.loads(r.stdout)
    return float(data.get("format", {}).get("duration", 0))


def _is_valid_video(path: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def _encode_scaled(src: Path, dst: Path) -> None:
    """One-pass scale to 2560x1440 using NVENC (VP9 trick)."""
    src_mb = src.stat().st_size / 1024 / 1024
    print(f"  [Scale] {src_mb:.0f} MB -> {TARGET_WIDTH}x{TARGET_HEIGHT} (NVENC CQ14)...", end=" ", flush=True)

    temp = dst.with_suffix(".temp.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:flags=spline,setsar=1,format=nv12",
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "14",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "5.1",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-movflags", "+faststart",
        "-c:a", "copy", str(temp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        temp.unlink(missing_ok=True)
        print(f"\n[ERROR] Scale failed: {r.stderr[-300:]}")
        print("  [Fallback] Retrying with CPU Lanczos + libx264...")
        vf_cpu = f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:flags=lanczos,setsar=1,format=yuv420p"
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-vf", vf_cpu,
            "-c:v", "libx264", "-crf", "15", "-preset", "slow",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "copy", str(temp),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0:
            temp.unlink(missing_ok=True)
            print(f"\n[ERROR] Scale fallback also failed: {r.stderr[-300:]}")
            sys.exit(1)
    temp.replace(dst)
    mb = dst.stat().st_size / 1024 / 1024
    print(f"OK ({mb:.0f} MB)")


def _concat_videos(file_list: list[Path], output: Path) -> Path:
    """Concatenate video files using ffmpeg stream copy (no re-encode)."""
    if len(file_list) == 1:
        shutil.copy2(str(file_list[0]), str(output))
        return output

    print(f"  [Concat] {len(file_list)} segments -> {output.name}")
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "files.txt"
        with open(lst, "w") as f:
            for p in file_list:
                f.write(f"file '{p.resolve().as_posix()}'\n")
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(output)],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            print(f"  [ERROR] Concat failed: {r.stderr[-500:]}")
            sys.exit(1)
    return output


def _sanitize(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', s)


def load_edit_timeline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for k in ("demo_path", "map", "segments"):
        if k not in data:
            raise ValueError(f"edit_timeline.json missing required field: {k}")
    return data


def _get_player_crosshair_cvars(steam_id: str, demo_path: Path) -> list[str]:
    """Extract crosshair share code from demo and decode to cvars."""
    cvars = []
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [CSDM, "json", str(demo_path.resolve()), "--output-folder", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return cvars
        jf = list(Path(tmp).glob("*.json"))
        if not jf:
            return cvars
        data = json.loads(jf[0].read_text(encoding="utf-8"))
        for pl in data.get("players", []):
            if pl.get("steamId") == steam_id:
                code = pl.get("crosshairShareCode")
                if code:
                    from crosshair_code import decode_crosshair, crosshair_to_convars
                    cvars = crosshair_to_convars(decode_crosshair(code))
                break
    return cvars


def _build_csdm_config(edit_tl: dict, demo_path: Path, output_dir: Path,
                       segments: list[dict] | None = None) -> dict:
    """Build CSDM config JSON with segments as sequences.

    If *segments* is None, uses all segments from the edit timeline.
    Otherwise renders only the supplied subset.
    """
    if segments is None:
        segments = edit_tl["segments"]

    # Pre-extract crosshairs for all unique POV players
    pov_sids = {seg["pov_steam_id"] for seg in segments}
    crosshair_cache = {}
    for sid in pov_sids:
        cvars = _get_player_crosshair_cvars(sid, demo_path)
        if cvars:
            crosshair_cache[sid] = cvars
            print(f"  Crosshair for {sid}: {len(cvars)} cvars")

    sequences = []
    seq_num = 1

    for seg in segments:
        start_tick = seg["start_tick"]
        end_tick = seg["end_tick"]

        # Swap if inverted (e.g. buy-phase offset pushed start past end)
        if start_tick >= end_tick:
            start_tick, end_tick = end_tick, start_tick
        # Ensure minimum duration
        if end_tick - start_tick < 64:
            end_tick = start_tick + 64

        pov_sid = seg["pov_steam_id"]
        cvars = crosshair_cache.get(pov_sid, [])

        cfg_lines = ["crosshair 1", "cl_chatfilters 63", "snd_mvp_volume 0",
                     "cl_draw_only_deathnotices 0", "cl_drawhud 1", "cl_showfps 0", "net_graph 0"] + cvars
        cfg_text = "\n".join(cfg_lines) + "\n"

        seq = {
            "number": seq_num,
            "startTick": start_tick,
            "endTick": end_tick,
            "cfg": cfg_text,
            "showXRay": False,
            "showAssists": True,
            "showOnlyDeathNotices": False,
            "playersOptions": [],
            "cameras": [],
            "playerCameras": [
                {"tick": start_tick, "playerSteamId": pov_sid, "playerName": "pov"},
            ],
            "playerVoicesEnabled": True,
            "recordAudio": True,
            "deathNoticesDuration": 5,
        }
        sequences.append(seq)
        seq_num += 1
    
    config = {
        "demoPath": str(demo_path.resolve()),
        "outputFolderPath": str(output_dir.resolve()),
        "recordingSystem": "HLAE",
        "recordingOutput": "video",
        "encoderSoftware": "FFmpeg",
        "framerate": CSDM_RECORD_FRAMERATE,
        "width": TARGET_WIDTH,
        "height": TARGET_HEIGHT,
        "closeGameAfterRecording": True,
        "concatenateSequences": False,
        "trueView": False,
        "ffmpegSettings": _ffmpeg_settings(),
        "sequences": sequences,
    }
    return config


def _ffmpeg_settings() -> dict:
    return {
        "constantRateFactor": 14,
        "videoContainer": "mp4",
        "videoCodec": "h264_nvenc",
        "audioCodec": "aac",
        "audioBitrate": 256,
        "inputParameters": "",
        "outputParameters": "-cq 14 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
        "customLocationEnabled": True,
        "customExecutableLocation": FFMPEG,
    }


def _read_actions_file(actions_path: Path) -> list | None:
    try:
        return json.loads(actions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_actions_atomic(actions_path: Path, raw: list) -> None:
    payload = json.dumps(raw, indent=2) + "\n"
    tmp_path = actions_path.with_name(f"{actions_path.name}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    for attempt in range(30):
        try:
            tmp_path.replace(actions_path)
            return
        except PermissionError:
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"Failed to write actions file after retries: {actions_path}")


def _actions_paths_for_demo(demo_path: str) -> list[Path]:
    """Return all possible CSDM actions file locations to poll."""
    demo = Path(demo_path)
    return [
        demo.parent / f"{demo.stem}_actions.json",
        Path.home() / "AppData" / "Local" / "cs-demo-manager" / "analysis" / demo.stem / "actions.json",
    ]


def _resolve_actions_path(demo_path: str) -> Path:
    """Find the actual CSDM actions file. Raises if not found at either location."""
    for p in _actions_paths_for_demo(demo_path):
        if p.exists():
            return p
    expected = " or ".join(str(p) for p in _actions_paths_for_demo(demo_path))
    raise FileNotFoundError(f"CSDM actions file not found at: {expected}")


def _build_inject_plan(segments: list[dict], demo_path: Path) -> dict[int, list[dict]]:
    """Build camera inject plan for all sequences.

    Pre-resolves spec_player indices so failures are visible before CSDM starts.
    """
    plan: dict[int, list[dict]] = {}
    for i, seg in enumerate(segments):
        pov_sid = seg["pov_steam_id"]
        start_tick = seg["start_tick"]
        idx = _resolve_spec_player_index(str(demo_path), pov_sid, start_tick)
        if idx is None:
            _dbg("plan", f"  [WARN] seq {i}: could not resolve steamid {pov_sid} at tick {start_tick} — camera may be wrong")
        plan[i] = [{
            "cmd": "spec_mode 1",
            "tick": start_tick,
            "_pov_steam_id": pov_sid,
            "_resolved_idx": idx,
        }]
    return plan


def _resolve_spec_player_index(demo_path: str, steamid: str, tick: int,
                               window: int = 128) -> int | None:
    """Resolve SteamID -> CSDM spec_player entity index (user_id + 1).

    Tries a window of ticks around *tick* (±*window* ticks) because the
    player may not be alive at the exact start tick.  Returns the first
    successful resolution found.
    """
    try:
        from demoparser2 import DemoParser
    except ImportError:
        return None

    parser = DemoParser(str(demo_path))
    ticks_to_try = list(range(max(0, tick - window), tick + window + 1, 32))
    # Always include the exact tick
    if tick not in ticks_to_try:
        ticks_to_try.append(tick)
        ticks_to_try.sort()

    try:
        pose_df = parser.parse_ticks(["user_id", "steamid"], ticks=ticks_to_try)
    except Exception:
        return None
    if pose_df is None or pose_df.empty or "steamid" not in pose_df.columns:
        return None

    rows = pose_df[pose_df["steamid"].astype(str) == str(steamid)]
    if rows.empty:
        _dbg("resolve", f"  steamid {steamid} not found in any tick in window [{tick-window}..{tick+window}]")
        return None
    idx = int(rows.iloc[0]["user_id"]) + 1
    _dbg("resolve", f"  steamid {steamid} -> user_id {idx-1} -> spec_player {idx} (tick {int(rows.iloc[0].get('tick', tick))})")
    return idx


def _csdm_actions_file_complete(raw: list, expected_blocks: int) -> bool:
    """Check if CSDM actions file has all blocks with mirv_streams record start."""
    if len(raw) < expected_blocks:
        return False
    for seq in raw[:expected_blocks]:
        actions = seq.get("actions", [])
        if not any("mirv_streams record start" in a.get("cmd", "") for a in actions):
            return False
    return True


def _poll_and_inject(
    demo_path: str,
    segments: list[dict],
    inject_plan: dict[int, list[dict]],
    stop_event: threading.Event,
    actions_snapshot_path: Path | None = None,
) -> None:
    """Background thread: wait for CSDM actions file, inject camera commands."""
    possible_paths = _actions_paths_for_demo(demo_path)
    merge_logged = False
    csdm_file_seen = False
    last_incomplete_log = 0.0
    expected_blocks = len(inject_plan)
    _dbg("poll", f"polling {len(possible_paths)} actions paths, expecting {expected_blocks} blocks")

    while not stop_event.wait(0.001):
        actions_path = None
        for p in possible_paths:
            if p.is_file():
                actions_path = p
                break
        if actions_path is None:
            continue

        raw = _read_actions_file(actions_path)
        if raw is None:
            continue

        if not _csdm_actions_file_complete(raw, expected_blocks):
            continue
        if not csdm_file_seen:
            csdm_file_seen = True
            _dbg("inject", f"CSDM actions file ready ({actions_path})")

        fresh = _read_actions_file(actions_path)
        if fresh is None:
            continue
        raw = fresh

        merged = raw
        for seq_idx, actions in inject_plan.items():
            if seq_idx >= len(merged):
                continue
            seq_actions = merged[seq_idx].setdefault("actions", [])
            for action in actions:
                if action.get("cmd") == "spec_mode 1":
                    pov_sid = action.get("_pov_steam_id")
                    tick = action.get("tick")
                    idx = action.get("_resolved_idx")
                    if idx is None:
                        # Retry resolution in case it failed during pre-resolve
                        if pov_sid and tick:
                            idx = _resolve_spec_player_index(demo_path, pov_sid, tick)
                    if pov_sid and tick and idx is not None:
                        if not any(f"spec_player {idx}" in str(a.get("cmd", "")) for a in seq_actions):
                            seq_actions.append({"cmd": f"spec_player {idx}", "tick": tick})
                            _dbg("inject", f"  seq[{seq_idx}] spec_player {idx} @ tick {tick} (steamid {pov_sid})")
            seq_actions.sort(key=lambda a: a.get("tick", 0))

        _write_actions_atomic(actions_path, merged)
        if actions_snapshot_path is not None:
            _write_actions_atomic(actions_snapshot_path, merged)

        verify = _read_actions_file(actions_path)
        if verify is None:
            continue

        ok = True
        for seq_idx, actions in inject_plan.items():
            if seq_idx >= len(verify):
                ok = False
                break
            for action in actions:
                if action.get("cmd") == "spec_mode 1":
                    pov_sid = action.get("_pov_steam_id")
                    tick = action.get("tick")
                    idx = action.get("_resolved_idx")
                    if idx is None and pov_sid and tick:
                        idx = _resolve_spec_player_index(demo_path, pov_sid, tick)
                    if pov_sid and tick and idx is not None:
                        found = any(
                            f"spec_player {idx}" in str(a.get("cmd", "")) and a.get("tick") == tick
                            for a in verify[seq_idx].get("actions", [])
                        )
                        if not found:
                            ok = False

        if not ok:
            now = time.monotonic()
            if now - last_incomplete_log >= 0.5:
                _dbg("inject", "merge incomplete, retrying...")
                last_incomplete_log = now
            continue

        if not merge_logged:
            n_actions = sum(len(v) for v in inject_plan.values())
            _dbg("inject", f"merged precomputed camera actions ({len(inject_plan)} sequence(s), {n_actions} cmd(s))")
            merge_logged = True
        break


def _run_csdm_batch(config_path: Path, demo_path: str, segments: list[dict]) -> int:
    """Run CSDM with config file, injecting camera commands in background."""
    cmd = [CSDM, "video", "--config-file", str(config_path.resolve())]
    _dbg("csdm", f"command: {' '.join(cmd)}")

    actions_snapshot = config_path.parent / "actions_merged.json"
    inject_plan = _build_inject_plan(segments, Path(demo_path))
    _dbg("csdm", f"inject plan: {len(inject_plan)} sequences")

    stop_poll = threading.Event()
    poll_thread = threading.Thread(
        target=_poll_and_inject,
        args=(demo_path, segments, inject_plan, stop_poll, actions_snapshot),
        daemon=True,
    )
    poll_thread.start()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    finally:
        stop_poll.set()
        poll_thread.join(timeout=5)

    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out if proc.returncode != 0 else out[-3000:], flush=True)
    _dbg("csdm", f"exit code: {proc.returncode}")
    return proc.returncode


def _find_csdm_sequence_files(search_dir: Path, num_segments: int) -> list[Path]:
    """Find CSDM sequence output files.

    CSDM may write either:
      1.  <N>-sequence/video.mp4  dirs
      2.  flat sequence-{N}-tick-{A}-to-{B}.mp4  files
    """
    # First: look for <N>-sequence/video.mp4 dirs
    seq_dirs = sorted(
        [d for d in search_dir.iterdir()
         if d.is_dir() and re.match(r"^\d+-sequence$", d.name)],
        key=lambda d: int(d.name.split("-")[0])
    )
    _dbg("find", f"searching {search_dir} for <N>-sequence/ dirs")
    if seq_dirs:
        _dbg("find", f"found {len(seq_dirs)} sequence dirs")
        files = []
        for d in seq_dirs:
            v = d / "video.mp4"
            if v.is_file():
                files.append(v)
                _dbg("find", f"  {d.name}/video.mp4 ({v.stat().st_size / 1e6:.1f} MB)")
            else:
                _dbg("find", f"  {d.name}/video.mp4 MISSING")
        return files

    # Fallback: flat sequence-{N}-tick-{A}-to-{B}.mp4 files
    flat_seq = sorted(
        search_dir.glob("sequence-*-tick-*-to-*.mp4"),
        key=lambda p: int(re.match(r"sequence-(\d+)", p.name).group(1)),
    )
    if flat_seq:
        _dbg("find", f"found {len(flat_seq)} flat sequence files")
        for f in flat_seq:
            _dbg("find", f"  {f.name} ({f.stat().st_size / 1e6:.1f} MB)")
        return flat_seq

    # Fallback: any video.mp4 in subdirs
    fallback = sorted(search_dir.rglob("video.mp4"))
    if fallback:
        _dbg("find", f"fallback: found {len(fallback)} video.mp4 in subdirs")
        return fallback

    _dbg("find", f"NO sequence files found in {search_dir}")
    return []


def _render_batch(
    edit_tl: dict,
    demo_path: Path,
    render_dir: Path,
    config_dir: Path,
    segments: list[dict],
    batch_idx: int,
    batch_start_global: int,
    batch_end_global: int,
    all_segments: list[dict],
) -> bool:
    """Render one batch of segments. Returns True if rendered (or skipped as done)."""
    # Check if this batch already has its output file
    batch_file = render_dir / f"batch-{batch_start_global:03d}-{batch_end_global:03d}.mp4"
    if batch_file.exists() and batch_file.stat().st_size >= 1_048_576:
        _dbg("batch", f"[SKIP] batch {batch_idx+1} already done: {batch_file.name} ({batch_file.stat().st_size/1e6:.0f} MB)")
        return True

    print(f"\n  Batch {batch_idx+1}: segments {batch_start_global}-{batch_end_global} ({len(segments)} segments)")
    for i, seg in enumerate(segments):
        global_idx = batch_start_global + i
        tick_range = seg["end_tick"] - seg["start_tick"]
        _dbg("batch", f"  seg {global_idx}: tick {seg['start_tick']}-{seg['end_tick']} ({tick_range} ticks, ~{tick_range/64:.1f}s)")

    config = _build_csdm_config(edit_tl, demo_path, render_dir, segments=segments)
    config_path = config_dir / f"batch_{batch_idx+1}_config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    _dbg("config", f"written: {config_path}")

    t0 = time.time()
    ret = _run_csdm_batch(config_path, str(demo_path), segments)
    elapsed = time.time() - t0

    if ret != 0:
        _dbg("render", f"CSDM batch {batch_idx+1} FAILED (exit {ret}, {elapsed:.0f}s)")
        _dbg("render", f"Check {render_dir} for partial output")
        return False

    _dbg("render", f"CSDM batch {batch_idx+1} finished in {elapsed:.0f}s")

    # CSDM writes <N>-sequence/video.mp4 into outputFolderPath.
    seq_files = _find_csdm_sequence_files(render_dir, len(segments))
    if len(seq_files) != len(segments):
        _dbg("render", f"[WARN] Expected {len(segments)} sequence files, found {len(seq_files)}")

    # Rename sequence files to round-NNN naming for tracking
    for i, (seq_file, seg) in enumerate(zip(seq_files, segments)):
        start_tick = seg["start_tick"]
        end_tick = seg["end_tick"]
        name = f"round-{batch_start_global+i:03d}-tick-{start_tick}-to-{end_tick}"
        dest = render_dir / f"{name}.mp4"
        if dest.exists() and dest.stat().st_size >= 1_048_576:
            _dbg("finalize", f"[SKIP] {dest.name} already exists")
        else:
            shutil.copy2(str(seq_file), str(dest))
            mb = dest.stat().st_size / 1e6
            _dbg("finalize", f"{seq_file.parent.name}/{seq_file.name} -> {dest.name} ({mb:.1f} MB)")

    # Clean up <N>-sequence/ dirs left by CSDM
    for d in render_dir.iterdir():
        if d.is_dir() and re.match(r"^\d+-sequence$", d.name):
            shutil.rmtree(d, ignore_errors=True)

    # Concat all round files for this batch into a single batch MP4
    batch_round_files = sorted(
        render_dir.glob(f"round-*-tick-*-to-*.mp4"),
        key=lambda p: int(p.name.split("-")[1]),
    )
    # Only keep round files that belong to this batch
    batch_round_files = [
        p for p in batch_round_files
        if batch_start_global + 1 <= int(p.name.split("-")[1]) <= batch_end_global
    ]
    if not batch_round_files:
        _dbg("batch", f"[ERROR] No round files found for batch {batch_idx+1}")
        return False

    if len(batch_round_files) == 1:
        shutil.copy2(str(batch_round_files[0]), str(batch_file))
    else:
        _concat_videos(batch_round_files, batch_file)

    if not batch_file.exists() or batch_file.stat().st_size < 1_048_576:
        _dbg("batch", f"[ERROR] Batch file {batch_file.name} missing or too small")
        return False

    mb = batch_file.stat().st_size / 1e6
    _dbg("batch", f"batch {batch_idx+1} done: {batch_file.name} ({mb:.0f} MB)")
    return True


def render_edit_timeline(
    edit_tl: dict,
    demo_override: Path | None = None,
    output_dir: Path | None = None,
    batch_size: int = 0,
    specific_batch: int | None = None,
) -> Path:
    demo_path = Path(demo_override) if demo_override else Path(edit_tl["demo_path"])
    if not demo_path.exists():
        raise FileNotFoundError(f"Demo not found: {demo_path}")

    all_segments = edit_tl["segments"]
    if not all_segments:
        raise ValueError("No segments in edit timeline")

    if output_dir:
        base_out = output_dir.resolve()
    else:
        stem = demo_path.stem.replace("-p1", "").replace(".dem", "")
        base_out = (_PROJECT_ROOT / "renders" / f"hl-{stem}").resolve()
    base_out.mkdir(parents=True, exist_ok=True)

    print(f"Edit Timeline: {len(all_segments)} segments, map={edit_tl['map']}")
    print(f"Demo: {demo_path.resolve()}")
    print(f"Output: {base_out}")

    config_dir = base_out / "batch_config"
    config_dir.mkdir(parents=True, exist_ok=True)

    # CSDM writes <N>-sequence/ dirs into outputFolderPath.
    render_dir = base_out / "segments"
    render_dir.mkdir(parents=True, exist_ok=True)

    # Split segments into batches
    if batch_size > 0:
        batches = []
        for i in range(0, len(all_segments), batch_size):
            batches.append(all_segments[i:i + batch_size])
    else:
        batches = [all_segments]

    total_batches = len(batches)
    print(f"  Batches: {total_batches} (batch size: {batch_size if batch_size > 0 else 'all'})")

    # Filter to specific batch if requested
    if specific_batch is not None:
        if specific_batch < 1 or specific_batch > total_batches:
            print(f"[ERROR] --batch {specific_batch} out of range (1-{total_batches})")
            sys.exit(1)
        batch_indices = [specific_batch - 1]
        print(f"  Rendering only batch {specific_batch}/{total_batches}")
    else:
        batch_indices = list(range(total_batches))

    t0 = time.time()
    rendered_count = 0

    for batch_idx in batch_indices:
        batch_segments = batches[batch_idx]
        batch_start_global = sum(len(b) for b in batches[:batch_idx]) + 1
        batch_end_global = batch_start_global + len(batch_segments) - 1

        ok = _render_batch(
            edit_tl, demo_path, render_dir, config_dir,
            batch_segments, batch_idx, batch_start_global, batch_end_global,
            all_segments,
        )
        if not ok:
            print(f"[ERROR] Batch {batch_idx+1} failed")
            sys.exit(1)
        rendered_count += 1

    elapsed = time.time() - t0
    _dbg("done", f"{rendered_count} batch(es) rendered in {elapsed:.0f}s to {render_dir}")

    # After all batches are done, concat into combined.mp4 + upscale
    if specific_batch is None:
        batch_files = sorted(
            render_dir.glob("batch-*-*.mp4"),
            key=lambda p: int(p.name.split("-")[1]),
        )
        if batch_files:
            combined = render_dir / "combined.mp4"
            if len(batch_files) == 1:
                shutil.copy2(str(batch_files[0]), str(combined))
            else:
                _concat_videos(batch_files, combined)

            if combined.exists() and combined.stat().st_size >= 1_048_576:
                mb = combined.stat().st_size / 1e6
                _dbg("concat", f"combined.mp4: {mb:.0f} MB")

                # Upscale to 2560x1440 (VP9 trick)
                scaled = render_dir / "combined_scaled.mp4"
                _encode_scaled(combined, scaled)
                scaled.replace(combined)
                _dbg("done", f"Upscaled combined.mp4 to {TARGET_WIDTH}x{TARGET_HEIGHT}")
                print(f"\nDone! Final video: {combined}")
            else:
                print(f"\n[WARN] combined.mp4 missing or too small")
        else:
            print(f"\n[WARN] No batch files found for concat")
    else:
        print(f"\nBatch {specific_batch} rendered. Re-run without --batch to concat all batches.")

    return render_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Edit Timeline segments via CSDM batch config")
    parser.add_argument("edit_timeline", type=Path, help="Path to edit_timeline.json")
    parser.add_argument("--demo", type=Path, help="Override demo path")
    parser.add_argument("--output", "-o", type=Path, help="Override output directory")
    parser.add_argument("--batches", type=int, default=0,
                        help="Segments per batch (default: 0 = all in one). Each batch produces one MP4.")
    parser.add_argument("--batch", type=int, default=None,
                        help="Render only this batch number (1-indexed). Omit to render all batches.")
    args = parser.parse_args()

    try:
        edit_tl = load_edit_timeline(args.edit_timeline)
        render_edit_timeline(edit_tl, args.demo, args.output,
                            batch_size=args.batches, specific_batch=args.batch)
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
