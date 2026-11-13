"""Single-pass final assembly (hook + intro + POV body + outro).

Covers the two audio policies in ``cs2archive/pov/assemble_final.py`` — the
measured-exact stream-copy join and the per-source audio rebuild — plus the
pipeline's body adoption / assembly-stamp invalidation rules.

The audio-timing tests are SYNTHETIC: they build parts with tone bursts at
known offsets and measure where those bursts land after the join. They prove
the join does not shift audio against the picture; they are not a substitute
for a real-capture check on a full POV.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2archive.pov import assemble_final
from cs2archive.pov.pipeline import Pipeline

FFMPEG = assemble_final.FFMPEG
FFPROBE = assemble_final.FFPROBE

W, H, FPS = 640, 360, 30


def _make_part(
    path: Path,
    duration: float,
    *,
    tone_start: float = 0.0,
    tone_len: float = 0.0,
    freq: int = 1000,
    audio: str = "aac",
    sample_rate: int = 48000,
    with_audio: bool = True,
) -> Path:
    """A tiny clip with a tone burst at a known offset (or no audio at all)."""
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:d={duration}",
    ]
    if with_audio:
        expr = (
            f"if(between(t,{tone_start},{tone_start + tone_len}),"
            f"0.5*sin(2*PI*{freq}*t),0)"
        )
        cmd += [
            "-f", "lavfi", "-i",
            f"aevalsrc='{expr}':s={sample_rate}:d={duration}:c=stereo",
        ]
    cmd += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "30"]
    if with_audio:
        cmd += ["-c:a", audio, "-ar", str(sample_rate), "-ac", "2"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    assert path.stat().st_size > 100_000, "part must clear concat_parts' size gate"
    return path


def _duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip())


def _tone_onsets(path: Path, noise_db: int = -40) -> list[float]:
    """Start times of non-silent stretches in the audio track.

    Trailing onsets at/after the container duration are dropped: the concat
    demuxer pads the last audio packet, and ``silencedetect`` reports that pad
    as one more "silence end" past the end of the picture.
    """
    r = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    total = _duration(path)
    return [t for t in (float(m) for m in re.findall(r"silence_end: ([0-9.]+)", r.stderr))
            if t < total - 0.1]


# ── video_join_compatible ────────────────────────────────────────────────


def test_matching_video_profiles_are_joinable(tmp_path):
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    b = _make_part(tmp_path / "b.mp4", 1.0, tone_start=0.4, tone_len=0.3, freq=2000)
    ok, reason = assemble_final.video_join_compatible([a, b])
    assert ok, reason


def test_a_silent_part_is_still_video_compatible(tmp_path):
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    outro = _make_part(tmp_path / "outro.mp4", 1.0, with_audio=False)
    ok, reason = assemble_final.video_join_compatible([a, outro])
    assert ok, reason


def test_mismatched_video_params_are_rejected_not_repaired(tmp_path):
    """A picture-profile mismatch must fail, never fall back to an audio-only fix."""
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    b = tmp_path / "b.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=size=1280x720:rate={FPS}:d=1", "-c:v", "libx264", "-crf", "18",
         "-pix_fmt", "yuv420p", str(b)],
        capture_output=True, check=True, timeout=120,
    )
    ok, reason = assemble_final.video_join_compatible([a, b])
    assert not ok
    assert "video params" in reason
    with pytest.raises(SystemExit) as exc:
        assemble_final.concat_parts([a, b], tmp_path / "joined.mp4")
    assert "ASSEMBLE_VIDEO_INCOMPATIBLE" in str(exc.value)
    assert not (tmp_path / "joined.mp4").exists()


# ── concat_parts: audio is ALWAYS rebuilt on the picture timeline ────────


def test_audio_is_rebuilt_even_when_every_part_is_house_format(tmp_path, capsys):
    """Matching codec settings are not proof of timing compatibility."""
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    b = _make_part(tmp_path / "b.mp4", 1.0, tone_start=0.4, tone_len=0.3, freq=2000)
    assert assemble_final.probe_audio_params(a) == assemble_final.HOUSE_AUDIO
    assert assemble_final.probe_audio_params(b) == assemble_final.HOUSE_AUDIO
    out = assemble_final.concat_parts([a, b], tmp_path / "joined.mp4")
    assert "audio rebuilt per part" in capsys.readouterr().out
    assert abs(_duration(out) - 2.0) < 0.15
    assert assemble_final.probe_audio_params(out) == assemble_final.HOUSE_AUDIO


def test_audio_does_not_drift_across_five_joins(tmp_path):
    """A per-join encoder delay would be ~100 ms by the fifth join.

    Each part is 2 s with a burst 1 s in, so the authored onsets are
    1, 3, 5, 7, 9 s. One AAC frame is 21.3 ms at 48 kHz, so 30 ms is about
    one frame of slack — not enough to hide accumulated drift.
    """
    parts = [
        _make_part(tmp_path / f"p{i}.mp4", 2.0, tone_start=1.0, tone_len=0.4,
                   freq=1000 + 200 * i)
        for i in range(5)
    ]
    out = assemble_final.concat_parts(parts, tmp_path / "joined.mp4")
    assert abs(_duration(out) - 10.0) < 0.3
    onsets = _tone_onsets(out)
    assert len(onsets) == 5, onsets
    for got, want in zip(onsets, (1.0, 3.0, 5.0, 7.0, 9.0)):
        assert abs(got - want) < 0.03, onsets


def test_audio_stays_on_the_picture_with_a_legacy_mp3_part(tmp_path):
    a = _make_part(tmp_path / "a.mp4", 2.0, tone_start=1.0, tone_len=0.4)
    b = _make_part(tmp_path / "b.mp4", 2.0, tone_start=0.5, tone_len=0.4,
                   freq=2000, audio="libmp3lame", sample_rate=44100)
    out = assemble_final.concat_parts([a, b], tmp_path / "joined.mp4")
    assert abs(_duration(out) - 4.0) < 0.2
    assert assemble_final.probe_audio_params(out) == assemble_final.HOUSE_AUDIO
    onsets = _tone_onsets(out)
    assert len(onsets) == 2, onsets
    assert abs(onsets[0] - 1.0) < 0.03, onsets
    assert abs(onsets[1] - 2.5) < 0.03, onsets


def test_a_silent_part_occupies_its_own_picture_length(tmp_path):
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    silent = _make_part(tmp_path / "silent.mp4", 1.0, with_audio=False)
    b = _make_part(tmp_path / "b.mp4", 1.0, tone_start=0.2, tone_len=0.3,
                   freq=2000, audio="libmp3lame", sample_rate=44100)
    out = assemble_final.concat_parts([a, silent, b], tmp_path / "joined.mp4")
    assert abs(_duration(out) - 3.0) < 0.2
    onsets = _tone_onsets(out)
    assert len(onsets) == 2, onsets
    assert abs(onsets[0] - 0.2) < 0.03, onsets
    assert abs(onsets[1] - 2.2) < 0.03, onsets


def test_unprobeable_part_refuses_the_stream_copy_audio_join(tmp_path, monkeypatch):
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    b = _make_part(tmp_path / "b.mp4", 1.0, tone_start=0.2, tone_len=0.3,
                   audio="libmp3lame", sample_rate=44100)
    monkeypatch.setattr(assemble_final, "probe_picture_duration", lambda _p: 0.0)
    with pytest.raises(SystemExit) as exc:
        assemble_final.concat_parts([a, b], tmp_path / "joined.mp4")
    assert "ASSEMBLE_AUDIO_UNPROBED" in str(exc.value)


def test_empty_part_list_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        assemble_final.concat_parts([], tmp_path / "joined.mp4")


# ── audio probe: confirmed silence vs probe failure ─────────────────────


def test_probe_audio_params_returns_none_only_for_confirmed_silence(tmp_path):
    """A successful probe with no audio stream is the ONLY None case."""
    silent = _make_part(tmp_path / "silent.mp4", 1.0, with_audio=False)
    assert assemble_final.probe_audio_params(silent) is None
    loud = _make_part(tmp_path / "loud.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    assert assemble_final.probe_audio_params(loud) == assemble_final.HOUSE_AUDIO


def test_probe_audio_params_raises_when_ffprobe_cannot_run(tmp_path, monkeypatch):
    """An inspection failure must not be reported as "this part is silent"."""
    part = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    monkeypatch.setattr(assemble_final, "FFPROBE", str(tmp_path / "no-such-ffprobe"))
    with pytest.raises(assemble_final.AudioProbeError):
        assemble_final.probe_audio_params(part)


def test_audio_probe_failure_is_fatal_not_silence(tmp_path, monkeypatch):
    """A failed probe on the body must abort, not mute the delivered video."""
    body = _make_part(tmp_path / "body.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    outro = _make_part(tmp_path / "outro.mp4", 1.0, with_audio=False)
    real = assemble_final.probe_audio_params

    def flaky(path):
        if Path(path).name == "body.mp4":
            raise assemble_final.AudioProbeError("simulated ffprobe timeout")
        return real(path)

    monkeypatch.setattr(assemble_final, "probe_audio_params", flaky)
    out = tmp_path / "joined.mp4"
    with pytest.raises(SystemExit) as exc:
        assemble_final.concat_parts([body, outro], out)
    assert "ASSEMBLE_AUDIO_PROBE_FAILED" in str(exc.value)
    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()


# ── no trailing full-file rewrite ───────────────────────────────────────


def _top_level_atoms(path: Path) -> list[str]:
    import struct

    names: list[str] = []
    total = path.stat().st_size
    pos = 0
    with open(path, "rb") as f:
        while pos < total and len(names) < 4:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = struct.unpack(">I", hdr[:4])[0]
            names.append(hdr[4:8].decode("latin-1"))
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
            if size <= 0:
                break
            pos += size
    return names


def test_join_does_not_rewrite_the_file_to_move_the_moov_atom(tmp_path):
    """``+faststart`` is a full-file rewrite of the ~13 GB output.

    The pre-existing delivered layout has the moov atom at the end (the old
    hook-prepend / outro-append passes never moved it), so the single pass must
    not reintroduce that rewrite.
    """
    a = _make_part(tmp_path / "a.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    b = _make_part(tmp_path / "b.mp4", 1.0, tone_start=0.4, tone_len=0.3, freq=2000)
    out = assemble_final.concat_parts([a, b], tmp_path / "joined.mp4")
    atoms = _top_level_atoms(out)
    assert atoms[0] == "ftyp"
    assert "mdat" in atoms and "moov" in atoms
    assert atoms.index("moov") > atoms.index("mdat"), atoms


# ── pipeline: body adoption + assembly stamp ────────────────────────────


def _pipeline(tmp_path: Path) -> Pipeline:
    p = Pipeline.__new__(Pipeline)
    p.youtube_dir = tmp_path / "youtube"
    p.youtube_dir.mkdir()
    p.render_dir = tmp_path / "renders"
    p.render_dir.mkdir()
    p.args = SimpleNamespace()
    return p


def _write_body(p: Pipeline, seconds: float = 1.0) -> Path:
    body = _make_part(p.youtube_dir / "video.body.mp4", seconds,
                      tone_start=0.2, tone_len=0.3)
    return body


def test_resolve_body_prefers_the_pristine_body_copy(tmp_path):
    p = _pipeline(tmp_path)
    body = _write_body(p)
    (p.youtube_dir / "video.mp4").write_bytes(b"x" * 200_000)
    assert p._resolve_body() == body


def test_resolve_body_adopts_an_unassembled_video(tmp_path):
    p = _pipeline(tmp_path)
    video = _make_part(p.youtube_dir / "video.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    adopted = p._resolve_body()
    assert adopted is not None
    assert adopted.name == "video.body.mp4"
    assert adopted.stat().st_size == video.stat().st_size


def test_resolve_body_refuses_a_video_with_an_outro_marker(tmp_path):
    p = _pipeline(tmp_path)
    _make_part(p.youtube_dir / "video.mp4", 1.0, tone_start=0.2, tone_len=0.3)
    (p.youtube_dir / "video.round_offsets.json").write_text(
        json.dumps({"outro_seconds": 5.0}), encoding="utf-8")
    assert p._resolve_body() is None


def test_resolve_body_refuses_a_video_longer_than_the_raw_body(tmp_path):
    p = _pipeline(tmp_path)
    _make_part(p.youtube_dir / "video.mp4", 3.0, tone_start=0.2, tone_len=0.3)
    (p.render_dir / "combined.round_offsets.json").write_text(
        json.dumps({"total_duration_seconds": 1.0}), encoding="utf-8")
    assert p._resolve_body() is None


def test_assembly_stamp_invalidates_when_a_part_changes(tmp_path):
    p = _pipeline(tmp_path)
    body = _write_body(p)
    outro = _make_part(p.youtube_dir / "outro.mp4", 1.0, with_audio=False)
    parts = [body, outro]
    stamp = p._assembly_stamp(parts, "cfg-1")
    (p.youtube_dir / "video.mp4").write_bytes(b"x" * 200_000)
    (p.youtube_dir / ".assembly.json").write_text(json.dumps(stamp), encoding="utf-8")
    assert p._assembly_matches(stamp)
    # A re-rendered component must force a re-assembly.
    changed = p._assembly_stamp(parts, "cfg-2")
    assert not p._assembly_matches(changed)
    outro.write_bytes(outro.read_bytes() + b"\0")
    assert not p._assembly_matches(p._assembly_stamp(parts, "cfg-1"))


def test_assembly_stamp_requires_a_delivered_video(tmp_path):
    p = _pipeline(tmp_path)
    body = _write_body(p)
    stamp = p._assembly_stamp([body], "")
    (p.youtube_dir / ".assembly.json").write_text(json.dumps(stamp), encoding="utf-8")
    assert not p._assembly_matches(stamp)


def test_assembly_sidecar_is_rebuilt_from_the_body_timeline(tmp_path):
    p = _pipeline(tmp_path)
    body = _write_body(p)
    outro = _make_part(p.youtube_dir / "outro.mp4", 1.0, with_audio=False)
    (p.youtube_dir / "video.body.round_offsets.json").write_text(json.dumps({
        "round_offsets": {"1": 0.0, "2": 0.5},
        "total_duration_seconds": 1.0,
    }), encoding="utf-8")
    (p.youtube_dir / "video.mp4").write_bytes(b"x" * 200_000)
    p._write_assembly_sidecar(body, None, None, outro, "")
    first = json.loads((p.youtube_dir / "video.round_offsets.json").read_text())
    assert first["round_offsets"]["2"] == pytest.approx(0.5)
    assert first["outro_seconds"] == pytest.approx(1.0, abs=0.1)
    # Re-running must not accumulate drift: it rebuilds from the body sidecar.
    p._write_assembly_sidecar(body, None, None, outro, "")
    second = json.loads((p.youtube_dir / "video.round_offsets.json").read_text())
    assert second["round_offsets"] == first["round_offsets"]
    assert second["total_duration_seconds"] == pytest.approx(
        first["total_duration_seconds"])


# ── deferred scale: canvas geometry reproduces the pre-scaled layout ─────


def test_pip_geometry_is_height_driven_so_canvas_matches_prescaled_input():
    from cs2archive.overlay.overlay_pov import _pip_geometry

    for index in range(4):
        canvas = _pip_geometry(index, 2560, 1440)
        prescaled = _pip_geometry(index, 1440, 1440)
        assert canvas == prescaled


# ── step_outro: the real assembly path ──────────────────────────────────


def _assembly_pipeline(tmp_path: Path) -> Pipeline:
    p = _pipeline(tmp_path)
    p.args = SimpleNamespace()
    p.meta = {}
    p.state = {"data": {}}
    return p


def _stub_components(p: Pipeline, monkeypatch, *, hook_cfg: str = "cfg-a"):
    """Replace the three component renderers with pre-made parts."""
    hook = _make_part(p.youtube_dir / "hook.mp4", 2.0, tone_start=0.2, tone_len=0.3)
    intro = _make_part(p.youtube_dir / "intro.mp4", 1.0, tone_start=0.2, tone_len=0.3,
                       freq=1500)
    outro = _make_part(p.youtube_dir / "outro.mp4", 1.0, with_audio=False)
    monkeypatch.setattr(p, "_ensure_hook", lambda _body: (hook, hook_cfg))
    monkeypatch.setattr(p, "_ensure_intro_segment", lambda _body: intro)
    monkeypatch.setattr(p, "_ensure_outro", lambda _body: outro)
    return hook, intro, outro


def test_step_outro_joins_every_component_in_one_pass(tmp_path, monkeypatch):
    p = _assembly_pipeline(tmp_path)
    body = _write_body(p, seconds=2.0)
    (p.youtube_dir / "video.body.round_offsets.json").write_text(json.dumps({
        "round_offsets": {"1": 0.0},
        "total_duration_seconds": 2.0,
    }), encoding="utf-8")
    hook, intro, outro = _stub_components(p, monkeypatch)

    p.step_outro()

    video = p.youtube_dir / "video.mp4"
    assert video.is_file()
    expected = sum(_duration(x) for x in (hook, intro, body, outro))
    assert abs(_duration(video) - expected) < 0.3
    sidecar = json.loads((p.youtube_dir / "video.round_offsets.json").read_text())
    lead = _duration(hook) + _duration(intro)
    assert sidecar["round_offsets"]["1"] == pytest.approx(lead, abs=0.1)
    assert sidecar["hook_seconds"] == pytest.approx(_duration(hook), abs=0.1)
    assert sidecar["intro_seconds"] == pytest.approx(_duration(intro), abs=0.1)
    assert sidecar["outro_seconds"] == pytest.approx(_duration(outro), abs=0.1)
    assert (p.youtube_dir / ".assembly.json").is_file()
    # The body is untouched: the assembly wrote a new file and replaced video.mp4.
    assert body.is_file()


def test_step_outro_skips_when_the_components_are_unchanged(tmp_path, monkeypatch, capsys):
    p = _assembly_pipeline(tmp_path)
    _write_body(p, seconds=2.0)
    (p.youtube_dir / "video.body.round_offsets.json").write_text(json.dumps({
        "round_offsets": {"1": 0.0}, "total_duration_seconds": 2.0,
    }), encoding="utf-8")
    _stub_components(p, monkeypatch)
    p.step_outro()
    capsys.readouterr()
    p.step_outro()
    assert "final assembly already matches its components" in capsys.readouterr().out


def test_step_outro_reassembles_when_the_hook_config_changes(tmp_path, monkeypatch, capsys):
    p = _assembly_pipeline(tmp_path)
    _write_body(p, seconds=2.0)
    (p.youtube_dir / "video.body.round_offsets.json").write_text(json.dumps({
        "round_offsets": {"1": 0.0}, "total_duration_seconds": 2.0,
    }), encoding="utf-8")
    _stub_components(p, monkeypatch, hook_cfg="cfg-a")
    p.step_outro()
    capsys.readouterr()
    _stub_components(p, monkeypatch, hook_cfg="cfg-b")
    p.step_outro()
    out = capsys.readouterr().out
    assert "final assembly already matches" not in out
    assert "Assembled" in out


def test_step_outro_falls_back_to_the_incremental_path_without_a_body(tmp_path, monkeypatch, capsys):
    p = _assembly_pipeline(tmp_path)
    # A delivered video with an outro marker cannot be adopted as the body.
    _make_part(p.youtube_dir / "video.mp4", 2.0, tone_start=0.2, tone_len=0.3)
    (p.youtube_dir / "video.round_offsets.json").write_text(
        json.dumps({"outro_seconds": 1.0}), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(p, "_prepend_intro", lambda *_a, **_k: calls.append("intro"))
    monkeypatch.setattr(p, "_prepend_hook", lambda *_a, **_k: calls.append("hook"))
    monkeypatch.setattr(p, "_append_outro", lambda *_a, **_k: calls.append("outro"))

    p.step_outro()

    assert calls == ["intro", "hook", "outro"]
    assert "no un-assembled body" in capsys.readouterr().out
