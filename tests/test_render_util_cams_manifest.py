"""Util-cam clip identity: combined flight+detonate, 1 MB floor, _throw_poses.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

import render_util_cams as ruc  # noqa: E402
import overlay_pov as op  # noqa: E402
from overlay._common import cameras_for_util_type, clip_is_done  # noqa: E402


@pytest.fixture
def throws_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e142:s1",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "smoke",
            "land_x": 500.0, "land_y": 200.0, "land_z": 100.0,
        },
        {
            "throw_id": "2395002-furia-vs-falcons-m3-inferno:e300:s2",
            "thrower_steamid": 76561198041683378,
            "thrower_side": "T",
            "map": "de_inferno",
            "util_type": "he",
            "land_x": 0.0, "land_y": 0.0, "land_z": 0.0,
        },
    ])


def test_smoke_cameras_are_combined_flight_detonate():
    assert cameras_for_util_type("smoke") == "flight,detonate"
    assert ruc._cameras_for_type("molotov") == "flight,detonate"
    assert ruc._cameras_for_type("he") == "flight"


def test_util_id_uses_map_from_row(throws_df):
    uid = ruc._util_id_for_row(throws_df.iloc[0].to_dict())
    assert uid.startswith("de_inferno:smoke:T:")


def test_util_id_requires_map():
    with pytest.raises(ValueError, match="map is required"):
        ruc._util_id_for_row({"util_type": "he", "land_x": 0, "land_y": 0, "land_z": 0})


def test_discover_requires_combined_clip_at_one_meg(tmp_path: Path):
    util_dir = tmp_path / "unnamed" / "spot"
    util_dir.mkdir(parents=True)
    tid = "2395002-foo:e1:s1"
    (util_dir / "_throw_poses.json").write_text(json.dumps({
        "_cameras": "flight,detonate",
        "_throws": {tid: {"pos": [0, 0, 0]}},
    }), encoding="utf-8")
    clip = util_dir / f"{ruc.clip_name_for_cameras('flight,detonate', tid)}.mp4"
    clip.write_bytes(b"x" * 100_000)
    assert ruc._discover_util_cams(tmp_path) == [util_dir]
    clip.write_bytes(b"x" * 1_000_001)
    assert ruc._discover_util_cams(tmp_path) == []
    assert clip_is_done(clip)


def test_scan_reads_throw_poses_combined_clip(tmp_path: Path):
    youtube_dir = tmp_path / "youtube" / "test_run"
    video = youtube_dir / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    util_dir = youtube_dir / "utility_cams" / "unnamed" / "spot"
    util_dir.mkdir(parents=True)
    tid = "x:e1:s0"
    (util_dir / "_throw_poses.json").write_text(json.dumps({
        "_cameras": "flight,detonate",
        "_throws": {tid: {"pos": [0, 0, 0]}},
    }), encoding="utf-8")
    clip = util_dir / f"{ruc.clip_name_for_cameras('flight,detonate', tid)}.mp4"
    clip.write_bytes(b"x" * 1_000_001)
    result = op._scan_utility_cams_clips(video)
    assert result[tid] == clip.resolve()


def test_scan_empty_without_poses(tmp_path: Path):
    youtube_dir = tmp_path / "youtube" / "test_run"
    video = youtube_dir / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    (youtube_dir / "utility_cams").mkdir()
    assert op._scan_utility_cams_clips(video) == {}


DEMO = Path(
    r"D:\Projects\CS2Archive\demos\hltv\furia-vs-falcons-iem-cologne-major"
    r"\furia-vs-falcons-m3-inferno.dem"
)
VIDEO = Path(
    r"D:\Projects\CS2Archive\renders\pov-furia-vs-falcons-m3-inferno"
    r"_76561198041683378_full\combined.mp4"
)
ROUND_OFFSETS_PATH = VIDEO.with_name("combined.round_offsets.json")


@pytest.mark.skipif(not VIDEO.exists(), reason="integration test requires real render on disk")
def test_returns_pipclips_integration():
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
    assert len(clips) > 0
    for c in clips:
        assert c.clip_path.is_file()
        assert c.clip_path.stat().st_size > 1_000_000
