"""Shorts demand gate: skip low-search POVs that burn daily slots."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.demand_gate import filter_publishable_shorts, passes_shorts_demand_gate

STARS = {
    "intercept": 5.0,
    "player": {"76561198000000001": 0.8},
    "opponent": {"Natus Vincere": 0.3},
    "stage": {"playoff": 0.2, "grand_final": 0.4},
    "kind": {"1v3_won": 0.4, "ace": 0.5, "4k": 0.2},
}


def _cut(**kwargs) -> dict:
    base = {
        "pov_nick": "latto",
        "pov_steam_id": "76561198000000002",
        "short_type": "clutch",
        "clutch_initial_count": "1v3",
        "kill_ticks": [1, 2, 3],
    }
    base.update(kwargs)
    return base


def test_hltv_keeps_only_when_candidate_score_above_intercept():
    clutch_ace = _cut(kill_ticks=[1, 2, 3, 4, 5])
    baseline = _cut(short_type="4k", clutch_initial_count=None, kill_ticks=[1])
    kept, dropped = filter_publishable_shorts(
        [clutch_ace, baseline], source="hltv", stars=STARS,
    )
    assert kept == [clutch_ace]
    assert dropped == 1


def test_hltv_slot_floor_keeps_none_at_or_below_intercept():
    weak = _cut(short_type="4k", clutch_initial_count=None, kill_ticks=[1])
    kept, dropped = filter_publishable_shorts(
        [weak, dict(weak)], source="hltv", stars=STARS,
    )
    assert kept == []
    assert dropped == 2


def test_hltv_ranks_keepers_by_candidate_score_descending():
    clutch_only = _cut(kill_ticks=[1, 2, 3])
    ace_only = _cut(
        short_type="4k", clutch_initial_count=None, kill_ticks=[1, 2, 3, 4, 5],
    )
    both = _cut(kill_ticks=[1, 2, 3, 4, 5])
    kept, dropped = filter_publishable_shorts(
        [clutch_only, ace_only, both], source="hltv", stars=STARS,
    )
    assert kept[0] is both
    assert kept[1] is ace_only
    assert kept[2] is clutch_only
    assert dropped == 0


def test_hltv_1v3_ace_scores_above_either_kind_alone():
    from shorts.demand_gate import candidate_score

    both = _cut(kill_ticks=[1, 2, 3, 4, 5])
    clutch_only = _cut(kill_ticks=[1, 2, 3])
    ace_only = _cut(
        short_type="4k", clutch_initial_count=None, kill_ticks=[1, 2, 3, 4, 5],
    )
    assert candidate_score(both, STARS) > candidate_score(clutch_only, STARS)
    assert candidate_score(both, STARS) > candidate_score(ace_only, STARS)


def test_hltv_cut_uses_other_fixture_side_when_opponent_unset():
    weak = _cut(
        short_type="4k",
        clutch_initial_count=None,
        kill_ticks=[1],
        pov_team="FURIA",
    )
    kept, dropped = filter_publishable_shorts(
        [weak], source="hltv", orgs=["FURIA", "NaVi"], stars=STARS,
    )
    assert kept == [weak]
    assert dropped == 0
    no_side, dropped_no = filter_publishable_shorts(
        [_cut(short_type="4k", clutch_initial_count=None, kill_ticks=[1])],
        source="hltv",
        orgs=["FURIA", "NaVi"],
        stars=STARS,
    )
    assert no_side == []
    assert dropped_no == 1


def test_hltv_navi_opponent_is_partial_star_not_hard_keep():
    navi = _cut(
        short_type="4k",
        clutch_initial_count=None,
        kill_ticks=[1],
        opponent="NaVi",
    )
    kept_zero, dropped_zero = filter_publishable_shorts(
        [navi],
        source="hltv",
        stars={**STARS, "opponent": {}},
    )
    assert kept_zero == []
    assert dropped_zero == 1
    kept_star, dropped_star = filter_publishable_shorts(
        [navi], source="hltv", stars=STARS,
    )
    assert kept_star == [navi]
    assert dropped_star == 0


def test_hltv_unset_player_opponent_stage_and_kinds_add_nothing():
    from shorts.demand_gate import candidate_score

    baseline = _cut(short_type="4k", clutch_initial_count=None, kill_ticks=[1])
    assert candidate_score(baseline, STARS) == STARS["intercept"]


def test_hltv_source_and_clip_age_are_not_in_candidate_score():
    from shorts.demand_gate import candidate_score

    cut = _cut(
        kill_ticks=[1, 2, 3, 4, 5],
        source="allstar",
        clip_age=90,
    )
    polluted = {
        **STARS,
        "source": {"allstar": 9.0},
        "clip_age": 9.0,
    }
    assert candidate_score(cut, polluted) == candidate_score(cut, STARS)


def test_hltv_stage_is_not_in_candidate_score():
    from shorts.demand_gate import candidate_score

    plain = _cut(kill_ticks=[1, 2, 3, 4, 5])
    playoff = _cut(kill_ticks=[1, 2, 3, 4, 5], stage="playoff")
    gf = _cut(kill_ticks=[1, 2, 3, 4, 5], stage="grand_final")
    assert candidate_score(playoff, STARS) == candidate_score(plain, STARS)
    assert candidate_score(gf, STARS) == candidate_score(plain, STARS)

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
        shorts, orgs=["Lynn Vision", "Falcons"], payload=PAYLOAD, source="faceit",
    )
    assert [s["pov_nick"] for s in kept] == ["donk"]
    assert dropped == 2

    kept_navi, dropped_navi = filter_publishable_shorts(
        shorts, orgs=["M80", "NaVi"], payload=PAYLOAD, source="faceit",
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

    faceit_dir = tmp_path / "demos" / "faceit" / "match"
    faceit_dir.mkdir(parents=True)
    folder = tmp_path / "shorts-z4kr-4k"
    folder.mkdir()
    (folder / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(faceit_dir / "demo.dem"),
        "shorts": [{"pov_nick": "z4KR"}],
    }), encoding="utf-8")
    meta_path = folder / "upload_meta_shorts.json"
    meta = {"title": "z4KR ACE vs Falcons on Nuke", "description": ""}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert not ups._passes_demand(meta_path, meta, payload=PAYLOAD)

    donk = tmp_path / "shorts-donk-ace"
    donk.mkdir()
    (donk / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(tmp_path / "demos" / "faceit" / "match2" / "demo.dem"),
        "shorts": [{"pov_nick": "donk"}],
    }), encoding="utf-8")
    donk_meta_path = donk / "upload_meta_shorts.json"
    donk_meta = {"title": "donk ACE vs MOUZ on Nuke", "description": ""}
    assert ups._passes_demand(donk_meta_path, donk_meta, payload=PAYLOAD)


def test_hltv_upload_below_intercept_is_slot_floor(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
    import upload_pending_shorts as ups

    folder = tmp_path / "shorts-latto-baseline"
    folder.mkdir()
    (folder / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(tmp_path / "demos" / "hltv" / "2390000-furia-vs-vitality" / "demo.dem"),
        "shorts": [_cut(short_type="4k", clutch_initial_count=None, kill_ticks=[1])],
    }), encoding="utf-8")
    meta_path = folder / "upload_meta_shorts.json"
    meta = {
        "upload_status": "pending",
        "tiktok_status": "pending",
        "instagram_status": "pending",
        "title": "latto 4K vs Vitality",
    }
    assert not ups._passes_demand(meta_path, meta, stars=STARS)
    ups._mark_skipped(meta_path, meta, "slot_floor")
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["skip_reason"] == "slot_floor"
    assert not ups._needs_upload(saved, skip_tiktok=False, skip_instagram=False)


def test_hltv_upload_above_intercept_passes(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))
    import upload_pending_shorts as ups

    folder = tmp_path / "shorts-latto-ace"
    folder.mkdir()
    (folder / "short_timeline.json").write_text(json.dumps({
        "demo_path": str(tmp_path / "demos" / "hltv" / "2390000-furia-vs-vitality" / "demo.dem"),
        "shorts": [_cut(kill_ticks=[1, 2, 3, 4, 5])],
    }), encoding="utf-8")
    meta_path = folder / "upload_meta_shorts.json"
    meta = {"title": "latto 1v3 ACE", "description": ""}
    assert ups._passes_demand(meta_path, meta, stars=STARS)


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
