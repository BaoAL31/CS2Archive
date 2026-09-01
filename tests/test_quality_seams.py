"""Seams for overlay-only variant, scoring chips, clip-done, pipeline_cmd."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _pathsetup import ensure

ensure()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
from _backlog_common import pipeline_cmd, write_card
from variant import resolve_skip_overlay, youtube_dir_name
from overlay._common import cameras_for_util_type, clip_is_done
from scoring import market_demand_bonus, star_bonus
from upload_youtube import youtube_upload_completed


def test_default_is_overlay():
    assert resolve_skip_overlay(raw_only=False, state={}) is False
    assert youtube_dir_name("abc_def", skip_overlay=False) == "abc_def_overlay"


def test_raw_only_wins_over_sticky_overlay_state():
    state = {"data": {"overlay_only": True, "skip_overlay": False}}
    assert resolve_skip_overlay(raw_only=True, state=state) is True
    assert youtube_dir_name("abc_def", skip_overlay=True) == "abc_def"


def test_resume_legacy_dual_upload_false_is_raw():
    state = {"data": {"dual_upload": False}}
    assert resolve_skip_overlay(raw_only=False, state=state) is True


def test_resume_legacy_overlay_only_is_overlay():
    state = {"data": {"overlay_only": True, "dual_upload": True}}
    assert resolve_skip_overlay(raw_only=False, state=state) is False


def test_smoke_cameras_are_combined():
    assert cameras_for_util_type("smoke") == "flight,detonate"
    assert cameras_for_util_type("molotov") == "flight,detonate"
    assert cameras_for_util_type("he") == "flight"
    assert cameras_for_util_type("flash") == "flight"


def test_clip_is_done_uses_one_meg_floor(tmp_path: Path):
    small = tmp_path / "tiny.mp4"
    small.write_bytes(b"x" * 100_000)
    big = tmp_path / "ok.mp4"
    big.write_bytes(b"x" * 1_000_001)
    assert clip_is_done(small) is False
    assert clip_is_done(big) is True
    assert clip_is_done(tmp_path / "missing.mp4") is False


def test_youtube_upload_completed_requires_id_and_status():
    assert youtube_upload_completed({"upload_status": "completed", "youtube_id": "abc"})
    assert not youtube_upload_completed({"upload_status": "completed"})
    assert not youtube_upload_completed({"youtube_id": "abc"})
    assert not youtube_upload_completed({})


def test_pipeline_cmd_uses_this_interpreter(tmp_path: Path):
    card = tmp_path / "backlog" / "high" / "x.json"
    cmd = pipeline_cmd(card)
    assert sys.executable.replace("\\", "/") in cmd.replace("\\", "/")
    assert "--overlay-only" not in cmd
    assert "scripts/pov/pipeline.py --backlog" in cmd


def test_write_card_fills_pipeline_cmd(tmp_path: Path):
    dest = tmp_path / "card.json"
    write_card({"player": "donk", "map": "Nuke"}, dest)
    data = json.loads(dest.read_text(encoding="utf-8"))
    cmd = data["pipeline_cmd"].replace("\\", "/")
    assert sys.executable.replace("\\", "/") in cmd
    assert "pipeline.py" in cmd


def test_star_bonus_still_pays_plus_kd_losses():
    assert star_bonus(400_000, False, kd=1.5) == 200_000
    assert star_bonus(400_000, True, kd=0.77) == 0


def test_market_demand_bonus_research_table(tmp_path: Path):
    missing = tmp_path / "no-index.json"
    assert market_demand_bonus("ropz", path=missing) == 172_500
    assert market_demand_bonus("unmeasured", path=missing) == 0
