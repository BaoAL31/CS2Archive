"""A failed audio remux must never promote video-only output to youtube/."""
import subprocess
from types import SimpleNamespace

import pytest

from cs2archive.pov.pipeline import Pipeline


@pytest.fixture
def silent_video(tmp_path):
    path = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=30:duration=2", "-c:v", "mpeg4", str(path),
    ], capture_output=True, check=True)
    assert path.stat().st_size > 100_000
    return path


def test_video_only_overlay_cannot_be_copied_to_upload(tmp_path, silent_video, capsys):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.skip_overlay = False
    pipeline.youtube_dir = tmp_path / "upload"
    with pytest.raises(SystemExit):
        pipeline._copy_overlay_result_to_youtube(silent_video)
    assert "OVERLAY_AUDIO_MISSING" in capsys.readouterr().out
    assert not pipeline.youtube_dir.exists()


def test_resume_checks_body_audio_even_when_hook_has_audio(tmp_path, silent_video, capsys):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.skip_overlay = False
    pipeline.youtube_dir = tmp_path / "upload"
    pipeline.youtube_dir.mkdir()
    pipeline.render_dir = tmp_path / "purged-render"
    pipeline.args = SimpleNamespace(overlay_style="auto", overlay_anim="instant")
    pipeline._voice_enabled = lambda: False
    pipeline._check_voice_capture = lambda _step: None
    # Existing final is large enough for the resume fast path. The separate
    # body captures the production failure: a hook track cannot hide its loss.
    (pipeline.youtube_dir / "video.mp4").write_bytes(b"v" * 100_001)
    (pipeline.youtube_dir / "video.nohook.mp4").write_bytes(silent_video.read_bytes())
    with pytest.raises(SystemExit):
        pipeline.step_overlay()
    assert "OVERLAY_AUDIO_MISSING" in capsys.readouterr().out
