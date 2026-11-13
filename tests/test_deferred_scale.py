"""Deferred native -> export upscale (Opt 1).

``concat_rounds --defer-scale`` leaves the mezzanine at capture resolution and
the overlay encode folds the upscale into its own pass. A raw-only run must
never defer: nothing downstream would scale it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2archive.config import settings
from cs2archive.pov.pipeline import Pipeline

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe
NATIVE = (960, 540)
TARGET = (1920, 1080)
DURATION = 5


def _make_batch(folder: Path, size: tuple[int, int] = NATIVE) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "batch-001-001.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=size={size[0]}x{size[1]}:rate=30:d={DURATION}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={DURATION}",
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(clip)],
        capture_output=True, check=True, timeout=120,
    )
    return clip


def _resolution(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def _run_concat(folder: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cs2archive.pov.concat_rounds", str(folder),
         "--width", str(TARGET[0]), "--height", str(TARGET[1]), *extra],
        capture_output=True, text=True, timeout=600,
    )


def test_defer_scale_keeps_the_mezzanine_native(tmp_path):
    folder = tmp_path / "renders"
    _make_batch(folder)
    r = _run_concat(folder, "--defer-scale")
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert "Skip scale" in r.stdout
    assert _resolution(folder / "combined.mp4") == NATIVE


def test_without_defer_scale_the_mezzanine_is_scaled(tmp_path):
    folder = tmp_path / "renders"
    _make_batch(folder)
    r = _run_concat(folder)
    assert "Skip scale" not in r.stdout
    if "GPU not available" in r.stdout:
        # _check_gpu_available is a scheduling hint: it refuses when another
        # ffmpeg holds the CUDA context. Nothing to assert about the output.
        pytest.skip("GPU busy (another ffmpeg holds the CUDA context)")
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert _resolution(folder / "combined.mp4") == TARGET


def test_defer_scale_on_the_resume_path_also_skips_the_scale(tmp_path):
    folder = tmp_path / "renders"
    _make_batch(folder)
    assert _run_concat(folder, "--defer-scale").returncode == 0
    # Second run: combined.mp4 exists, no clips left -> resume branch.
    r = _run_concat(folder, "--defer-scale")
    assert r.returncode == 0, r.stdout[-800:]
    assert "Skip scale" in r.stdout
    assert _resolution(folder / "combined.mp4") == NATIVE


# ── pipeline wiring ─────────────────────────────────────────────────────


def _pipeline(tmp_path: Path, *, skip_overlay: bool) -> Pipeline:
    p = Pipeline.__new__(Pipeline)
    p.skip_overlay = skip_overlay
    p.render_dir = tmp_path / "renders"
    p.render_dir.mkdir(parents=True, exist_ok=True)
    p.youtube_dir = tmp_path / "youtube"
    p.args = SimpleNamespace(width=TARGET[0], height=TARGET[1])
    p.meta = {}
    p.state = {"data": {}}
    return p


def test_step_concat_defers_the_scale_when_an_overlay_will_run(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, skip_overlay=False)
    _make_batch(p.render_dir)
    seen: list[list[str]] = []

    def fake_run_py(args, **_kw):
        seen.append(list(args))
        # Produce the combined.mp4 the step expects, at native resolution.
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc2=size={NATIVE[0]}x{NATIVE[1]}:rate=30:d={DURATION}",
             "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
             str(p.render_dir / "combined.mp4")],
            capture_output=True, check=True, timeout=120,
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(p, "_run_py", fake_run_py)
    monkeypatch.setattr(p, "_check_voice_capture", lambda _s: None)
    monkeypatch.setattr(p, "_validate_concat", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_copy_video_to_youtube", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_copy_round_offsets", lambda *_a, **_k: None)

    p.step_concat()
    assert "--defer-scale" in seen[0]


def test_step_concat_never_defers_for_a_raw_only_run(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, skip_overlay=True)
    _make_batch(p.render_dir)
    seen: list[list[str]] = []

    def fake_run_py(args, **_kw):
        seen.append(list(args))
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc2=size={TARGET[0]}x{TARGET[1]}:rate=30:d={DURATION}",
             "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
             str(p.render_dir / "combined.mp4")],
            capture_output=True, check=True, timeout=120,
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(p, "_run_py", fake_run_py)
    monkeypatch.setattr(p, "_check_voice_capture", lambda _s: None)
    monkeypatch.setattr(p, "_validate_concat", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_copy_video_to_youtube", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_copy_round_offsets", lambda *_a, **_k: None)

    p.step_concat()
    assert "--defer-scale" not in seen[0]


def test_overlay_target_size_reports_the_export_size_for_a_native_mezzanine(tmp_path):
    p = _pipeline(tmp_path, skip_overlay=False)
    native = p.render_dir / "combined.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=size={NATIVE[0]}x{NATIVE[1]}:rate=30:d={DURATION}",
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(native)],
        capture_output=True, check=True, timeout=120,
    )
    assert p._overlay_target_size(native) == TARGET


def test_overlay_target_size_is_none_for_an_already_scaled_mezzanine(tmp_path):
    p = _pipeline(tmp_path, skip_overlay=False)
    scaled = p.render_dir / "combined.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=size={TARGET[0]}x{TARGET[1]}:rate=30:d={DURATION}",
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(scaled)],
        capture_output=True, check=True, timeout=120,
    )
    assert p._overlay_target_size(scaled) is None


def test_overlay_target_size_is_none_when_the_probe_fails(tmp_path):
    p = _pipeline(tmp_path, skip_overlay=False)
    assert p._overlay_target_size(tmp_path / "missing.mp4") is None


@pytest.mark.parametrize("size", [(1440, 1080), (1350, 1080), (1728, 1080)])
def test_overlay_target_size_covers_every_capture_aspect(tmp_path, size):
    p = _pipeline(tmp_path, skip_overlay=False)
    src = p.render_dir / "combined.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=size={size[0]}x{size[1]}:rate=30:d={DURATION}",
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, check=True, timeout=120,
    )
    assert p._overlay_target_size(src) == TARGET
