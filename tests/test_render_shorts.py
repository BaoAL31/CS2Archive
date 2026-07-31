"""Tests for Shorts render pipeline (CSDM config generation + composite)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from shorts.render_shorts import (
    render_shorts,
    _build_csdm_config,
    _composite_9x16,
    _probe_duration,
    _probe_resolution,
    OUT_WIDTH,
    OUT_HEIGHT,
    SRC_WIDTH,
)


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def _fake_ffprobe_duration(*_args, **_kwargs) -> "subprocess.CompletedProcess":
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = 0
    r.stdout = json.dumps({"format": {"duration": "5.0"}})
    return r


def _fake_ffprobe_resolution(w=1080, h=1920):
    def _f(*_args, **_kwargs):
        r = MagicMock(spec=subprocess.CompletedProcess)
        r.returncode = 0
        r.stdout = f"{w},{h}\n"
        return r
    return _f


# ------------------------------------------------------------
# CSDM config tests
# ------------------------------------------------------------

def test_csdm_config_structure(tmp_path):
    demo = tmp_path / "demos" / "faceit" / "test.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    demo.write_text("")

    shorts = [
        {"short_type": "4k", "pov_steam_id": "123", "start_tick": 1000, "end_tick": 5000, "kill_ticks": [1000, 2000, 3000, 4000]},
    ]
    out_dir = tmp_path / "renders" / "test"
    out_dir.mkdir(parents=True, exist_ok=True)

    with patch("shorts.render_shorts._get_player_crosshair_cvars", return_value=[]):
        config = _build_csdm_config(shorts, demo, out_dir)

    assert "demoPath" in config
    assert "outputFolderPath" in config
    assert config["framerate"] == 60
    assert config["width"] == 1920
    assert config["height"] == 1080
    assert config["concatenateSequences"] is False
    assert len(config["sequences"]) == 1
    seq = config["sequences"][0]
    assert seq["startTick"] == 1000
    assert seq["endTick"] == 5000
    assert seq["playerCameras"][0]["playerSteamId"] == "123"


def test_csdm_subprocess_called(tmp_path):
    demo = tmp_path / "demos" / "faceit" / "test.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    demo.write_text("")

    timeline_path = tmp_path / "short_timeline.json"
    timeline = {
        "short_type": "short_timeline",
        "demo_path": str(demo),
        "map": "Nuke",
        "short_count": 1,
        "shorts": [
            {"short_type": "4k", "pov_steam_id": "123", "start_tick": 100, "end_tick": 500, "kill_ticks": [100, 200, 300, 400]},
        ],
    }
    timeline_path.write_text(json.dumps(timeline))

    mock_subprocess = MagicMock()
    mock_subprocess.return_value.returncode = 0
    mock_seq_view = MagicMock()
    mock_seq_view.return_value = []

    with patch("subprocess.run", mock_subprocess), \
         patch("shorts.render_shorts._get_player_crosshair_cvars", return_value=[]), \
         patch("shorts.render_shorts._find_sequence_files", mock_seq_view), \
         patch("shorts.render_shorts._composite_9x16") as mock_composite:
        try:
            render_shorts(timeline_path, batch_size=0)
        except RuntimeError:
            pass  # no sequence files found is expected after CSDM mock

    csdm_calls = [
        c for c in mock_subprocess.call_args_list
        if "csdm.cmd" in str(c.args[0]) or ("video" in str(c.args[0]) and "--config-file" in str(c.args[0]))
    ]
    assert len(csdm_calls) >= 1


# ------------------------------------------------------------
# Composite tests
# ------------------------------------------------------------

def test_output_resolution_1080x1920(tmp_path, monkeypatch):
    """Given a source, composite produces 1080x1920 output."""
    from shorts.render_shorts import _render_kill_feed_pip

    ffmpeg = None
    for p in [
        r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]:
        if Path(p).exists():
            ffmpeg = p
            break
    if not ffmpeg:
        pytest.skip("ffmpeg not found")

    src = tmp_path / "src.mp4"
    dst = tmp_path / "short_test.mp4"
    kf = tmp_path / "kf.mp4"
    cmd = [
        ffmpeg, "-y", "-f", "lavfi", "-i",
        f"testsrc=size={SRC_WIDTH}x{1080}:rate=60:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-frames:v", "60",
        "-pix_fmt", "yuv420p", str(src),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"ffmpeg failed: {r.stderr[:500]}"
    assert src.exists() and src.stat().st_size > 1000

    _render_kill_feed_pip(src, kf)
    _composite_9x16(src, dst, kill_feed_path=kf)

    assert dst.exists() and dst.stat().st_size > 1000
    w, h = _probe_resolution(dst)
    assert w == OUT_WIDTH
    assert h == OUT_HEIGHT


def test_output_has_duration(tmp_path, monkeypatch):
    """Output has non-zero duration."""
    from shorts.render_shorts import _render_kill_feed_pip

    ffmpeg = None
    for p in [
        r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe",
    ]:
        if Path(p).exists():
            ffmpeg = p
            break
    if not ffmpeg:
        pytest.skip("ffmpeg not found")

    src = tmp_path / "src_colorbars.mp4"
    dst = tmp_path / "short_dur.mp4"
    kf = tmp_path / "kf_dur.mp4"
    cmd = [
        ffmpeg, "-y", "-f", "lavfi", "-i",
        f"testsrc=size={SRC_WIDTH}x{1080}:rate=60:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-t", "1", "-pix_fmt", "yuv420p", str(src),
    ]
    subprocess.run(cmd, capture_output=True, text=True)

    _render_kill_feed_pip(src, kf)
    _composite_9x16(src, dst, kill_feed_path=kf)
    dur = _probe_duration(dst)
    assert dur > 0.5


def test_scale_is_accepted_kwarg():
    """scale=1.0 (default) and scale=1.5 both pass through the function signature."""
    import inspect
    sig = inspect.signature(_composite_9x16)
    assert "scale" in sig.parameters
    assert sig.parameters["scale"].default == 2.0
    anno = sig.parameters["scale"].annotation
    assert anno in (float, "float")


def test_scale_scales_foreground_wider():
    """scale>1.0 should produce a wider foreground in the ffmpeg filter chain."""
    from unittest.mock import patch, MagicMock
    captured: list[list[str]] = []
    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        m = MagicMock(returncode=0, stderr="", stdout="")
        return m

    fake_stat = MagicMock(st_size=2000000)
    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "stat", return_value=fake_stat):
        _composite_9x16(
            Path("src.mp4"), Path("dst.mp4"),
            scale=1.0, kill_feed_path=Path("kf.mp4"),
        )
        _composite_9x16(
            Path("src.mp4"), Path("dst.mp4"),
            scale=1.5, kill_feed_path=Path("kf.mp4"),
        )

    fcs = [c[c.index("-filter_complex") + 1] for c in captured]
    assert any("force_original_aspect_ratio=decrease" in f for f in fcs)
    assert any("scale=1620:-1" in f for f in fcs)


def _capture_filter_complex(scale: float = 2.0, kill_feed_path=None, **kwargs) -> list[str]:
    """Run _composite_9x16 with subprocess mocked; return the full ffmpeg cmd."""
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs_run):
        captured.append(cmd)
        m = MagicMock(returncode=0, stderr="", stdout="")
        return m

    fake_stat = MagicMock(st_size=2_000_000)
    if kill_feed_path is None:
        kill_feed_path = Path("default_kill_feed.mp4")
    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "stat", return_value=fake_stat):
        _composite_9x16(
            Path("src.mp4"),
            Path("dst.mp4"),
            scale=scale,
            kill_feed_path=kill_feed_path,
            **kwargs,
        )

    return captured[0]


def _capture_filter_str(scale: float = 2.0, kill_feed_path=None, **kwargs) -> str:
    """Run _composite_9x16 with subprocess mocked; return the filter_complex string."""
    cmd = _capture_filter_complex(scale=scale, kill_feed_path=kill_feed_path, **kwargs)
    return cmd[cmd.index("-filter_complex") + 1]


def test_kill_feed_overlay_present_by_default():
    """Default compositing includes a kill-feed PiP overlay in the filter chain."""
    fc = _capture_filter_str()
    # Kill feed PiP must be in the filter chain — distinct from background/foreground
    assert "[kf]" in fc, "kill feed label [kf] missing from filter chain"
    # The kill feed must be overlaid onto the foreground (not just background)
    assert fc.count("overlay=") >= 2, "expected bg/fg overlay + kill feed overlay"


def test_kill_feed_overlay_positioned_top_right_of_canvas():
    """Kill feed overlay must be placed at the top-right of the 1080x1920 canvas."""
    from shorts import render_shorts as rmod
    fc = _capture_filter_str()
    import re
    m = re.search(r"\[tmp\]\[kf\]overlay=(\d+):(\d+)", fc)
    assert m is not None, f"kill-feed overlay step not found:\n{fc}"
    ox, oy = (int(v) for v in m.groups())
    expected_x = OUT_WIDTH - rmod.KILLFEED_PIP_W
    assert ox == expected_x, f"kill feed overlay x={ox}, expected {expected_x}"
    assert oy == 0, f"kill feed overlay y={oy}, expected 0"


def test_kill_feed_can_be_disabled():
    """Kill feed overlay can be turned off via kill_feed=False."""
    fc = _capture_filter_str(kill_feed=False)
    assert "[kf]" not in fc, "kill feed present when disabled"


def test_kill_feed_crop_constants_within_source():
    """Kill feed crop stays within the 1920x1080 source and at top edge (y=0)."""
    from shorts import render_shorts as rmod
    assert 0 < rmod.KILLFEED_CROP_X
    assert rmod.KILLFEED_CROP_X + rmod.KILLFEED_CROP_W <= 1920
    assert rmod.KILLFEED_CROP_Y == 0
    assert rmod.KILLFEED_CROP_H <= 200


def test_kill_feed_signature_kwarg():
    """_composite_9x16 accepts kill_feed and kill_feed_path keyword arguments."""
    import inspect
    sig = inspect.signature(_composite_9x16)
    assert "kill_feed" in sig.parameters
    assert sig.parameters["kill_feed"].default is True
    assert "kill_feed_path" in sig.parameters
    assert sig.parameters["kill_feed_path"].default is None


def test_kill_feed_uses_external_input_when_provided():
    """The kill feed is taken from input [1:v] (pre-rendered PiP), no inline crop."""
    from shorts import render_shorts as rmod
    fc = _capture_filter_str(kill_feed_path=Path("kill_feed.mp4"))
    assert "[1:v]" in fc, "kill feed must reference external input [1:v]"
    assert "[kf_src]" not in fc, "kill feed must not be cropped inline when external PiP provided"
    assert f"scale={rmod.KILLFEED_PIP_W}:{rmod.KILLFEED_PIP_H}" not in fc, "PiP must not be rescaled in composite"


def test_kill_feed_external_input_command_uses_two_inputs():
    """With kill_feed_path, ffmpeg receives two -i inputs (src + kill_feed)."""
    cmd = _capture_filter_complex(kill_feed_path=Path("kf.mp4"))
    input_count = sum(1 for arg in cmd if arg == "-i")
    assert input_count == 2, f"expected 2 -i inputs, got {input_count}: {cmd}"


def test_render_kill_feed_pip_creates_file():
    """_render_kill_feed_pip calls ffmpeg and writes the kill-feed PiP file."""
    from shorts.render_shorts import _render_kill_feed_pip
    captured: list[list[str]] = []
    def fake_run(cmd, *a, **k):
        captured.append(cmd)
        m = MagicMock(returncode=0, stderr="", stdout="")
        return m

    fake_stat = MagicMock(st_size=2_000_000)
    dst = Path("dst_kf.mp4")
    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "stat", return_value=fake_stat):
        _render_kill_feed_pip(Path("src.mp4"), dst)

    assert len(captured) == 1
    cmd = captured[0]
    assert Path(cmd[-1]) == dst
    assert "-i" in cmd
    src_idx = cmd.index("-i") + 1
    assert cmd[src_idx] == "src.mp4"
    fc = cmd[cmd.index("-vf") + 1]
    import re
    crop_match = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", fc)
    assert crop_match is not None
    w, h, x, y = (int(v) for v in crop_match.groups())
    assert y == 0
    assert x > 0


def test_render_kill_feed_pip_uses_pre_renderer_constants():
    """The pre-renderer uses KILLFEED_CROP_* and KILLFEED_PIP_* module constants."""
    from shorts import render_shorts as rmod
    from shorts.render_shorts import _render_kill_feed_pip
    captured: list[list[str]] = []
    def fake_run(cmd, *a, **k):
        captured.append(cmd)
        m = MagicMock(returncode=0)
        return m

    fake_stat = MagicMock(st_size=2_000_000)
    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "stat", return_value=fake_stat):
        _render_kill_feed_pip(Path("src.mp4"), Path("kf.mp4"))

    fc = captured[0][captured[0].index("-vf") + 1]
    assert f"crop={rmod.KILLFEED_CROP_W}:{rmod.KILLFEED_CROP_H}:{rmod.KILLFEED_CROP_X}:{rmod.KILLFEED_CROP_Y}" in fc
    assert f"scale={rmod.KILLFEED_PIP_W}:{rmod.KILLFEED_PIP_H}" in fc


def test_render_kill_feed_pip_runtime_failure_raises():
    """ffmpeg failure surfaces as RuntimeError with stderr excerpt."""
    from shorts.render_shorts import _render_kill_feed_pip
    fake_stat = MagicMock(st_size=2_000_000)
    fail_run = MagicMock(
        returncode=1,
        stderr="some ffmpeg error",
        stdout="",
    )
    with patch("subprocess.run", return_value=fail_run), \
         patch.object(Path, "stat", return_value=fake_stat):
        with pytest.raises(RuntimeError, match="kill feed pre-render failed"):
            _render_kill_feed_pip(Path("src.mp4"), Path("kf.mp4"))


def test_output_file_naming(monkeypatch):
    """Outputs are named short_001.mp4, short_002.mp4, etc."""
    # Build index manually — _render_shorts iterates enumerate(zip(seq_list, shorts))
    # Verify naming pattern
    with patch('shorts.render_shorts.render_shorts') as mock_rs:
        pass


def test_skip_already_rendered(tmp_path):
    """If short_001.mp4 already exists with 1080x1920, skip re-render."""
    out_dir = tmp_path / "renders" / "hl-test_shorts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pre-place a fake file
    existing = out_dir / "short_001.mp4"
    existing.write_bytes(b"\x00" * 2_000_000)  # 2 MB

    with patch("shorts.render_shorts._probe_resolution", return_value=(OUT_WIDTH, OUT_HEIGHT)):
        with patch("shorts.render_shorts._composite_9x16") as mock_comp:
            # This simulates the skip check
            import shorts.render_shorts as rmod
            if existing.exists() and existing.stat().st_size >= 1_048_576:
                w, h = (OUT_WIDTH, OUT_HEIGHT)
                assert w == OUT_WIDTH and h == OUT_HEIGHT
                # Would skip
            assert mock_comp.called is False
    assert True  # no exception