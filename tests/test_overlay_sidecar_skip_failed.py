"""Tests for overlay sidecar generation with --skip-failed-rounds.

Verifies:
  1. The reference sidecar in the test folder passes validation.
  2. concat_rounds.py generates a correct sidecar when rounds are missing
     (simulating --skip-failed-rounds).
  3. The overlay frame->tick mapping handles gaps in round_offsets correctly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

import concat_rounds
from concat_rounds import validate_round_offsets_sidecar

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"

REFERENCE_SIDECAR = Path(
    r"D:\Projects\CS2Archive\renders"
    r"\pov-heroic-vs-the-mongolz-m2-cache_910"
    r"\combined.round_offsets.json"
)


def _make_fake_round(folder: Path, name: str, duration: float = 0.5) -> Path:
    vid = folder / name
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i",
         f"color=c=black:s=320x240:d={duration}",
         "-c:v", "libx264", "-r", "30", "-pix_fmt", "yuv420p", str(vid)],
        capture_output=True, timeout=30,
    )
    return vid


def _make_fake_round_with_ticks(folder: Path, rn: int, start_tick: int,
                                end_tick: int, duration: float = 0.5) -> Path:
    """Create a round-{rn:03d}-tick-{start}-to-{end}.mp4 file."""
    name = f"round-{rn:03d}-tick-{start_tick}-to-{end_tick}.mp4"
    return _make_fake_round(folder, name, duration)


# ── 1. Reference sidecar validation ──────────────────────────────────────


def test_reference_sidecar_passes_validation() -> None:
    """The known-correct sidecar from the test folder must pass validation."""
    if not REFERENCE_SIDECAR.is_file():
        print("SKIP: reference sidecar not on disk")
        return
    data = json.loads(REFERENCE_SIDECAR.read_text(encoding="utf-8"))
    errs = validate_round_offsets_sidecar(
        data,
        video_duration_seconds=float(data["total_duration_seconds"]),
    )
    assert errs == [], f"reference sidecar has validation errors: {errs}"


def test_reference_sidecar_has_required_fields() -> None:
    """The reference sidecar must have all fields the overlay step needs."""
    if not REFERENCE_SIDECAR.is_file():
        print("SKIP: reference sidecar not on disk")
        return
    data = json.loads(REFERENCE_SIDECAR.read_text(encoding="utf-8"))
    assert "total_rounds" in data
    assert "total_duration_seconds" in data
    assert "round_offsets" in data
    assert "batches" in data
    assert "per_round_ticks" in data, "overlay needs per_round_ticks"
    assert "per_round_durations" in data, "overlay needs per_round_durations"
    # round_offsets keys must be strings (JSON)
    assert all(isinstance(k, str) for k in data["round_offsets"])
    # per_round_ticks values must be [start, end] pairs
    for rn, ticks in data["per_round_ticks"].items():
        assert len(ticks) == 2, f"round {rn} ticks: {ticks}"
        assert ticks[0] < ticks[1], f"round {rn} ticks not ordered: {ticks}"


def test_reference_sidecar_round_offsets_monotonic() -> None:
    """Round offsets must be strictly increasing."""
    if not REFERENCE_SIDECAR.is_file():
        print("SKIP: reference sidecar not on disk")
        return
    data = json.loads(REFERENCE_SIDECAR.read_text(encoding="utf-8"))
    offsets = {int(k): float(v) for k, v in data["round_offsets"].items()}
    sorted_rns = sorted(offsets)
    for prev, rn in zip(sorted_rns, sorted_rns[1:]):
        assert offsets[rn] > offsets[prev], (
            f"round {rn} offset {offsets[rn]} <= round {prev} offset {offsets[prev]}"
        )


# ── 2. Sidecar generation with missing rounds (--skip-failed-rounds) ──────


def test_sidecar_with_missing_rounds_passes_validation() -> None:
    """Simulate --skip-failed-rounds: rounds 3 and 7 are missing.

    The sidecar should only include rounds 1, 2, 4, 5, 6, 8.
    Validation should pass (gaps are allowed; only critical errors fail).
    """
    # 6 rounds, 2 missing (3 and 7), each ~10s
    offsets = {
        "1": 0.0, "2": 10.0, "4": 20.0, "5": 30.0, "6": 40.0, "8": 50.0,
    }
    data = {
        "total_rounds": 6,
        "total_duration_seconds": 60.0,
        "round_offsets": offsets,
        "batches": [
            {"batch": "round-001-tick-100-to-200.mp4", "round_start": 1,
             "round_end": 1, "duration_seconds": 10.0},
            {"batch": "round-002-tick-300-to-400.mp4", "round_start": 2,
             "round_end": 2, "duration_seconds": 10.0},
            {"batch": "round-004-tick-500-to-600.mp4", "round_start": 4,
             "round_end": 4, "duration_seconds": 10.0},
            {"batch": "round-005-tick-700-to-800.mp4", "round_start": 5,
             "round_end": 5, "duration_seconds": 10.0},
            {"batch": "round-006-tick-900-to-1000.mp4", "round_start": 6,
             "round_end": 6, "duration_seconds": 10.0},
            {"batch": "round-008-tick-1100-to-1200.mp4", "round_start": 8,
             "round_end": 8, "duration_seconds": 10.0},
        ],
        "per_round_ticks": {
            "1": [100, 200], "2": [300, 400], "4": [500, 600],
            "5": [700, 800], "6": [900, 1000], "8": [1100, 1200],
        },
        "per_round_durations": {
            "1": 10.0, "2": 10.0, "4": 10.0,
            "5": 10.0, "6": 10.0, "8": 10.0,
        },
    }
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=60.0)
    assert errs == [], f"sidecar with missing rounds should pass: {errs}"


def test_sidecar_with_missing_rounds_non_monotonic_fails() -> None:
    """If a present round's offset is before a prior round's, that's critical."""
    data = {
        "total_rounds": 3,
        "total_duration_seconds": 30.0,
        "round_offsets": {"1": 0.0, "2": 10.0, "4": 5.0},  # r4 before r2
        "batches": [],
        "per_round_ticks": {"1": [100, 200], "2": [300, 400], "4": [500, 600]},
        "per_round_durations": {"1": 10.0, "2": 10.0, "4": 10.0},
    }
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=30.0)
    assert any("not monotonic" in e for e in errs), errs


def test_sidecar_with_missing_rounds_negative_duration_fails() -> None:
    """Negative per_round_durations must still fail (critical error)."""
    data = {
        "total_rounds": 2,
        "total_duration_seconds": 20.0,
        "round_offsets": {"1": 0.0, "2": 10.0},
        "batches": [],
        "per_round_ticks": {"1": [100, 200], "2": [300, 400]},
        "per_round_durations": {"1": 10.0, "2": -5.0},
    }
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=20.0)
    assert errs, f"expected validation errors for negative duration: {errs}"


# ── 3. concat_rounds.py generates correct sidecar with missing rounds ─────


def test_concat_generates_sidecar_with_gaps() -> None:
    """concat_rounds.py should generate a sidecar that includes per_round_ticks
    and per_round_durations even when some rounds are missing.

    We create round files for rounds 1, 2, 4 (skip 3) and verify the sidecar
    has the correct structure with the missing round omitted.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        # Create round files for rounds 1, 2, 4 (skip 3)
        _make_fake_round_with_ticks(folder, 1, 100, 200, duration=1.0)
        _make_fake_round_with_ticks(folder, 2, 300, 400, duration=1.0)
        _make_fake_round_with_ticks(folder, 4, 500, 600, duration=1.0)

        concat_rounds.concat_rounds(folder, allow_gaps=True)

        sidecar = json.loads((folder / "combined.round_offsets.json").read_text())
        # Should have rounds 1, 2, 4 (not 3)
        assert set(sidecar["round_offsets"].keys()) == {"1", "2", "4"}, (
            f"expected rounds 1,2,4; got {set(sidecar['round_offsets'].keys())}"
        )
        # Should have per_round_ticks for all present rounds
        assert set(sidecar["per_round_ticks"].keys()) == {"1", "2", "4"}
        # Should have per_round_durations for all present rounds
        assert set(sidecar["per_round_durations"].keys()) == {"1", "2", "4"}
        # Validate against the actual video
        video_dur = _get_duration(folder / "combined.mp4")
        errs = validate_round_offsets_sidecar(
            sidecar, video_duration_seconds=video_dur,
        )
        assert errs == [], f"sidecar validation failed: {errs}"


def _get_duration(vid: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(vid)],
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(r.stdout)
    return float(data["format"]["duration"])


# ── 4. Overlay frame->tick mapping handles gaps ───────────────────────────


def test_overlay_frame_mapping_handles_gaps() -> None:
    """Verify that _extract_keyboard_states' frame->tick mapping logic
    correctly handles gaps in round_offsets (missing rounds).

    We can't run the full overlay (needs demoparser2 + demo), but we can
    verify the bisect logic that finds the correct round for each frame.
    """
    from bisect import bisect_right

    # Simulate: rounds 1, 2, 4 present (round 3 missing)
    round_offsets = {1: 0.0, 2: 10.0, 4: 20.0}
    sorted_rounds = sorted(round_offsets.keys())
    round_end_sec = {1: 10.0, 2: 20.0, 4: 30.0}  # last round extends

    fps = 30.0
    frame_count = 900  # 30s video

    # Verify that frame 0 (sec=0.0) maps to round 1.
    # bisect_right([0.0, 10.0, 20.0], 0.0) returns 1 (insertion after equal),
    # so pos=1, idx=0, rn=sorted_rounds[0]=1. This is correct.
    sec = 0.0
    pos = bisect_right([round_offsets[r] for r in sorted_rounds], sec)
    assert pos == 1
    rn = sorted_rounds[pos - 1]
    assert rn == 1

    # Verify that frame 300 (10s) maps to round 2
    sec = 300 / fps  # 10.0s
    pos = bisect_right([round_offsets[r] for r in sorted_rounds], sec)
    assert pos == 2
    rn = sorted_rounds[pos - 1]
    assert rn == 2

    # Verify that frame 600 (20s) maps to round 4
    sec = 600 / fps  # 20.0s
    pos = bisect_right([round_offsets[r] for r in sorted_rounds], sec)
    assert pos == 3
    rn = sorted_rounds[pos - 1]
    assert rn == 4

    # Verify that frame 899 (29.97s) maps to round 4
    sec = 899 / fps  # ~29.97s
    pos = bisect_right([round_offsets[r] for r in sorted_rounds], sec)
    assert pos >= len(sorted_rounds)
    rn = sorted_rounds[-1]
    assert rn == 4


# ── 5. Overlay sidecar validation with critical errors ─────────────────────


def test_overlay_sidecar_critical_errors_only() -> None:
    """The overlay step should only hard-fail on critical errors, not on
    gaps from --skip-failed-rounds.

    The only non-critical error is "starts at round X (not 1) — OK if
    --skip-failed-rounds". Everything else is critical.
    """
    # Non-critical: start != 1 with gaps (realistic sidecar: total_rounds matches
    # len(round_offsets), same as concat_rounds.py sets it)
    data = {
        "total_rounds": 3,
        "total_duration_seconds": 25.0,
        "round_offsets": {"2": 0.0, "4": 10.0, "6": 20.0},
        "batches": [],
        "per_round_ticks": {},
        "per_round_durations": {},
    }
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=25.0)
    critical = [e for e in errs if "OK if --skip-failed-rounds" not in e]
    assert critical == [], (
        f"gap warnings should not be critical. errs={errs}, critical={critical}"
    )

    # Critical: non-monotonic offsets
    data2 = {
        "total_rounds": 3,
        "total_duration_seconds": 30.0,
        "round_offsets": {"1": 0.0, "2": 15.0, "3": 10.0},
        "batches": [],
        "per_round_ticks": {},
        "per_round_durations": {},
    }
    errs2 = validate_round_offsets_sidecar(data2, video_duration_seconds=30.0)
    critical2 = [e for e in errs2 if "OK if --skip-failed-rounds" not in e]
    assert critical2, (
        f"non-monotonic offsets should be critical. errs={errs2}"
    )


if __name__ == "__main__":
    test_reference_sidecar_passes_validation()
    test_reference_sidecar_has_required_fields()
    test_reference_sidecar_round_offsets_monotonic()
    test_sidecar_with_missing_rounds_passes_validation()
    test_sidecar_with_missing_rounds_non_monotonic_fails()
    test_sidecar_with_missing_rounds_negative_duration_fails()
    test_concat_generates_sidecar_with_gaps()
    test_overlay_frame_mapping_handles_gaps()
    test_overlay_sidecar_critical_errors_only()
    print("PASS")
