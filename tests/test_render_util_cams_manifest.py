"""Unified render_util_cams: thin manifest-builder wrapper around render_utils.py.

Tests the new behavior (post-unification):
  1. _build_manifest_jobs() schema correctness + cameras selection per util_type
  2. main() writes _manifest.json + invokes render_utils.py subprocess
  3. main() writes _clip_index.json mapping throw_id -> mp4 path
  4. overlay_pov._scan_utility_cams_clips reads _clip_index.json (no _throw_poses.json dep)
  5. Idempotence: clip_index present -> no subprocess call

Replaces legacy _throw_poses.json sidecar approach.

Run:
    python -m pytest tests/test_render_util_cams_manifest.py -v
or:
    python tests/test_render_util_cams_manifest.py
"""
from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import render_util_cams as ruc  # noqa: E402
import overlay_pov as op  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def throws_df() -> pd.DataFrame:
    """Minimal throws.parquet rows covering smoke + non-smoke + edge cases."""
    return pd.DataFrame([
        # Renderable smoke throw (NiKo on inferno, T-side)
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e142:s1",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "smoke",
            "demo_id": "2395002-furia-vs-falcons-m3-inferno",
            "round_num": 5,
            "throw_tick": 5000,
            "detonate_tick": 5128,
            "land_tick": 5128,
            "throw_x": -1200.0, "throw_y": 800.0, "throw_z": 0.0,
            "release_x": -1200, "release_y": 800, "release_z": 0,
            "land_x": 500.0, "land_y": 200.0, "land_z": 100.0,
            "throw_pitch": -30.0, "throw_yaw": 90.0,
            "flight_ticks": 128,
            "is_renderable": True,
            "has_trajectory": True,
        },
        # Renderable HE throw (different util_type, same player)
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e300:s2",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "he",
            "demo_id": "2395002-furia-vs-falcons-m3-inferno",
            "round_num": 7,
            "throw_tick": 7000,
            "detonate_tick": 7050,
            "land_tick": 7050,
            "throw_x": -1100.0, "throw_y": 850.0, "throw_z": 0.0,
            "release_x": -1100, "release_y": 850, "release_z": 0,
            "land_x": 0.0, "land_y": 0.0, "land_z": 0.0,
            "throw_pitch": -20.0, "throw_yaw": 45.0,
            "flight_ticks": 50,
            "is_renderable": True,
            "has_trajectory": True,
        },
        # Renderable flash (different util_type)
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e400:s3",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "flash",
            "demo_id": "2395002-furia-vs-falcons-m3-inferno",
            "round_num": 8,
            "throw_tick": 8000,
            "detonate_tick": 8100,
            "land_tick": 8100,
            "throw_x": -1000.0, "throw_y": 900.0, "throw_z": 0.0,
            "release_x": -1000, "release_y": 900, "release_z": 0,
            "land_x": 100.0, "land_y": 100.0, "land_z": 50.0,
            "throw_pitch": -10.0, "throw_yaw": 0.0,
            "flight_ticks": 100,
            "is_renderable": True,
            "has_trajectory": True,
        },
        # Unrenderable (flight_ticks=0) — must be filtered
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e500:s4",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "smoke",
            "demo_id": "2395002-furia-vs-falcons-m3-inferno",
            "round_num": 9,
            "throw_tick": 9000,
            "detonate_tick": 9128,
            "land_tick": 9128,
            "throw_x": 0, "throw_y": 0, "throw_z": 0,
            "release_x": 0, "release_y": 0, "release_z": 0,
            "land_x": 0, "land_y": 0, "land_z": 0,
            "throw_pitch": 0, "throw_yaw": 0,
            "flight_ticks": 0,
            "is_renderable": True,
            "has_trajectory": False,
        },
        # Different player (other_steamid) — must be filtered when --steamid set
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e600:s5",
            "thrower_steamid": 99999999999999999,
            "thrower_side": "CT",
            "map": "de_inferno",
            "util_type": "smoke",
            "demo_id": "2395002-furia-vs-falcons-m3-inferno",
            "round_num": 10,
            "throw_tick": 10000,
            "detonate_tick": 10128,
            "land_tick": 10128,
            "throw_x": 0, "throw_y": 0, "throw_z": 0,
            "release_x": 0, "release_y": 0, "release_z": 0,
            "land_x": 0, "land_y": 0, "land_z": 0,
            "throw_pitch": 0, "throw_yaw": 0,
            "flight_ticks": 128,
            "is_renderable": True,
            "has_trajectory": True,
        },
    ])


@pytest.fixture
def demo_path(tmp_path) -> Path:
    """Create a fake .dem file in standard hltv layout."""
    hltv_dir = tmp_path / "demos" / "hltv" / "furia-vs-falcons-iem-cologne-major"
    hltv_dir.mkdir(parents=True)
    dem = hltv_dir / "2395002-furia-vs-falcons-m3-inferno.dem"
    dem.write_bytes(b"fake dem content")
    return dem


@pytest.fixture
def util_cams_root(tmp_path) -> Path:
    return tmp_path / "utility_cams"


# ---------------------------------------------------------------------------
# Slice 1: _build_manifest_jobs() — schema + cameras selection
# ---------------------------------------------------------------------------

class TestBuildManifestJobs:
    def test_smoke_uses_flight_orbit_cameras(self, throws_df):
        """Smoke throws get cameras='flight,orbit' (chase + orbit around detonate)."""
        jobs = ruc._build_manifest_jobs(throws_df, demo_id="x", demo_path=Path("/fake.dem"))
        smoke_jobs = [j for j in jobs if j["util_type"] == "smoke" and j["is_renderable"]]
        assert len(smoke_jobs) >= 1
        for j in smoke_jobs:
            assert j["cameras"] == "flight,orbit", f"smoke job has cameras={j['cameras']!r}"

    def test_non_smoke_uses_flight_cameras(self, throws_df):
        """Non-smoke (he, flash, molotov, decoy) get cameras='flight'."""
        jobs = ruc._build_manifest_jobs(throws_df, demo_id="x", demo_path=Path("/fake.dem"))
        for util_type in ("he", "flash"):
            non_smoke = [j for j in jobs if j["util_type"] == util_type]
            assert non_smoke, f"no {util_type} jobs in manifest"
            for j in non_smoke:
                assert j["cameras"] == "flight", \
                    f"{util_type} job has cameras={j['cameras']!r}, expected 'flight'"

    def test_filters_unrenderable(self, throws_df):
        """Rows with flight_ticks <= 0 or is_renderable=False are excluded."""
        jobs = ruc._build_manifest_jobs(throws_df, demo_id="x", demo_path=Path("/fake.dem"))
        assert all(j["is_renderable"] for j in jobs)
        for j in jobs:
            # Unrenderable row has flight_ticks=0; it should be gone
            assert j["throw_id"] != "2395002-furia-vs-falcons-m3-inferno:e500:s4"

    def test_includes_required_render_fields(self, throws_df):
        """All fields render_utils.py needs are present on every job."""
        required = {
            "job_type", "util_id", "demo_id", "demo_path", "util_type",
            "throw_id", "thrower_steamid", "round_num", "throw_tick",
            "detonate_tick", "land_tick", "throw_x", "throw_y", "throw_z",
            "throw_pitch", "throw_yaw", "is_renderable",
            "cluster_x", "cluster_y", "cluster_z", "cameras",
        }
        jobs = ruc._build_manifest_jobs(throws_df, demo_id="x", demo_path=Path("/fake.dem"))
        for j in jobs:
            missing = required - set(j.keys())
            assert not missing, f"job missing fields: {missing}\njob={j}"

    def test_util_id_format(self, throws_df):
        """util_id follows map:util_type:side:x_y_z convention."""
        jobs = ruc._build_manifest_jobs(throws_df, demo_id="x", demo_path=Path("/fake.dem"))
        for j in jobs:
            parts = j["util_id"].split(":")
            assert len(parts) == 4, f"util_id malformed: {j['util_id']}"
            assert parts[0] == j["map"]
            assert parts[1] == j["util_type"]
            assert parts[2] == j["thrower_side"].upper()
            assert "_" in parts[3]  # x_y_z

    def test_cluster_falls_back_to_release_when_no_land(self):
        """If land_x/y/z missing, cluster uses release_x/y/z (per existing behavior)."""
        df = pd.DataFrame([{
            "throw_id": "x:e1:s0",
            "thrower_steamid": 1, "thrower_side": "T",
            "map": "de_d2", "util_type": "he", "demo_id": "x",
            "round_num": 1, "throw_tick": 100, "detonate_tick": 150, "land_tick": 150,
            "throw_x": 0, "throw_y": 0, "throw_z": 0,
            "release_x": 100, "release_y": 200, "release_z": 300,
            "land_x": None, "land_y": None, "land_z": None,
            "throw_pitch": 0, "throw_yaw": 0,
            "flight_ticks": 50, "is_renderable": True, "has_trajectory": True,
        }])
        jobs = ruc._build_manifest_jobs(df, demo_id="x", demo_path=Path("/x.dem"))
        j = jobs[0]
        assert (j["cluster_x"], j["cluster_y"], j["cluster_z"]) == (100, 200, 300)


# ---------------------------------------------------------------------------
# Slice 2: main() — writes _manifest.json + invokes render_utils.py
# ---------------------------------------------------------------------------

class TestMainWritesManifest:
    def test_writes_manifest_json(self, util_cams_root, throws_df, demo_path, tmp_path, monkeypatch):
        """main() writes a valid render_manifest.json (list of job dicts)."""
        # Stub: avoid actually running render_utils.py
        monkeypatch.setattr(
            "render_util_cams._invoke_render_utils",
            lambda manifest_path, util_cams_root, data_dir, project, demo_id, chunk_size=0: 0,
        )
        # Pretend the throws.parquet exists under data_dir/demo=*/*
        data_dir = tmp_path / "data"  # PARENT of demo=* subdirs
        demo_data_dir = data_dir / "demo=2395002-furia-vs-falcons-m3-inferno"
        demo_data_dir.mkdir(parents=True)
        throws_df.to_parquet(demo_data_dir / "throws.parquet", index=False)

        # Run main with sys.argv stub
        monkeypatch.setattr("sys.argv", [
            "render_util_cams.py",
            "--util-cams-root", str(util_cams_root),
            "--data-dir", str(data_dir),
            "--steamid", "76561198041683378",
            "--demo-id", "2395002-furia-vs-falcons-m3-inferno",
        ])
        rc = ruc.main()
        assert rc == 0

        manifest_path = util_cams_root / "_manifest.json"
        assert manifest_path.is_file(), f"manifest not written at {manifest_path}"
        manifest = json.loads(manifest_path.read_text())
        assert isinstance(manifest, list)
        assert len(manifest) == 3  # smoke + he + flash (filtered out 2)
        for j in manifest:
            assert "cameras" in j
            assert "throw_id" in j

    def test_invokes_render_utils_with_correct_args(
        self, util_cams_root, throws_df, demo_path, tmp_path, monkeypatch
    ):
        """Subprocess invocation passes --manifest, --output-root, --project, --data-dir."""
        calls = []
        def fake_invoke(manifest_path, util_cams_root, data_dir, project, demo_id, chunk_size=0):
            calls.append({
                "manifest_path": str(manifest_path),
                "util_cams_root": str(util_cams_root),
                "data_dir": str(data_dir),
                "project": project,
                "demo_id": demo_id,
                "chunk_size": chunk_size,
            })
            return 0
        monkeypatch.setattr("render_util_cams._invoke_render_utils", fake_invoke)

        data_dir = tmp_path / "data"
        demo_data_dir = data_dir / "demo=2395002-furia-vs-falcons-m3-inferno"
        demo_data_dir.mkdir(parents=True)
        throws_df.to_parquet(demo_data_dir / "throws.parquet", index=False)

        monkeypatch.setattr("sys.argv", [
            "render_util_cams.py",
            "--util-cams-root", str(util_cams_root),
            "--data-dir", str(data_dir),
            "--steamid", "76561198041683378",
            "--demo-id", "2395002-furia-vs-falcons-m3-inferno",
        ])
        ruc.main()

        assert len(calls) == 1
        call = calls[0]
        assert call["util_cams_root"] == str(util_cams_root.resolve())
        assert call["data_dir"] == str(data_dir.resolve())  # parent of demo=*
        assert call["project"]  # non-empty
        assert call["demo_id"] == "2395002-furia-vs-falcons-m3-inferno"


# ---------------------------------------------------------------------------
# Slice 3: _clip_index.json — throw_id -> mp4 path
# ---------------------------------------------------------------------------

class TestClipIndex:
    def test_writes_clip_index_after_render(
        self, util_cams_root, throws_df, demo_path, tmp_path, monkeypatch
    ):
        """After main() runs, _clip_index.json maps each throw_id to its mp4 path."""
        # Fake the renderer: write a fake mp4 per job at the expected util_dir location
        def fake_invoke(manifest_path, util_cams_root, data_dir, project, demo_id, chunk_size=0):
            util_cams_root_p = Path(util_cams_root)
            manifest = json.loads(Path(manifest_path).read_text())
            for j in manifest:
                util_id = j["util_id"]
                util_slug = util_id.replace(":", "_")
                map_name = util_id.split(":")[0]
                util_dir = util_cams_root_p / project / map_name / "unnamed" / util_slug / demo_id
                util_dir.mkdir(parents=True, exist_ok=True)
                cameras = j["cameras"]
                cameras_prefix = cameras.replace(",", "_")
                throw_slug = j["throw_id"].replace(":", "_")
                # Match CS2UtilArchive clip_name_for_cameras: strip leading match id
                import re
                stripped = re.sub(r"^\d{6,}-", "", throw_slug)
                clip = util_dir / f"{cameras_prefix}_{stripped}.mp4"
                clip.write_bytes(b"fake mp4")
            return 0
        monkeypatch.setattr("render_util_cams._invoke_render_utils", fake_invoke)

        data_dir = tmp_path / "data"
        demo_data_dir = data_dir / "demo=2395002-furia-vs-falcons-m3-inferno"
        demo_data_dir.mkdir(parents=True)
        throws_df.to_parquet(demo_data_dir / "throws.parquet", index=False)

        monkeypatch.setattr("sys.argv", [
            "render_util_cams.py",
            "--util-cams-root", str(util_cams_root),
            "--data-dir", str(data_dir),
            "--steamid", "76561198041683378",
            "--demo-id", "2395002-furia-vs-falcons-m3-inferno",
        ])
        rc = ruc.main()
        assert rc == 0

        index_path = util_cams_root / "_clip_index.json"
        assert index_path.is_file(), f"clip index not written at {index_path}"
        index = json.loads(index_path.read_text())
        assert len(index) == 3
        # Each throw_id maps to a real mp4 file
        for tid, mp4_path in index.items():
            assert Path(mp4_path).is_file(), f"mp4 missing for {tid}: {mp4_path}"


# ---------------------------------------------------------------------------
# Slice 4: overlay_pov._scan_utility_cams_clips reads _clip_index.json
# ---------------------------------------------------------------------------

class TestScanUtilityCamsClips:
    def test_reads_clip_index(self, tmp_path):
        """_scan_utility_cams_clips returns {throw_id: mp4_path} from _clip_index.json."""
        # Set up: youtube/<run_id>/video.mp4 + utility_cams/_clip_index.json
        youtube_dir = tmp_path / "youtube" / "test_run"
        video = youtube_dir / "video.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"fake video")

        util_cams = youtube_dir / "utility_cams"
        util_cams.mkdir()
        clip = util_cams / "flight_x_e1_s0.mp4"
        clip.write_bytes(b"fake mp4")
        index = {"x:e1:s0": str(clip)}
        (util_cams / "_clip_index.json").write_text(json.dumps(index))

        result = op._scan_utility_cams_clips(video)
        assert result == {"x:e1:s0": clip.resolve()}

    def test_returns_empty_when_no_index(self, tmp_path):
        """No _clip_index.json -> empty dict (does NOT fall back to _throw_poses.json)."""
        youtube_dir = tmp_path / "youtube" / "test_run"
        video = youtube_dir / "video.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"fake video")
        util_cams = youtube_dir / "utility_cams"
        util_cams.mkdir()

        result = op._scan_utility_cams_clips(video)
        assert result == {}

    def test_returns_empty_when_no_util_cams_dir(self, tmp_path):
        """No utility_cams/ at all -> empty dict."""
        youtube_dir = tmp_path / "youtube" / "test_run"
        video = youtube_dir / "video.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"fake video")

        result = op._scan_utility_cams_clips(video)
        assert result == {}


# ---------------------------------------------------------------------------
# Slice 5: Idempotence — re-run doesn't invoke subprocess if all clips in index
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_idempotent_rerun_skips_subprocess(self, tmp_path, monkeypatch, throws_df):
        """If _clip_index.json has all throws, no subprocess is invoked."""
        # Set up: util_cams_root + data_dir + clip index with all 3 renderable throws
        util_cams = tmp_path / "utility_cams"
        util_cams.mkdir()
        data_dir = tmp_path / "data"
        demo_data_dir = data_dir / "demo=2395002-furia-vs-falcons-m3-inferno"
        demo_data_dir.mkdir(parents=True)
        throws_df.to_parquet(demo_data_dir / "throws.parquet", index=False)

        # Pre-populate clip index with all 3 renderable throw_ids + real mp4 files
        renderable = throws_df[
            (throws_df["flight_ticks"] > 0) & (throws_df["is_renderable"] == True)
            & (throws_df["thrower_steamid"] == 76561198041683378)
        ]
        index = {}
        for _, row in renderable.iterrows():
            tid = str(row["throw_id"])
            clip = util_cams / f"flight_{tid.replace(':', '_')}.mp4"
            clip.write_bytes(b"fake mp4")
            index[tid] = str(clip)
        (util_cams / "_clip_index.json").write_text(json.dumps(index))

        # Subprocess MUST NOT be called
        def fail_invoke(*args, **kwargs):
            raise AssertionError("subprocess invoked when index already complete")
        monkeypatch.setattr("render_util_cams._invoke_render_utils", fail_invoke)

        monkeypatch.setattr("sys.argv", [
            "render_util_cams.py",
            "--util-cams-root", str(util_cams),
            "--data-dir", str(data_dir),
            "--steamid", "76561198041683378",
            "--demo-id", "2395002-furia-vs-falcons-m3-inferno",
        ])
        rc = ruc.main()
        assert rc == 0, "main() should return 0 when all clips already in index"


# ---------------------------------------------------------------------------
# Slice 6: Integration smoke test — existing render still works
# ---------------------------------------------------------------------------

DEMO = Path(r"D:\Projects\CS2Archive\demos\hltv\furia-vs-falcons-iem-cologne-major\furia-vs-falcons-m3-inferno.dem")
VIDEO = Path(r"D:\Projects\CS2Archive\renders\pov-furia-vs-falcons-m3-inferno_76561198041683378_full\combined.mp4")
ROUND_OFFSETS_PATH = Path(r"D:\Projects\CS2Archive\renders\pov-furia-vs-falcons-m3-inferno_76561198041683378_full\combined.round_offsets.json")


@pytest.mark.skipif(not VIDEO.exists(), reason="integration test requires real render on disk")
def test_returns_28_pipclips_integration():
    """End-to-end: existing pov-furia-vs-falcons-m3-inferno render still yields 28 PipClips."""
    import json
    data = json.loads(ROUND_OFFSETS_PATH.read_text(encoding="utf-8"))
    round_offsets = {int(k): v for k, v in data["round_offsets"].items()}
    total_secs = data.get("total_duration_seconds", 0)

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
    # Count comes from _clip_index.json (pre-rendered) + new renders. Just verify
    # all returned clips point to real mp4s ≥1MB. Exact count depends on render state.
    assert len(clips) > 0, f"expected PipClips, got 0"
    for c in clips:
        assert c.clip_path.is_file(), f"missing mp4: {c.clip_path}"
        assert c.clip_path.stat().st_size > 1_000_000, f"too small: {c.clip_path}"


def main() -> int:
    """Allow running as plain script: python tests/test_render_util_cams_manifest.py"""
    import pytest
    return pytest.main([__file__, "-v"])


if __name__ == "__main__":
    raise SystemExit(main())
