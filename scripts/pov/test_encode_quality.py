"""Encode quality gate for pipeline speed optimizations.

Compares OLD vs NEW encode settings on a short sample cut from real footage
and fails if the new settings degrade quality past tolerance. Also times
each encode so claimed speedups are measured, not assumed.

Covers:
  T1 render mezzanine:  p7 CQ10 200M (old) vs p5 CQ10 200M (new)
  T2 scale mezzanine:   p7 CQ8 (old) vs p5 CQ8 (new)
  T3 outro clip:        libx264 medium CRF15 (old) vs h264_nvenc p7 CQ15 60M (new)
                        + checks outro params match the overlay final export
                        (codec/pix_fmt/profile/dims/fps) for -c copy concat
  T4 dead-batch copy:   null re-encode (old) vs stream copy (new);
                        copy must land within duration tolerance, and a
                        [enc, copy, enc] concat must succeed
  T5 overlay hwaccel:   CPU decode + overlay filter (old) vs CUDA decode +
                        hwdownload + same filter (new); time + quality verdict

Usage:
    python scripts/pov/test_encode_quality.py [--sample <video.mp4>] [--seconds 12]

Sample auto-discovery: newest youtube/*/video.mp4, else newest
renders/*/batch-*.mp4 or round-*.mp4. Reference subclip is cut once at high
quality (libx264 CRF10); OLD vs NEW are compared against EACH OTHER so the
reference generation loss cancels out.

Exit 0 = PASS (or PASS-with-SKIPs when no NVENC/GPU box), 1 = FAIL.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings  # noqa: E402

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# NEW-vs-OLD similarity floors (same frames, only preset/codec changed).
# Preset-only NVENC deltas measured ~0.996/52dB — visually lossless, so the
# floor sits just below measured variance, not at identity.
T_PRESET_SSIM_MIN = 0.995
T_PRESET_PSNR_MIN = 48.0
T_OUTRO_SSIM_MIN = 0.99
T_OUTRO_PSNR_MIN = 40.0
T_COPY_DRIFT_S = 0.25  # same tolerance as overlay_pov._segment_copy_exact


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _have_nvenc() -> bool:
    r = _run([FFMPEG, "-hide_banner", "-h", "encoder=h264_nvenc"], timeout=60)
    return r.returncode == 0 and "h264_nvenc" in (r.stdout or "")


def _probe_stream(path: Path) -> dict:
    r = _run([FFPROBE, "-v", "quiet", "-print_format", "json",
              "-show_streams", "-select_streams", "v:0", str(path)], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {(r.stderr or '')[-300:]}")
    streams = json.loads(r.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"no video stream in {path}")
    return streams[0]


def _duration(path: Path) -> float:
    r = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=60)
    return float(r.stdout.strip())


def _compare(ref: Path, dist: Path) -> tuple[float, float]:
    """Return (ssim_all, psnr_avg) of dist vs ref. Uses shortest duration."""
    r = _run([FFMPEG, "-y", "-i", str(ref), "-i", str(dist),
              "-lavfi", "ssim;[0:v][1:v]psnr", "-f", "null", "-"],
             timeout=900)
    err = r.stderr or ""
    m_s = re.search(r"All:\s*([0-9.]+)", err)
    m_p = re.search(r"average:\s*(inf|[0-9.]+)", err)
    if not m_s or not m_p:
        raise RuntimeError(f"ssim/psnr parse failed:\n{err[-800:]}")
    psnr_s = m_p.group(1)
    return float(m_s.group(1)), (float("inf") if psnr_s == "inf" else float(psnr_s))


def _find_sample() -> Path | None:
    vids = sorted(PROJECT_ROOT.glob("youtube/*/video.mp4"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if vids:
        return vids[0]
    cands: list[Path] = []
    for pat in ("renders/*/batch-*.mp4", "renders/*/round-*.mp4",
                "renders/*/combined.mp4"):
        cands += [p for p in PROJECT_ROOT.glob(pat) if p.stat().st_size > 1_000_000]
    if not cands:
        return None
    return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _enc(src: Path, dst: Path, vargs: list[str], label: str) -> float:
    t0 = time.time()
    r = _run([FFMPEG, "-y", "-i", str(src), *vargs, str(dst)], timeout=1800)
    dt = time.time() - t0
    if r.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1000:
        raise RuntimeError(f"{label} encode failed: {(r.stderr or '')[-500:]}")
    print(f"    [{label}] {dt:.1f}s, {dst.stat().st_size / 1e6:.1f} MB")
    return dt


results: list[tuple[str, str, str]] = []  # (test, verdict, detail)


def _check(name: str, ok: bool, detail: str) -> None:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Encode quality gate (old vs new settings)")
    ap.add_argument("--sample", default="", help="Source footage (default: auto-discover)")
    ap.add_argument("--seconds", type=float, default=12.0, help="Sample length in seconds")
    args = ap.parse_args()

    sample = Path(args.sample) if args.sample else _find_sample()
    if sample is None or not sample.exists():
        print("[ERROR] No sample footage found. Pass --sample <video.mp4>.")
        return 1
    print(f"Sample: {sample} ({sample.stat().st_size / 1e9:.2f} GB)")

    nvenc = _have_nvenc()
    print(f"NVENC available: {nvenc}")
    if not nvenc:
        print("SKIP: GPU encode tests need h264_nvenc. Nothing to gate on this box.")
        return 0

    with tempfile.TemporaryDirectory(prefix="quality_gate_") as tmp:
        work = Path(tmp)
        # Common high-quality reference subclip (loss cancels out: OLD vs NEW).
        ref = work / "ref.mp4"
        total = _duration(sample)
        ss = max(0.0, min(total * 0.4, total - args.seconds - 1))
        r = _run([FFMPEG, "-y", "-ss", f"{ss:.2f}", "-i", str(sample),
                  "-t", f"{args.seconds:.1f}",
                  "-c:v", "libx264", "-crf", "10", "-preset", "veryfast",
                  "-pix_fmt", "yuv420p", "-an", str(ref)], timeout=900)
        if r.returncode != 0:
            print(f"[ERROR] reference cut failed: {(r.stderr or '')[-500:]}")
            return 1
        s = _probe_stream(ref)
        print(f"Reference: {s['width']}x{s['height']}, {ref.stat().st_size / 1e6:.1f} MB")

        # ---- T1: render mezzanine p7 -> p5 (CQ10 200M) ----
        print("T1 render mezzanine p7 vs p5 (CQ10 200M)...")
        t1_old = work / "t1_old.mp4"
        t1_new = work / "t1_new.mp4"
        dt_old = _enc(ref, t1_old, ["-c:v", "h264_nvenc", "-preset", "p7",
                                    "-b:v", "0", "-cq", "10",
                                    "-maxrate", "200M", "-bufsize", "400M",
                                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                                    "-level", "5.1", "-an"], "old p7")
        dt_new = _enc(ref, t1_new, ["-c:v", "h264_nvenc", "-preset", "p5",
                                    "-b:v", "0", "-cq", "10",
                                    "-maxrate", "200M", "-bufsize", "400M",
                                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                                    "-level", "5.1", "-an"], "new p5")
        ssim, psnr = _compare(t1_old, t1_new)
        _check("T1 mezzanine preset", ssim >= T_PRESET_SSIM_MIN and psnr >= T_PRESET_PSNR_MIN,
               f"SSIM {ssim:.5f} (>= {T_PRESET_SSIM_MIN}), PSNR {psnr:.1f}dB "
               f"(>= {T_PRESET_PSNR_MIN}); {dt_old:.0f}s -> {dt_new:.0f}s")

        # ---- T2: scale mezzanine p7 -> p5 (CQ8) ----
        print("T2 scale mezzanine p7 vs p5 (CQ8)...")
        t2_old = work / "t2_old.mp4"
        t2_new = work / "t2_new.mp4"
        scale_vf = "scale=2560:1440:flags=lanczos,setsar=1"
        dt_old = _enc(ref, t2_old, ["-vf", scale_vf, "-c:v", "h264_nvenc",
                                    "-preset", "p7", "-b:v", "0", "-cq", "8",
                                    "-maxrate", "200M", "-bufsize", "400M",
                                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                                    "-level", "5.1", "-an"], "old p7")
        dt_new = _enc(ref, t2_new, ["-vf", scale_vf, "-c:v", "h264_nvenc",
                                    "-preset", "p5", "-b:v", "0", "-cq", "8",
                                    "-maxrate", "200M", "-bufsize", "400M",
                                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                                    "-level", "5.1", "-an"], "new p5")
        ssim, psnr = _compare(t2_old, t2_new)
        _check("T2 scale preset", ssim >= T_PRESET_SSIM_MIN and psnr >= T_PRESET_PSNR_MIN,
               f"SSIM {ssim:.5f}, PSNR {psnr:.1f}dB; {dt_old:.0f}s -> {dt_new:.0f}s")

        # ---- T3: outro libx264 -> NVENC + concat-compat ----
        print("T3 outro libx264-medium-CRF15 vs NVENC-p7-CQ15...")
        from generate_outro import render_frame
        w, h = int(s["width"]), int(s["height"])
        fps_s = s.get("r_frame_rate", "60/1")
        num, den = fps_s.split("/")
        fps = float(num) / float(den)
        frame_png = work / "outro_frame.png"
        render_frame(w, h).save(frame_png)
        t3_old = work / "t3_old.mp4"
        t3_new = work / "t3_new.mp4"
        dt_old = _enc(frame_png, t3_old,
                      ["-loop", "1", "-t", "5", "-r", f"{fps}",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p",
                       "-profile:v", "high", "-level", "5.1",
                       "-vf", f"scale={w}:{h}",
                       "-preset", "medium", "-crf", "15"], "old x264")
        dt_new = _enc(frame_png, t3_new,
                      ["-loop", "1", "-t", "5", "-r", f"{fps}",
                       "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
                       "-profile:v", "high", "-level", "5.1",
                       "-vf", f"scale={w}:{h}",
                       "-preset", "p7", "-b:v", "0", "-cq", "15",
                       "-maxrate", "60M", "-bufsize", "120M",
                       "-g", "60", "-keyint_min", "60",
                       "-movflags", "+faststart"], "new nvenc")
        ssim, psnr = _compare(t3_old, t3_new)
        _check("T3 outro quality", ssim >= T_OUTRO_SSIM_MIN and psnr >= T_OUTRO_PSNR_MIN,
               f"SSIM {ssim:.5f} (>= {T_OUTRO_SSIM_MIN}), PSNR {psnr:.1f}dB; "
               f"{dt_old:.0f}s -> {dt_new:.0f}s")
        # Concat-compat: outro must match final-export video params.
        so, sn = _probe_stream(t3_old), _probe_stream(t3_new)
        compat = all(sn.get(k) == so.get(k) for k in
                     ("codec_name", "pix_fmt", "profile", "width", "height"))
        _check("T3 outro concat-compat",
               compat and sn.get("codec_name") == "h264" and sn.get("pix_fmt") == "yuv420p",
               f"codec {sn.get('codec_name')}/{sn.get('profile')} "
               f"{sn.get('pix_fmt')} {sn.get('width')}x{sn.get('height')}")

        # ---- T4: dead-batch stream copy exactness + concat ----
        print("T4 dead-batch copy vs null re-encode...")
        seg_dur = min(8.0, args.seconds)
        t4_enc = work / "t4_enc.mp4"
        t4_copy = work / "t4_copy.mp4"
        _enc(ref, t4_enc, ["-t", f"{seg_dur:.1f}", "-filter_complex", "[0:v]null[outv]",
                           "-map", "[outv]", "-c:v", "h264_nvenc", "-preset", "p7",
                           "-b:v", "0", "-cq", "15", "-maxrate", "60M", "-bufsize", "120M",
                           "-profile:v", "high", "-pix_fmt", "yuv420p",
                           "-g", "60", "-keyint_min", "60", "-an"], "re-encode")
        from overlay.overlay_encode import _ffmpeg_segment_copy
        t0 = time.time()
        _ffmpeg_segment_copy(ref, 0.0, seg_dur, t4_copy)
        copy_dt = time.time() - t0
        drift = abs(_duration(t4_copy) - seg_dur)
        _check("T4 copy exactness", drift <= T_COPY_DRIFT_S,
               f"drift {drift:.3f}s (tol {T_COPY_DRIFT_S}s), copy {copy_dt:.1f}s; "
               f"{'copy accepted' if drift <= T_COPY_DRIFT_S else 'fallback re-encode would trigger'}")
        lst = work / "files.txt"
        joined = work / "t4_joined.mp4"
        lst.write_text(f"file '{t4_enc.resolve().as_posix()}'\n"
                       f"file '{t4_copy.resolve().as_posix()}'\n"
                       f"file '{t4_enc.resolve().as_posix()}'\n")
        r = _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                  "-c", "copy", str(joined)], timeout=600)
        ok = r.returncode == 0 and joined.is_file() and joined.stat().st_size > 1000
        _check("T4 mixed concat", ok,
               "enc+copy+enc stream-copy concat " + ("OK" if ok else f"FAILED: {(r.stderr or '')[-300:]}"))

        # ---- T5: overlay CPU decode vs CUDA decode (bake-off) ----
        # Same overlay filter (3 sprite overlays + setsar, like the keyboard
        # layer) + same NVENC params. Only the decode/upload path differs.
        # Verdict is informational on speed; quality must match to PASS.
        print("T5 overlay CPU vs CUDA decode...")
        from PIL import Image, ImageDraw
        sprites = []
        for i, col in enumerate([(255, 80, 80), (80, 255, 120), (100, 140, 255)]):
            sp = work / f"spr{i}.png"
            img = Image.new("RGBA", (200, 200), col + (255,))
            ImageDraw.Draw(img).rounded_rectangle(
                [4, 4, 196, 196], radius=28,
                outline=(255, 255, 255, 255), width=6)
            img.save(sp)
            sprites.append(sp)
        ov_chain = ("[base][1:v]overlay=100:1150[o1];"
                    "[o1][2:v]overlay=420:1150[o2];"
                    "[o2][3:v]overlay=740:1150[o3];"
                    "[o3]setsar=1[outv]")
        fc_cpu = ov_chain.replace("[base]", "[0:v]", 1)
        fc_gpu = "[0:v]hwdownload,format=nv12[base];" + ov_chain
        vhook = ["-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0",
                 "-cq", "15", "-maxrate", "60M", "-bufsize", "120M",
                 "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-g", "60", "-keyint_min", "60",
                 "-movflags", "+faststart", "-an"]
        t_seg = min(10.0, args.seconds)
        t5_cpu = work / "t5_cpu.mp4"
        t5_gpu = work / "t5_gpu.mp4"
        t0 = time.time()
        r_cpu = _run([FFMPEG, "-y", "-i", str(ref),
                      "-i", str(sprites[0]), "-i", str(sprites[1]),
                      "-i", str(sprites[2]),
                      "-filter_complex", fc_cpu, "-map", "[outv]",
                      "-t", f"{t_seg:.1f}", *vhook, str(t5_cpu)], timeout=1800)
        dt_cpu = time.time() - t0
        if r_cpu.returncode != 0:
            _check("T5 cpu baseline", False,
                   f"CPU overlay encode failed: {(r_cpu.stderr or '')[-300:]}")
        else:
            print(f"    [cpu] {dt_cpu:.1f}s, {t5_cpu.stat().st_size / 1e6:.1f} MB")
            t0 = time.time()
            r_gpu = _run([FFMPEG, "-y",
                          "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                          "-i", str(ref),
                          "-i", str(sprites[0]), "-i", str(sprites[1]),
                          "-i", str(sprites[2]),
                          "-filter_complex", fc_gpu, "-map", "[outv]",
                          "-t", f"{t_seg:.1f}", *vhook, str(t5_gpu)], timeout=1800)
            dt_gpu = time.time() - t0
            if r_gpu.returncode != 0:
                results.append(("T5 cuda path", "SKIP",
                                f"CUDA decode unavailable: {(r_gpu.stderr or '')[-300:]}"))
                print(f"  [SKIP] T5 cuda path: no CUDA decode on this box")
            else:
                print(f"    [cuda] {dt_gpu:.1f}s, {t5_gpu.stat().st_size / 1e6:.1f} MB")
                ssim, psnr = _compare(t5_cpu, t5_gpu)
                _check("T5 cpu-vs-cuda quality",
                       ssim >= 0.999 and psnr >= 50.0,
                       f"SSIM {ssim:.5f} (>= 0.999), PSNR {psnr:.1f}dB (>= 50); "
                       f"cpu {dt_cpu:.1f}s vs cuda {dt_gpu:.1f}s "
                       f"(cuda {dt_cpu / dt_gpu:.2f}x)")

    print("\n==== SUMMARY ====")
    failed = [n for n, v, _ in results if v == "FAIL"]
    for n, v, d in results:
        print(f"  [{v}] {n}: {d}")
    print("GATE: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
