from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

_BATCH_RE = re.compile(r"batch-(\d+)-(\d+)\.mp4$")
_ROUND_SEQ_RE = re.compile(r"round-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")


def _batch_range(f: Path) -> tuple[int, int]:
    """Return (start_round, end_round) for a batch or per-round sequence file."""
    m = _ROUND_SEQ_RE.match(f.name)
    if m:
        rn = int(m.group(1))
        return (rn, rn)
    m = _BATCH_RE.match(f.name)
    return (int(m.group(1)), int(m.group(2)))


def _concat_two(a: Path, b: Path, out: Path) -> None:
    a_mb = a.stat().st_size / 1024 / 1024
    b_mb = b.stat().st_size / 1024 / 1024
    print(f"\n  [Concat] {a_mb:.0f} MB + {b_mb:.0f} MB (disk I/O, no re-encode)...", end=" ", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "files.txt"
        with open(lst, "w") as f:
            f.write(f"file '{a.resolve().as_posix()}'\n")
            f.write(f"file '{b.resolve().as_posix()}'\n")
        r = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", str(out)],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            print(f"\n  [CONCAT FAILED] {r.stderr[-300:]}")
            sys.exit(1)
    out_mb = out.stat().st_size / 1024 / 1024
    print(f"OK ({out_mb:.0f} MB)")


def _parse_batches(
    folder: Path,
    combined_exists: bool = False,
    allow_gaps: bool = False,
) -> tuple[list[Path], int]:
    batch_files = sorted(
        [f for f in folder.glob("batch-*.mp4") if _BATCH_RE.match(f.name)],
        key=lambda f: int(_BATCH_RE.match(f.name).group(1)),
    )
    round_files = sorted(
        [f for f in folder.glob("round-*.mp4") if _ROUND_SEQ_RE.match(f.name)],
        key=lambda f: int(_ROUND_SEQ_RE.match(f.name).group(1)),
    )
    files = sorted(batch_files + round_files, key=lambda f: _batch_range(f)[0])
    if not files:
        raise FileNotFoundError(f"No batch-*.mp4 / round-*.mp4 files in {folder}")
    expected_start = _batch_range(files[0])[0]
    for f in files:
        start, end = _batch_range(f)
        if start > end:
            raise ValueError(f"Invalid batch range (start > end): {f.name}")
        if start < expected_start:
            raise ValueError(
                f"CONCAT_BATCH_OVERLAP: batch {f.name} starts at round {start} "
                f"but expected round {expected_start} (overlap with previous batch)"
            )
        if start > expected_start:
            if allow_gaps:
                print(
                    f"  [GAP] rounds {expected_start}-{start - 1} missing "
                    f"(--skip-failed-rounds); continuing with {f.name}"
                )
            else:
                msg = (
                    f"remaining batches not contiguous (expected {expected_start}, "
                    f"got {f.name})"
                ) if combined_exists else (
                    f"expected batch starting at round {expected_start}, "
                    f"got {f.name} (check for missing or overlapping batches)"
                )
                raise ValueError(f"CONCAT_BATCH_GAP: {msg}")
        expected_start = end + 1
    return files, _batch_range(files[0])[0]


_SEQ_RE = re.compile(r"sequence-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")


def _parse_sequence_files(folder: Path, batch_files: list[Path] | None = None) -> dict | None:
    """Parse round-*-tick-START-to-END.mp4 (preferred) or legacy
    sequence-*-tick-START-to-END.mp4 files left by CSDM.

    Round number is encoded in the filename for round-* files (authoritative);
    legacy sequence-* files are paired in order with batch_files. Returns
    {per_round_ticks, per_round_durations} or None if no sequence/round files.
    """
    # Preferred: round-{rn:03d}-tick-A-to-B.mp4 (round encoded in filename)
    items = []
    for f in folder.rglob("round-*-tick-*-to-*.mp4"):
        m = _ROUND_SEQ_RE.match(f.name)
        if m:
            items.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), f))
    if items:
        per_round_ticks: dict[int, tuple[int, int]] = {}
        per_round_durations: dict[int, float] = {}
        for rn, a, b, f in items:
            per_round_ticks[rn] = (a, b)
            per_round_durations[rn] = _probe_duration(f)
        print(
            f"  [seq] {len(items)} round files parsed: "
            f"{sum(per_round_durations.values()):.2f}s total across rounds"
        )
        return {"per_round_ticks": per_round_ticks, "per_round_durations": per_round_durations}

    # Legacy: sequence-{i}-tick-*.mp4 paired by order with batch_files
    seqs = []
    for f in folder.rglob("sequence-*-tick-*-to-*.mp4"):
        m = _SEQ_RE.match(f.name)
        if not m:
            continue
        seqs.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), f))
    if not seqs:
        return None
    seqs.sort(key=lambda x: x[0])
    round_nums: list[int] = []
    if batch_files:
        for b in batch_files:
            m = _BATCH_RE.match(b.name)
            if not m:
                continue
            for rn in range(int(m.group(1)), int(m.group(2)) + 1):
                round_nums.append(rn)
    if not round_nums or len(seqs) != len(round_nums):
        print(
            f"  [warn] legacy sequence files ({len(seqs)}) != rounds in batches "
            f"({len(round_nums)}); keeping even-split durations"
        )
        return None
    per_round_ticks = {}
    per_round_durations = {}
    for (si, a, b, f), rn in zip(seqs, round_nums):
        per_round_ticks[rn] = (a, b)
        per_round_durations[rn] = _probe_duration(f)
    print(
        f"  [seq] {len(seqs)} legacy sequence files parsed: "
        f"{sum(per_round_durations.values()):.2f}s total across rounds"
    )
    return {"per_round_ticks": per_round_ticks, "per_round_durations": per_round_durations}


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0.0
    data = json.loads(r.stdout)
    return float(data.get("format", {}).get("duration", 0))


def validate_round_offsets_sidecar(
    data: dict,
    *,
    video_duration_seconds: float | None = None,
    allow_gaps: bool = False,
) -> list[str]:
    """Validate a ``*.round_offsets.json`` payload.

    Returns a list of error strings (empty = PASS). Catches the class of bugs
    where batch durations were probed from cumulative ``combined.mp4`` after
    append — sidecar claimed ~1× the real video duration and late round
    offsets landed past EOF (e.g. total 4195s / round 22 at 3093s on a
    2202s POV).
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["sidecar is not a JSON object"]

    try:
        total_rounds = int(data.get("total_rounds") or 0)
        total_dur = float(data.get("total_duration_seconds") or 0)
    except (TypeError, ValueError):
        return ["sidecar total_rounds/total_duration_seconds not numeric"]

    raw_offsets = data.get("round_offsets") or {}
    if not isinstance(raw_offsets, dict) or not raw_offsets:
        errors.append("round_offsets missing or empty")
        return errors

    try:
        offsets = {int(k): float(v) for k, v in raw_offsets.items()}
    except (TypeError, ValueError):
        return ["round_offsets keys/values not numeric"]

    if total_rounds and total_rounds != len(offsets):
        errors.append(
            f"total_rounds={total_rounds} but round_offsets has {len(offsets)} entries"
        )

    sorted_rns = sorted(offsets)
    if sorted_rns[0] != 1:
        msg = f"round_offsets starts at round {sorted_rns[0]} (not 1)"
        if allow_gaps:
            return errors  # expected with --skip-failed-rounds, not an error
        errors.append(msg)
    # Non-contiguous gaps (e.g. 2,4,5 where 3 is missing) are valid under --skip-failed-rounds.
    # Just check monotonic timestamps below.

    if offsets[sorted_rns[0]] < -0.01:
        errors.append(f"first round offset is negative: {offsets[sorted_rns[0]]}")
    for prev, rn in zip(sorted_rns, sorted_rns[1:]):
        if offsets[rn] + 1e-6 < offsets[prev]:
            errors.append(
                f"round_offsets not monotonic: r{prev}={offsets[prev]:.3f} > "
                f"r{rn}={offsets[rn]:.3f}"
            )

    batches = data.get("batches") or []
    if batches:
        try:
            batch_sum = sum(float(b["duration_seconds"]) for b in batches)
        except (KeyError, TypeError, ValueError):
            errors.append("batches[].duration_seconds missing or not numeric")
            batch_sum = None
        if batch_sum is not None and total_dur > 0:
            if abs(batch_sum - total_dur) > 0.5:
                errors.append(
                    f"sum(batch durations)={batch_sum:.3f}s != "
                    f"total_duration_seconds={total_dur:.3f}s"
                )

    per_durs = data.get("per_round_durations") or {}
    if per_durs and total_dur > 0:
        try:
            pr_sum = sum(float(v) for v in per_durs.values())
        except (TypeError, ValueError):
            errors.append("per_round_durations values not numeric")
            pr_sum = None
        if pr_sum is not None and abs(pr_sum - total_dur) > 0.5:
            errors.append(
                f"sum(per_round_durations)={pr_sum:.3f}s != "
                f"total_duration_seconds={total_dur:.3f}s"
            )

    # Hard gate vs the real video — this is the check that would have caught
    # the Twistzz Cache 4195s-vs-2202s sidecar corruption.
    if video_duration_seconds is not None and video_duration_seconds > 0:
        tol = max(2.0, video_duration_seconds * 0.02)
        if total_dur <= 0:
            errors.append("total_duration_seconds missing/zero while video was probed")
        elif abs(total_dur - video_duration_seconds) > tol:
            errors.append(
                f"total_duration_seconds={total_dur:.3f}s does not match video "
                f"duration={video_duration_seconds:.3f}s (tol ±{tol:.1f}s) — "
                f"sidecar is corrupt or from a different concat"
            )
        last_off = offsets[sorted_rns[-1]]
        if last_off >= video_duration_seconds:
            errors.append(
                f"last round (r{sorted_rns[-1]}) offset {last_off:.3f}s is past "
                f"video end {video_duration_seconds:.3f}s"
            )
        for rn, off in offsets.items():
            if off >= video_duration_seconds:
                errors.append(
                    f"round {rn} offset {off:.3f}s is past video end "
                    f"{video_duration_seconds:.3f}s"
                )
                break

    return errors


def concat_rounds(folder: Path, allow_gaps: bool = False) -> Path:
    combined = folder / "combined.mp4"
    offset_path = folder / "combined.round_offsets.json"
    resuming = combined.exists()
    files, _ = _parse_batches(folder, combined_exists=resuming, allow_gaps=allow_gaps)
    new_rounds = sum(
        _batch_range(f)[1] - _batch_range(f)[0] + 1
        for f in files
    )

    print(f"Concatenating {len(files)} batch(es) ({new_rounds} rounds) -> {combined}")

    # Parse sequence/round tick spans BEFORE consuming files: the per-round
    # round-*.mp4 files get deleted as they are concatenated into combined.mp4,
    # so their tick spans must be read first.
    seq_fields = _parse_sequence_files(folder, files)
    if seq_fields:
        print(f"  [seq] per-round tick spans available ({len(seq_fields['per_round_ticks'])} rounds)")

    round_offsets: dict[int, float] = {}
    batch_offsets: list[dict] = []
    cumulative = 0.0

    # Resume: seed offsets/batches from the existing sidecar and set
    # cumulative from the probed combined duration so newly appended batches
    # continue the timeline instead of rewriting a partial sidecar.
    if resuming:
        cumulative = _probe_duration(combined)
        if offset_path.is_file():
            prev = json.loads(offset_path.read_text(encoding="utf-8"))
            round_offsets = {
                int(k): float(v) for k, v in (prev.get("round_offsets") or {}).items()
            }
            batch_offsets = list(prev.get("batches") or [])
            print(
                f"  [resume] seeded {len(round_offsets)} rounds from sidecar; "
                f"combined={cumulative:.2f}s"
            )
        elif cumulative > 0:
            print(
                "  [warn] resume without sidecar — cannot reconstruct offsets "
                "for rounds already inside combined.mp4"
            )

    for f in files:
        s, e = _batch_range(f)

        # Probe THIS batch before consuming it. After rename/unlink, probing
        # `combined` would return the cumulative duration so far — which for
        # batch N>1 double-counts earlier batches into total_duration_seconds
        # and pushes later round_offsets past the end of the video
        # (e.g. round 22 at 3093s in a 2202s POV).
        dur = _probe_duration(f)
        if dur <= 0:
            print(f"  [warn] ffprobe returned 0 duration for {f.name}")

        if not combined.exists():
            f.rename(combined)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} -> {combined.name} ({mb:.0f} MB)")
        else:
            tmp = folder / "_tmp.mp4"
            _concat_two(combined, f, tmp)
            tmp.replace(combined)
            f.unlink()  # consumed into combined; remaining batch-*.mp4 = not-yet-concatted (resume signal)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} appended ({mb:.0f} MB)")

        # Distribute this batch's duration evenly across its rounds
        per_round = dur / (e - s + 1)
        for r in range(s, e + 1):
            offset = cumulative + (r - s) * per_round
            round_offsets[r] = offset
        cumulative += dur
        batch_offsets.append({
            "batch": f.name,
            "round_start": s,
            "round_end": e,
            "duration_seconds": dur,
        })

    # Write round offset sidecar. When CSDM sequence files are present, parse
    # the actual per-round tick ranges and probed video durations from them —
    # this is ground truth (avoids concat's even-split approximation when
    # rounds have unequal freeze + play + death spans). Falls back to the
    # even-split `per_round` estimate above when sequences are absent.
    if seq_fields:
        per_round_ticks = seq_fields["per_round_ticks"]
        per_round_durations = seq_fields["per_round_durations"]
        seq_rounds = sorted(per_round_durations.keys())
        # On resume, only trust sequences when they cover a contiguous 1..N
        # that includes every round we already tracked — otherwise keep the
        # seeded even-split offsets for prior rounds.
        # Check if sequence files cover a contiguous range (ideal case:
        # all rounds present). With --skip-failed-rounds, early/late rounds
        # may be missing—that's OK, we still write the per-round data.
        covers_all = (
            len(seq_rounds) > 0
            and seq_rounds == list(range(seq_rounds[0], seq_rounds[-1] + 1))
            and (not round_offsets or seq_rounds[-1] >= max(round_offsets))
        )
        if covers_all:
            cumulative = 0.0
            round_offsets = {}
            for rn in seq_rounds:
                round_offsets[rn] = cumulative
                cumulative += per_round_durations[rn]
            for b in batch_offsets:
                rn_start, rn_end = b["round_start"], b["round_end"]
                b["duration_seconds"] = sum(
                    per_round_durations[r] for r in range(rn_start, rn_end + 1)
                    if r in per_round_durations
                )
        else:
            print(
                "  [warn] sequence files don't cover all rounds on resume; "
                "keeping even-split / seeded offsets"
            )
            # Do NOT set seq_fields=None — still write per-round data
            # for whatever rounds we have (critical for --skip-failed-rounds)

    total_rounds = len(round_offsets)
    payload = {
        "total_rounds": total_rounds,
        "total_duration_seconds": cumulative,
        "round_offsets": {str(k): round(v, 3) for k, v in round_offsets.items()},
        "batches": batch_offsets,
    }
    if seq_fields:
        payload["per_round_ticks"] = {
            str(k): list(v) for k, v in seq_fields["per_round_ticks"].items()
        }
        payload["per_round_durations"] = {
            str(k): round(v, 3) for k, v in seq_fields["per_round_durations"].items()
        }
    with open(offset_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nRound offsets: {offset_path} ({len(round_offsets)} rounds)")

    video_dur = _probe_duration(combined)
    sidecar_errs = validate_round_offsets_sidecar(
        payload, video_duration_seconds=video_dur, allow_gaps=allow_gaps,
    )
    if sidecar_errs:
        for err in sidecar_errs:
            print(f"  [SIDECAR_INVALID] {err}")
        sys.exit(1)
    print(
        f"  [OK] sidecar validated against combined.mp4 "
        f"({video_dur:.2f}s, {len(round_offsets)} rounds)"
    )

    print(f"\nDone. {combined} ({combined.stat().st_size // 1024 // 1024} MB)")
    return combined


def _get_resolution(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0, 0
    data = json.loads(r.stdout)
    s = data.get("streams", [])
    return (s[0]["width"], s[0]["height"]) if s else (0, 0)


def _is_valid_video(path: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def _check_gpu_available() -> bool:
    """Check if GPU is available for CUDA upscaling via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            if 'ffmpeg' in line.lower():
                return False
        return True
    except Exception:
        return True  # optimistic if nvidia-smi fails


def _scale_vf(w: int, h: int, scaling_mode: str = "") -> tuple[str, str]:
    """One-pass scale to target WxH — always stretch (ignore Black Bars).

    ``setsar=1`` is required: without it ffmpeg keeps the source DAR (e.g. 4:3)
    by writing a non-square sample aspect ratio, so players display the file
    pillarboxed/unsquished instead of anamorphically stretched to 16:9.
    """
    _ = scaling_mode
    return f"scale={w}:{h}:flags=spline,setsar=1,format=nv12", "stretch"



def _write_filter_script(filter_complex: str) -> str:
    """Write a filter_complex string to a temp script file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="fcs_")
    with open(fd, "w", encoding="utf-8") as f:
        f.write(filter_complex)
    return path


def _build_shade_filter(
    box_rects: list[tuple[int, int, int, int]],
    shade_ctrls: list[Path],
    fps: float,
    w: int,
    h: int,
    scaling_mode: str,
) -> tuple[str, int]:
    """Build a filter_complex graph that composites native-res voice-shade boxes
    THEN scales video+shade together to target WxH.

    Returns ``(filter_complex, next_input_index)``. The shade is applied at the
    video's NATIVE coordinates, so the trailing ``scale`` stretches the video and
    the shade identically — the boxes stay locked to the avatars. This is the
    whole reason the shade lives in the scale step, not the overlay step.
    """
    vf, _ = _scale_vf(w, h, scaling_mode)
    parts = ["[0:v]null[v0]"]
    cur = "v0"
    idx = 1
    for ctrl, rect in zip(shade_ctrls, box_rects):
        x0, y0, x1, y1 = rect
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        cur2 = f"{cur}s"
        nxt = f"{cur}o"
        parts.append(
            f"[{idx}:v]scale={bw}:{bh}:flags=bilinear[{cur2}];"
            f"[{cur}][{cur2}]overlay=x={x0}:y={y0}:shortest=1[{nxt}]"
        )
        cur = nxt
        idx += 1
    parts.append(f"[{cur}]{vf}[vout]")
    fc = ";".join(parts)
    return fc, idx


def _encode_scaled(src: Path, dst: Path, w: int, h: int, scaling_mode: str,
                   shade: dict | None = None) -> None:
    """Single encode: resample (stretch and/or upscale) straight to target WxH.

    ``shade`` (optional) = ``{"box_rects": [...], "alpha_frames": np.ndarray,
    "fps": float}`` for the voice-activity shade. When provided the shade is
    composited at native resolution BEFORE the upscale, so the scale stretches
    the video and the shade together (boxes stay locked to avatars).
    """
    src_mb = src.stat().st_size / 1024 / 1024
    vf, label = _scale_vf(w, h, scaling_mode)
    print(f"\n  [Scale] {src_mb:.0f} MB -> {w}x{h} ({label}, spline + NVENC CQ8)...",
          end=" ", flush=True)

    if not _check_gpu_available():
        print("\n[ERROR] GPU not available — another ffmpeg process may be holding CUDA")
        print("  Kill stale ffmpeg processes with: taskkill /f /im ffmpeg.exe")
        sys.exit(1)

    temp = dst.with_suffix(".temp.mp4")
    t0 = time.time()

    filter_complex = None
    shade_ctrls: list[Path] = []
    next_input = 1
    cmd_prefix = ["ffmpeg", "-y", "-i", str(src)]
    if shade:
        import tempfile as _tf
        shade_tmp = _tf.mkdtemp(prefix="shade_scale_")
        try:
            from overlay.voice_shade import _write_alpha_control
            for bi, alpha in enumerate(shade["alpha_frames"]):
                ctrl = Path(shade_tmp) / f"box_{bi}.rgba"
                _write_alpha_control(ctrl, alpha)
                shade_ctrls.append(ctrl)
                cmd_prefix += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", "1x1",
                               "-r", f"{shade['fps']:.3f}", "-i", str(ctrl)]
            filter_complex, next_input = _build_shade_filter(
                shade["box_rects"], shade_ctrls, shade["fps"], w, h, scaling_mode)
        except Exception:
            import shutil as _sh
            _sh.rmtree(shade_tmp, ignore_errors=True)
            raise

    cmd = list(cmd_prefix)
    if filter_complex is not None:
        cmd += [
            "-filter_complex_script",
            _write_filter_script(filter_complex),
        ]
        cmd += ["-map", "[vout]", "-map", "0:a?", "-shortest"]
    else:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "8",
        "-maxrate", "200M", "-bufsize", "400M",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "5.1",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-movflags", "+faststart",
        "-c:a", "copy", str(temp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    elapsed = time.time() - t0
    if r.returncode != 0:
        temp.unlink(missing_ok=True)
        print(f"\n[ERROR] Scale failed: {r.stderr[-300:]}")
        print("  [Fallback] Retrying with CPU Lanczos + libx264...")
        if filter_complex is not None:
            fc_cpu = filter_complex.replace("flags=spline", "flags=lanczos")
            cmd = list(cmd_prefix)
            cmd += ["-filter_complex_script", _write_filter_script(fc_cpu)]
            cmd += ["-map", "[vout]", "-map", "0:a?", "-shortest"]
            cmd += [
                "-c:v", "libx264", "-crf", "15", "-preset", "slow",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-c:a", "copy", str(temp),
            ]
        else:
            vf_cpu = vf.replace("flags=spline", "flags=lanczos")
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
    print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")


def _build_shade_for_scale(args, folder: Path) -> dict | None:
    """Build the shade data dict for the scale step, or None if not requested.

    The shade must be computed against the NATIVE combined.mp4 (pre-scale) so the
    box coords are native and stretch together with the video during upscale.
    """
    if not args.voice_shade_demo:
        return None
    if not args.voice_shade_steam_id:
        print("[voice-shade] --voice-shade-steam-id required with --voice-shade-demo")
        sys.exit(1)
    combined = folder / "combined.mp4"
    offsets_path = folder / "combined.round_offsets.json"
    if not offsets_path.is_file():
        print("[voice-shade] combined.round_offsets.json not found; cannot align shade")
        sys.exit(1)

    # Use the same ffprobe/ffmpeg resolution as this module (from PATH).
    from overlay.voice_shade import build_voice_shade_data, _probe_video_info

    src = combined
    native_w, native_h, fps, dur = _probe_video_info(src)
    data = build_voice_shade_data(
        Path(args.voice_shade_demo), src, args.voice_shade_steam_id,
        json.loads(offsets_path.read_text(encoding="utf-8")), fps, dur,
        native_res=f"{native_w}x{native_h}",
        pov_side=args.voice_shade_side,
        fade=args.voice_shade_fade,
    )
    return {
        "box_rects": data.box_rects,
        "alpha_frames": data.alpha_frames,
        "fps": fps,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Concatenate round clips into one video")
    parser.add_argument("folder", help="Folder containing batch-*.mp4 files")
    parser.add_argument("--output", "-o", help="Output path (default: folder/combined.mp4)")
    parser.add_argument("--width", type=int, default=2560, help="Target width")
    parser.add_argument("--height", type=int, default=1440, help="Target height")
    parser.add_argument(
        "--scaling-mode",
        default="Stretched",
        help="Ignored for encode (always stretch to target). Kept for backlog/CLI compat.",
    )
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        default=False,
        help="Allow non-contiguous round ranges (for --skip-failed-rounds). "
             "Gaps in round numbers are logged but do not abort.",
    )
    parser.add_argument(
        "--voice-shade-demo",
        default=None,
        help="Demo path used to compute the voice-activity shade. When set, the "
             "shade is composited at native res BEFORE the upscale so it stretches "
             "together with the video (boxes stay locked to avatars).",
    )
    parser.add_argument(
        "--voice-shade-steam-id",
        default=None,
        help="POV player steam64 (required with --voice-shade-demo).",
    )
    parser.add_argument(
        "--voice-shade-fade",
        type=float,
        default=0.3,
        help="Shade fade in/out seconds (default 0.3).",
    )
    parser.add_argument(
        "--voice-shade-side",
        default="right",
        help="Which side is the POV team's avatar block (left/right, default right).",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"[ERROR] Folder not found: {folder}")
        sys.exit(1)

    combined = folder / "combined.mp4"
    has_clips = any(folder.glob("batch-*.mp4")) or any(folder.glob("round-*.mp4"))
    shade = _build_shade_for_scale(args, folder)
    if combined.exists() and combined.stat().st_size >= 1_000_000 and not has_clips:
        vid_w, vid_h = _get_resolution(combined)
        if (vid_w, vid_h) == (args.width, args.height):
            print(f"  [Skip] combined.mp4 already {vid_w}x{vid_h}, no clips left")
            mb = combined.stat().st_size / 1024 / 1024
            print(f"\nDone. {combined} ({mb:.0f} MB)")
            return
        print(f"  [Resume scale] combined.mp4 is {vid_w}x{vid_h}, scaling to "
              f"{args.width}x{args.height}...")
        scaled = folder / "_scaled.mp4"
        for p in folder.glob("_scaled*"):
            if not _is_valid_video(p):
                p.unlink(missing_ok=True)
        if not scaled.exists():
            _encode_scaled(combined, scaled, args.width, args.height, args.scaling_mode,
                           shade=shade)
        scaled.replace(combined)
        mb = combined.stat().st_size / 1024 / 1024
        print(f"\nDone. {combined} ({mb:.0f} MB)")
        return

    try:
        combined = concat_rounds(folder, allow_gaps=args.allow_gaps)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # One encode: stretch/pillarbox + upscale to final 16:9 size (no 1080p middle).
    shade = _build_shade_for_scale(args, folder)
    vid_w, vid_h = _get_resolution(combined)
    if (vid_w, vid_h) == (args.width, args.height):
        print(f"\n  [Skip scale] Already {vid_w}x{vid_h}")
    else:
        scaled = folder / "_scaled.mp4"
        if scaled.exists() and not _is_valid_video(scaled):
            print(f"\n  [Cleanup] Removing corrupt {scaled.name}")
            scaled.unlink()
        if not scaled.exists():
            _encode_scaled(combined, scaled, args.width, args.height, args.scaling_mode,
                           shade=shade)
        scaled.replace(combined)

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        combined.replace(output)

    mb = combined.stat().st_size / 1024 / 1024
    print(f"\nDone. {combined} ({mb:.0f} MB)")


if __name__ == "__main__":
    main()
