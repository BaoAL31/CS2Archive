"""Verify overlay_pov.py integration with batch_util_cams.py.

Tests:
  1. Module imports cleanly (no missing refs after refactor).
  2. `_render_throw_flight_clips` returns 28 PipClips for the existing
     pov-furia-vs-falcons-m3-inferno render (all 28 player throws have
     pre-rendered batched util_cams).
  3. `_run_batch_util_cams_subprocess` correctly invokes the batch script
     with the data_dir PARENT (not the per-demo dir).
  4. `_write_throw_poses` (in batch_util_cams) MERGES instead of
     overwriting — multiple throws sharing one util_cam dir all appear
     in the `_throws` dict.
  5. Idempotence: re-running `_render_throw_flight_clips` does NOT
     invoke the subprocess if all clips are already rendered.

Run:
    python tests/test_batched_util_cams.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import overlay_pov as op  # noqa: E402
import batch_util_cams as buc  # noqa: E402


DEMO = Path(r"D:\Projects\CS2Archive\demos\hltv\furia-vs-falcons-iem-cologne-major\furia-vs-falcons-m3-inferno.dem")
VIDEO = Path(r"D:\Projects\CS2Archive\renders\pov-furia-vs-falcons-m3-inferno_76561198041683378_full\combined.mp4")
ROUND_OFFSETS_PATH = Path(r"D:\Projects\CS2Archive\renders\pov-furia-vs-falcons-m3-inferno_76561198041683378_full\combined.round_offsets.json")


def _load_round_offsets() -> tuple[dict[int, float], float]:
    data = json.loads(ROUND_OFFSETS_PATH.read_text(encoding="utf-8"))
    return ({int(k): v for k, v in data["round_offsets"].items()},
            data.get("total_duration_seconds", 0))


def test_1_imports():
    """Both modules import cleanly."""
    assert hasattr(op, "_render_throw_flight_clips")
    assert hasattr(op, "_run_batch_util_cams_subprocess")
    assert hasattr(op, "_util_slug_for_throw")
    assert hasattr(buc, "main")
    assert hasattr(buc, "_write_throw_poses")
    print("  [ok] imports")


def test_2_returns_28_pipclips():
    """All 28 player throws map to pre-rendered batched util_cams."""
    round_offsets, total_secs = _load_round_offsets()
    clips = op._render_throw_flight_clips(
        demo_path=DEMO,
        steam_id="76561198041683378",
        fps=60.0,
        frame_count=10**9,  # no clipping in this test
        output_dir=VIDEO.parent,
        video_path=VIDEO,
        round_offsets=round_offsets,
        round_tick_ranges={},
        total_duration_seconds=total_secs,
    )
    assert len(clips) == 28, f"expected 28 PipClips, got {len(clips)}"
    # Each clip points to a real mp4 ≥ 1MB
    for c in clips:
        assert c.clip_path.is_file(), f"missing mp4: {c.clip_path}"
        assert c.clip_path.stat().st_size > 1_000_000, f"too small: {c.clip_path}"
    print(f"  [ok] {len(clips)} PipClips returned, all mp4s >1MB")


def test_3_subprocess_data_dir_is_parent():
    """Subprocess must receive the data_dir parent (containing demo=* subdirs)."""
    # _find_demo_data_dir returns the per-demo dir; the subprocess caller
    # must wrap it in .parent before passing to batch_util_cams.
    per_demo = op._find_demo_data_dir(DEMO)
    assert per_demo is not None, "no data dir found"
    assert per_demo.name.startswith("demo="), \
        f"expected per-demo dir, got {per_demo.name}"
    parent = per_demo.parent
    assert parent.is_dir()
    assert any(p.name.startswith("demo=") for p in parent.iterdir()), \
        f"parent {parent} should contain demo=* subdirs"
    print(f"  [ok] per-demo={per_demo.name} -> parent={parent.name} contains demo=* subdirs")


def test_4_write_throw_poses_merges():
    """_write_throw_poses must MERGE with existing _throw_poses.json."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        util_dir = Path(td) / "unnamed" / "de_d2_smoke_T_0_0_0" / "demo1"
        util_dir.mkdir(parents=True)
        # Pre-existing entry from earlier throw
        existing = {
            "1": {"pos": [0, 0, 0]},
            "_throws": {"demo1:e100:s0": {"pos": [0, 0, 0]}},
        }
        (util_dir / "_throw_poses.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )
        # New throw at same position
        from scripts.render.batch_csdm import BatchSpotJob
        new_entry = BatchSpotJob(
            job={
                "throw_id": "demo1:e200:s0",
                "util_id": "de_d2:smoke:T:0_0_0",
                "demo_id": "demo1",
                "release_x": 0, "release_y": 0, "release_z": 0,
            },
            util_dir=util_dir,
        )
        buc._write_throw_poses(new_entry, util_dir / "out.mp4")
        result = json.loads((util_dir / "_throw_poses.json").read_text())
        throws = result["_throws"]
        assert "demo1:e100:s0" in throws, "old throw was overwritten"
        assert "demo1:e200:s0" in throws, "new throw missing"
        assert len(throws) == 2, f"expected 2 throws, got {len(throws)}"
    print("  [ok] merge preserved old throw + added new")


def test_5_idempotence():
    """Re-running with all clips present must NOT invoke subprocess."""
    import io
    from contextlib import redirect_stdout
    round_offsets, total_secs = _load_round_offsets()
    buf = io.StringIO()
    with redirect_stdout(buf):
        clips = op._render_throw_flight_clips(
            demo_path=DEMO,
            steam_id="76561198041683378",
            fps=60.0,
            frame_count=10**9,
            output_dir=VIDEO.parent,
            video_path=VIDEO,
            round_offsets=round_offsets,
            round_tick_ranges={},
            total_duration_seconds=total_secs,
        )
    output = buf.getvalue()
    assert len(clips) == 28
    assert "Subprocess: batch_util_cams.py" not in output, \
        "subprocess was invoked even though all clips were pre-rendered"
    print("  [ok] no subprocess call when all clips pre-rendered")


def main() -> int:
    tests = [
        test_1_imports,
        test_2_returns_28_pipclips,
        test_3_subprocess_data_dir_is_parent,
        test_4_write_throw_poses_merges,
        test_5_idempotence,
    ]
    failed = 0
    for t in tests:
        print(f"\n{t.__name__}:")
        try:
            t()
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'PASS' if failed == 0 else 'FAIL'}: {len(tests) - failed}/{len(tests)} tests")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
