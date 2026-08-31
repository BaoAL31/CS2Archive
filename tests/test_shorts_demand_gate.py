"""Shorts demand gate: skip low-search POVs that burn daily slots."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.demand_gate import filter_publishable_shorts, passes_shorts_demand_gate

PAYLOAD = {
    "index": {"donk": 1.53, "m0nesy": 1.28, "tn1r": 1.19},
    "players": {
        "donk": {"videos": 551, "index": 1.53},
        "m0NESY": {"videos": 259, "index": 1.28},
        "tN1R": {"videos": 20, "index": 1.19},
        "ZywOo": {"videos": 139, "index": 1.05},
        "b1t": {"videos": 64, "index": 1.0},
        "w0nderful": {"videos": 44, "index": 0.69},
        "makazze": {"videos": 32, "index": 0.69},
        "r1nkle": {"videos": 4, "index": 0.9},
    },
}


def test_demand_player_passes_without_org():
    assert passes_shorts_demand_gate("donk", payload=PAYLOAD)
    assert passes_shorts_demand_gate("m0NESY", payload=PAYLOAD)
    assert passes_shorts_demand_gate("tN1R", payload=PAYLOAD)


def test_measured_star_just_under_notable_floor_still_passes():
    assert passes_shorts_demand_gate("ZywOo", payload=PAYLOAD)
    assert passes_shorts_demand_gate("b1t", payload=PAYLOAD)


def test_unknown_pov_is_dropped():
    assert not passes_shorts_demand_gate("z4KR", payload=PAYLOAD)
    assert not passes_shorts_demand_gate("r1nkle", payload=PAYLOAD)
    assert not passes_shorts_demand_gate("qw1nk1", payload=PAYLOAD)


def test_falcons_opponent_does_not_rescue_unknown():
    assert not passes_shorts_demand_gate("z4KR", opponent="Falcons", payload=PAYLOAD)


def test_navi_or_spirit_hook_rescues_unknown():
    assert passes_shorts_demand_gate("JBa", opponent="NaVi", payload=PAYLOAD)
    assert passes_shorts_demand_gate("try", orgs=["Legacy", "Natus Vincere"], payload=PAYLOAD)
    assert passes_shorts_demand_gate(
        "gr1ks",
        text="gr1ks pulls off a 1v3 CLUTCH vs Spirit on Dust2",
        payload=PAYLOAD,
    )


def test_low_index_pro_without_hook_is_dropped():
    assert not passes_shorts_demand_gate("w0nderful", opponent="Legacy", payload=PAYLOAD)
    assert not passes_shorts_demand_gate("makazze", opponent="MongolZ", payload=PAYLOAD)


def test_filter_publishable_shorts_counts_drops():
    shorts = [
        {"pov_nick": "donk"},
        {"pov_nick": "z4KR"},
        {"pov_nick": "JBa"},
    ]
    kept, dropped = filter_publishable_shorts(
        shorts, orgs=["Lynn Vision", "Falcons"], payload=PAYLOAD,
    )
    assert [s["pov_nick"] for s in kept] == ["donk"]
    assert dropped == 2

    kept_navi, dropped_navi = filter_publishable_shorts(
        shorts, orgs=["M80", "NaVi"], payload=PAYLOAD,
    )
    assert [s["pov_nick"] for s in kept_navi] == ["donk", "z4KR", "JBa"]
    assert dropped_navi == 0


def test_skipped_meta_is_not_pending() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
    import upload_pending_shorts as ups

    meta = {
        "upload_status": "skipped",
        "skip_reason": "low_demand",
        "tiktok_status": "skipped",
        "instagram_status": "skipped",
    }
    assert not ups._needs_upload(meta, skip_tiktok=False, skip_instagram=False)
    pending = ups._platform_pending(meta)
    assert pending == {"youtube": False, "tiktok": False, "instagram": False}


def test_upload_gate_reads_nick_and_folder(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
    import upload_pending_shorts as ups

    folder = tmp_path / "shorts-z4kr-4k"
    folder.mkdir()
    (folder / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(
            tmp_path / "2390000-lynn-vision-vs-falcons-m1-nuke" / "demo.dem"
        ),
        "shorts": [{"pov_nick": "z4KR"}],
    }), encoding="utf-8")
    meta_path = folder / "upload_meta_shorts.json"
    meta = {"title": "z4KR ACE vs Falcons on Nuke", "description": ""}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert not ups._passes_demand(meta_path, meta, payload=PAYLOAD)

    donk = tmp_path / "shorts-donk-ace"
    donk.mkdir()
    (donk / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(tmp_path / "2390001-spirit-vs-mouz-m1-nuke" / "demo.dem"),
        "shorts": [{"pov_nick": "donk"}],
    }), encoding="utf-8")
    donk_meta_path = donk / "upload_meta_shorts.json"
    donk_meta = {"title": "donk ACE vs MOUZ on Nuke", "description": ""}
    assert ups._passes_demand(donk_meta_path, donk_meta, payload=PAYLOAD)


def test_mark_skipped_stops_future_pending(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
    import upload_pending_shorts as ups

    meta_path = tmp_path / "upload_meta_shorts.json"
    meta = {
        "upload_status": "pending",
        "tiktok_status": "pending",
        "instagram_status": "pending",
        "title": "z4KR 4K vs Falcons",
    }
    ups._mark_skipped_low_demand(meta_path, meta)
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["upload_status"] == "skipped"
    assert saved["skip_reason"] == "low_demand"
    assert not ups._needs_upload(saved, skip_tiktok=False, skip_instagram=False)
