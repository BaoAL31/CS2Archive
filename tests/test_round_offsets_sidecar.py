"""Regression tests for round_offsets sidecar validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from concat_rounds import validate_round_offsets_sidecar  # noqa: E402


def _good_sidecar() -> dict:
    # 20 rounds @ 100s + 2 rounds @ 105s = 2200s total
    offsets = {str(i): float((i - 1) * 100) for i in range(1, 21)}
    offsets["21"] = 2000.0
    offsets["22"] = 2100.0
    return {
        "total_rounds": 22,
        "total_duration_seconds": 2200.0,
        "round_offsets": offsets,
        "batches": [
            {"batch": "batch-001-020.mp4", "round_start": 1, "round_end": 20,
             "duration_seconds": 2000.0},
            {"batch": "batch-021-022.mp4", "round_start": 21, "round_end": 22,
             "duration_seconds": 200.0},
        ],
    }


def test_good_sidecar_passes() -> None:
    errs = validate_round_offsets_sidecar(
        _good_sidecar(), video_duration_seconds=2200.0,
    )
    assert errs == [], errs


def test_twistzz_corrupt_sidecar_fails() -> None:
    """Exact failure mode from Twistzz Cache: batch2 probed as combined."""
    # Real video ~2202s; sidecar claimed 4195s with r22 at 3093s.
    data = {
        "total_rounds": 22,
        "total_duration_seconds": 4195.018549,
        "round_offsets": {
            **{str(i): round((i - 1) * 99.628, 3) for i in range(1, 22)},
            "22": 3093.79,
        },
        "batches": [
            {"batch": "batch-001-020.mp4", "round_start": 1, "round_end": 20,
             "duration_seconds": 1992.56195},
            {"batch": "batch-021-022.mp4", "round_start": 21, "round_end": 22,
             "duration_seconds": 2202.456599},
        ],
    }
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=2202.456599)
    assert errs, "expected validation errors for corrupt sidecar"
    joined = " | ".join(errs)
    assert "does not match video" in joined, errs
    assert "past video end" in joined, errs


def test_batch_sum_mismatch_fails() -> None:
    data = _good_sidecar()
    data["batches"][1]["duration_seconds"] = 999.0
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=2200.0)
    assert any("sum(batch durations)" in e for e in errs), errs


def test_non_monotonic_offsets_fail() -> None:
    data = _good_sidecar()
    data["round_offsets"]["5"] = 50.0  # before r4
    errs = validate_round_offsets_sidecar(data, video_duration_seconds=2200.0)
    assert any("not monotonic" in e for e in errs), errs


def test_real_corrected_twistzz_sidecar_passes() -> None:
    path = Path(
        r"D:\Projects\CS2Archive\youtube"
        r"\2395491_parivision-vs-faze-m1-cache_Twistzz_Cache"
        r"\video.round_offsets.json"
    )
    if not path.is_file():
        print("SKIP: corrected Twistzz sidecar not on disk")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    errs = validate_round_offsets_sidecar(
        data, video_duration_seconds=float(data["total_duration_seconds"]),
    )
    assert errs == [], errs


if __name__ == "__main__":
    test_good_sidecar_passes()
    test_twistzz_corrupt_sidecar_fails()
    test_batch_sum_mismatch_fails()
    test_non_monotonic_offsets_fail()
    test_real_corrected_twistzz_sidecar_passes()
    print("PASS")
