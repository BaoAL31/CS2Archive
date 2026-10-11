"""Exercise offset/rate correction through the real ffmpeg remux path."""
import os
import struct
import subprocess
from pathlib import Path

import pytest

from cs2archive.overlay.overlay_encode import _remux_source_audio


@pytest.mark.parametrize("offset", [0.3, -0.3, 0.0])
def test_remux_aligns_audio_landmarks_and_preserves_video(tmp_path, offset):
    tempo = 1.001
    source = tmp_path / "source.mp4"
    overlay = tmp_path / "overlay.mp4"
    # The source's audible landmarks are offset + tempo * picture_time.
    times = [offset + tempo * t for t in (0.8, 1.8)]
    pulses = "+".join(f"between(t,{t},{t + 0.04})" for t in times)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=size=64x64:rate=30:duration=3", "-f", "lavfi", "-i",
        f"aevalsrc='0.8*sin(2*PI*1000*t)*({pulses})':s=48000:d=3",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(source),
        "-c:v", "copy", "-an", str(overlay),
    ], check=True, capture_output=True)
    def video_bytes(path):
        return subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v",
            "-c", "copy", "-f", "data", "-",
        ], check=True, capture_output=True).stdout
    original_video = video_bytes(overlay)
    _remux_source_audio(overlay, source, tempo=tempo, offset=offset)
    pcm = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(overlay), "-vn", "-ac", "1",
        "-ar", "48000", "-f", "f32le", "-",
    ], check=True, capture_output=True).stdout
    samples = struct.unpack(f"<{len(pcm) // 4}f", pcm)
    for expected in (0.8, 1.8):
        start = int((expected - 0.15) * 48000)
        end = int((expected + 0.15) * 48000)
        onset = next(i for i in range(start, end) if abs(samples[i]) > 0.15)
        assert abs(onset / 48000 - expected) < 0.025
    # Trimming audio must not truncate the video tail through -shortest.
    assert len(samples) / 48000 >= 2.98
    assert video_bytes(overlay) == original_video


def test_tiny_rate_correction_does_not_add_long_form_delay(tmp_path):
    """A +39 ppm correction must advance shots, not add WSOLA drift."""
    source = tmp_path / "source.mp4"
    overlay = tmp_path / "overlay.mp4"
    tempo = 1.000039
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=size=16x16:rate=10:duration=240", "-f", "lavfi", "-i",
        "anoisesrc=sample_rate=48000:duration=240:amplitude=0.015:seed=73",
        "-f", "lavfi", "-i",
        "aevalsrc='0.8*sin(2*PI*1000*t)*between(t,220,220.04)':s=48000:d=240",
        "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "mpeg4", "-c:a", "aac",
        str(source),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(source),
        "-c:v", "copy", "-an", str(overlay),
    ], check=True, capture_output=True)
    _remux_source_audio(overlay, source, tempo=tempo)
    start = 219.5
    pcm = subprocess.run([
        "ffmpeg", "-v", "error", "-ss", str(start), "-i", str(overlay),
        "-t", "1", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-",
    ], check=True, capture_output=True).stdout
    samples = struct.unpack(f"<{len(pcm) // 4}f", pcm)
    onset = start + next(i for i, value in enumerate(samples)
                         if abs(value) > 0.15) / 48000
    assert abs(onset - 220 / tempo) < 0.006, onset


@pytest.mark.skipif(not os.environ.get("CS2ARCHIVE_AUDIO_DRIFT_FIXTURE"),
                    reason="requires retained capture audio fixture")
def test_retained_capture_rate_correction_does_not_add_delay(tmp_path):
    """Real capture reproducer: WSOLA bias is absent from simple pulse audio."""
    np = pytest.importorskip("numpy")
    signal = pytest.importorskip("scipy.signal")
    source = Path(os.environ["CS2ARCHIVE_AUDIO_DRIFT_FIXTURE"])
    overlay = tmp_path / "overlay.mp4"
    tempo = 1.000039
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=size=16x16:rate=10:duration=100", "-c:v", "mpeg4", "-an",
        str(overlay),
    ], check=True, capture_output=True)
    _remux_source_audio(overlay, source, tempo=tempo)

    def audio(path, start, duration):
        pcm = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
            "-t", str(duration), "-vn", "-ac", "1", "-ar", "8000",
            "-f", "f32le", "-",
        ], check=True, capture_output=True).stdout
        return np.frombuffer(pcm, dtype="<f4").astype(float)

    time = 84.2
    reference = audio(source, time, 4)
    candidate = audio(overlay, time - 0.2, 4.4)
    lag = int(np.argmax(signal.correlate(candidate, reference,
                                         mode="valid", method="fft")))
    delay = lag / 8000 - 0.2
    expected = time / tempo - time
    assert abs(delay - expected) < 0.006, (delay, expected)
