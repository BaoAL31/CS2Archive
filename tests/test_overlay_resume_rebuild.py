"""A look change must not be absorbed by run_overlay's own resume.

``overlay_pov.run_overlay`` short-circuits on an existing
``.overlay_work/video.overlay.mp4`` via ``_resume_overlay_output`` BEFORE any
batch stamp is checked. Without an explicit rebuild that path remuxes the
source audio into the OLD picture and hands it back as if it were the new look,
so the pipeline's look fingerprint never gets a chance to matter.

These tests drive the REAL ``run_overlay`` control flow and the REAL
``_resume_overlay_output``. Only the GPU encode and the CS2/CS2UtilArchive
dependencies are stubbed — the resume decision under test is not mocked.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cs2archive.overlay.overlay_pov as op
from cs2archive.pov.pipeline import Pipeline, _overlay_inputs_hash, _overlay_look


def _digest(path: Path) -> str:
    """Hash a file instead of comparing bytes.

    pytest's assertion rewriting builds a difflib diff of two byte strings on
    failure, which is pathologically slow for multi-hundred-KB videos.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _width(path: Path) -> int:
    """Probe the video width — distinguishes the rebuilt picture from the stale one."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr[-300:]
    return int(r.stdout.strip().splitlines()[0])


def _make_av(path: Path, *, size: str = "640x480", duration: float = 6.0) -> None:
    """A real A/V file comfortably over the 100 KB overlay-validity floor."""
    r = subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=s={size}:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:d={duration}",
         "-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(path)],
        capture_output=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert path.stat().st_size > 100_000


def _write_sidecar(work_dir: Path, duration: float = 6.0) -> None:
    (work_dir / "video.round_offsets.json").write_text(json.dumps({
        "total_rounds": 1,
        "total_duration_seconds": duration,
        "round_offsets": {"1": 0.0},
        "per_round_durations": {"1": duration},
        # 64 tickrate: duration * 64 ticks, so the mapping validates.
        "per_round_ticks": {"1": [0, int(duration * 64)]},
    }), encoding="utf-8")


@pytest.fixture
def overlay_work(tmp_path):
    """A real .overlay_work dir: raw input + an existing final overlay."""
    work = tmp_path / ".overlay_work"
    work.mkdir()
    source = tmp_path / "source.mp4"
    _make_av(source)
    # What a rebuild produces — deliberately a DIFFERENT picture so a stale
    # composite can be told apart from a freshly rendered one.
    rebuilt = tmp_path / "rebuilt.mp4"
    _make_av(rebuilt, size="320x240")

    video_path = work / "video.mp4"
    shutil.copyfile(source, video_path)
    _write_sidecar(work)

    # The composite a previous run left behind, in the OLD look.
    final = work / "video.overlay.mp4"
    shutil.copyfile(source, final)
    assert final.stat().st_size > 100_000

    demo = tmp_path / "demo.dem"
    demo.write_bytes(b"not a real demo")
    return SimpleNamespace(work=work, source=source, rebuilt=rebuilt,
                           video=video_path, final=final, demo=demo)


@pytest.fixture
def stub_heavy(monkeypatch, overlay_work):
    """Stub the GPU encode + CS2 deps; leave the resume logic real."""
    calls = {"encode": [], "concat": []}

    def fake_encode(main_input, extra_inputs, fc_args, out_label, output_path,
                    **_kw):
        calls["encode"].append(Path(output_path))
        shutil.copyfile(overlay_work.rebuilt, output_path)

    def fake_concat(batch_files, output_path, source_audio=None):
        calls["concat"].append(Path(output_path))
        # The real concat muxes the source audio in the same pass; the stub
        # copies a file that already carries audio, so the pipeline's
        # _require_overlay_audio check still sees a valid composite.
        shutil.copyfile(overlay_work.rebuilt, output_path)

    monkeypatch.setattr(op, "_ffmpeg_encode", fake_encode)
    monkeypatch.setattr(op, "_concat_overlay_batches", fake_concat)
    monkeypatch.setattr(op, "_render_throw_flight_clips", lambda *a, **k: [])
    monkeypatch.setattr(op, "_load_player_throws", lambda *a, **k: [])
    # demoparser2 panics on a non-demo fixture; round windows are irrelevant
    # to the resume decision under test.
    monkeypatch.setattr(op, "_load_round_tick_ranges", lambda *a, **k: {})
    return calls


def _run(overlay_work, *, force_rebuild: bool):
    return op.run_overlay(
        overlay_work.video,
        overlay_work.demo,
        "76561198041683378",
        None,
        1,                      # batches
        util_cams_root=overlay_work.work / "utility_cams",
        work_dir=overlay_work.work,
        keyboard=False,
        freeze=False,
        force_rebuild=force_rebuild,
    )


# ── run_overlay resume semantics ─────────────────────────────────────────

def test_existing_final_overlay_is_resumed_by_default(overlay_work, stub_heavy, capsys):
    """Baseline: without a rebuild the existing composite is reused as-is.

    The resume path still rewrites the file (it re-muxes the source audio), so
    the assertion is on the work it skips, not on the bytes.
    """
    _run(overlay_work, force_rebuild=False)

    out = capsys.readouterr().out
    assert "Overlay video exists; source audio restored" in out
    assert stub_heavy["encode"] == []
    assert stub_heavy["concat"] == []


def test_force_rebuild_ignores_the_existing_final_overlay(overlay_work, stub_heavy, capsys):
    """The fix: force_rebuild must not take the resume short-circuit."""
    stale = _digest(overlay_work.final)

    _run(overlay_work, force_rebuild=True)

    out = capsys.readouterr().out
    assert "forced rebuild" in out
    assert "Overlay video exists; source audio restored" not in out
    # It actually re-encoded and re-concatenated instead of remuxing the old one.
    assert stub_heavy["encode"], "expected the batch to be re-encoded"
    assert stub_heavy["concat"], "expected the batches to be re-concatenated"
    # The composite is now the rebuilt picture, not the stale one.
    assert _digest(overlay_work.final) != stale


# ── step_overlay end to end ──────────────────────────────────────────────

def _step_pipeline(tmp_path, overlay_work, look, recorded_look):
    p = Pipeline.__new__(Pipeline)
    p.skip_overlay = False
    p.youtube_dir = tmp_path / "upload"
    p.youtube_dir.mkdir(parents=True, exist_ok=True)
    p.args = look
    p.meta = {}
    p.state = {"data": {"steam_id": "76561198041683378"}}
    p.map_name = "de_mirage"
    p.player = "testplayer"
    p.demo_path = overlay_work.demo
    p.steam_id = "76561198041683378"
    p._voice_enabled = lambda: False
    p._check_voice_capture = lambda _s: None
    # The work dir already holds the raw input AND a stale final overlay.
    p.render_dir = overlay_work.work.parent
    shutil.copyfile(overlay_work.source, p.render_dir / "combined.mp4")
    shutil.copyfile(overlay_work.work / "video.round_offsets.json",
                    p.render_dir / "combined.round_offsets.json")
    (p.youtube_dir / ".overlay_look.json").write_text(
        json.dumps(recorded_look, sort_keys=True), encoding="utf-8")
    return p


def test_step_overlay_look_change_rebuilds_over_existing_work_and_final(
        tmp_path, overlay_work, stub_heavy, monkeypatch, capsys):
    """Existing work input AND existing final overlay must both be rebuilt.

    This is the parent finding: ``_resume_overlay_output`` would otherwise
    remux audio into the stale composite and the pipeline would stamp it with
    the requested look.
    """
    monkeypatch.setattr(
        "cs2archive.overlay.overlay_utilcams._ensure_cs2util_data",
        lambda *_a, **_k: None)

    old_look = _overlay_look(SimpleNamespace(
        overlay_style="broadcast", overlay_anim="instant",
        keyboard=False, freeze=False, straightforward_filter=False))
    new_args = SimpleNamespace(
        overlay_style="ghost", overlay_anim="instant",
        keyboard=False, freeze=False, straightforward_filter=False)

    stale = _digest(overlay_work.final)
    p = _step_pipeline(tmp_path, overlay_work, new_args, old_look)

    p.step_overlay()

    out = capsys.readouterr().out
    assert "overlay look changed" in out
    assert "forced rebuild" in out
    assert "Overlay video exists; source audio restored" not in out
    # The stale composite was not promoted: the upload is the rebuilt picture
    # (320x240), not the stale one (640x480). Audio is re-muxed afterwards, so
    # the bytes differ from the stub's output — the geometry is the stable
    # signal.
    uploaded = p.youtube_dir / "video.mp4"
    assert _digest(uploaded) != stale
    assert _width(uploaded) == 320
    # ...and the recorded look is the one actually rendered.
    recorded = json.loads((p.youtube_dir / ".overlay_look.json").read_text(encoding="utf-8"))
    assert recorded == _overlay_look(new_args)


def test_step_overlay_matching_look_still_resumes(
        tmp_path, overlay_work, stub_heavy, monkeypatch, capsys):
    """Control: an unchanged look keeps the cheap resume path."""
    monkeypatch.setattr(
        "cs2archive.overlay.overlay_utilcams._ensure_cs2util_data",
        lambda *_a, **_k: None)

    look = _overlay_look(SimpleNamespace(
        overlay_style="auto", overlay_anim="instant",
        keyboard=False, freeze=False, straightforward_filter=False))
    args = SimpleNamespace(
        overlay_style="auto", overlay_anim="instant",
        keyboard=False, freeze=False, straightforward_filter=False)

    p = _step_pipeline(tmp_path, overlay_work, args, look)
    # A youtube video already exists in this look, so the fast path applies.
    shutil.copyfile(overlay_work.source, p.youtube_dir / "video.mp4")
    (p.youtube_dir / ".overlay_inputs.sha256").write_text(
        _overlay_inputs_hash(p.render_dir), encoding="utf-8")

    p.step_overlay()

    out = capsys.readouterr().out
    assert "[skip] Overlay video already exists" in out
    assert "forced rebuild" not in out
    assert stub_heavy["encode"] == []
