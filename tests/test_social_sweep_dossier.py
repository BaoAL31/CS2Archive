"""Moment dossiers: no fabrication, explicit bounds/uncertainty, idempotent writes."""

from __future__ import annotations

import json

from cs2archive.shorts.social.dossier import (
    DOSSIER_VERSION, build_dossier, dossier_source_hash, load_state_rows, moment_id,
    render_text, tick_to_seconds, write_dossier,
)

STEAM = "76561199032006224"
OPP = "76561198074762801"


def _timeline(**overrides):
    timeline = {
        "timeline_version": 3,
        "event_data_version": 1,
        "stakes_version": 2,
        "place_source": "nav",
        "demo_path": "demos/hltv/2399649-a-vs-b-iem-krakw/a-vs-b-m3-inferno.dem",
        "map": "Inferno",
        "teams": {"2": [STEAM, "76561190000000002"], "3": [OPP, "76561190000000004"]},
        "raw_player_info": {"columns": ["steamid", "name", "team_number"],
                            "rows": [{"steamid": STEAM, "name": "kyousuke",
                                      "team_number": 2},
                                     {"steamid": OPP, "name": "m0NESY",
                                      "team_number": 3}]},
        "pros": [{"steam_id": STEAM, "nick": "kyousuke",
                  "rounds": [{"round": 12, "kills": 3, "deaths": 0, "openers": 1,
                              "openers_won": 1, "multikills": 1, "best_multi": 3,
                              "clutches_won": 0, "clutch_attempts": 0,
                              "tags": ["multi"]}]}],
        "sides_by_round": {"12": {"T": 2, "CT": 3}},
        "winner_by_round": {"12": 2},
        "win_reason_by_round": {"12": "t_killed"},
        "rounds": [{
            "round": 12, "start_tick": 80000, "freeze_end_tick": 81274,
            "end_tick": 87600, "winner_team": 2, "win_reason": "t_killed",
            "score_after": {"2": 7, "3": 5}, "match_point_for": None,
            "overtime": False, "closes_map": None,
            "alive": [{"tick": 80000, "teams": {"2": 3, "3": 4}},
                      {"tick": 86500, "teams": {"2": 2, "3": 4}},
                      {"tick": 87000, "teams": {"2": 2, "3": 3}}],
            "hot_zones": ["Banana"],
            "stakes": {"buys": {"2": "full", "3": "eco"},
                       "equip_value": {"2": 21500, "3": 7000},
                       "win_streak_before": {"2": 2, "3": 0},
                       "deficit_at_start": {"2": 2, "3": -2},
                       "is_pistol": False, "buy_source": "ticks"},
        }],
        "kills_all": [
            {"tick": 86559, "round": 12, "attacker": "kyousuke",
             "attacker_steam_id": STEAM, "victim": "m0NESY",
             "victim_steam_id": OPP, "weapon": "ak47", "victim_weapon": "m4a1",
             "is_bomb": False, "headshot": True, "penetrated": 1,
             "attacker_place": "Banana", "victim_place": "Banana",
             "kill_event": {"headshot": True, "distance": 10.6, "hitgroup": "head",
                            "dmg_health": 118, "penetrated": 1, "thrusmoke": True,
                            "noscope": False, "dominated": 1, "revenge": 0,
                            "assister_name": None, "assister_steamid": None}},
            {"tick": 86641, "round": 12, "attacker": "kyousuke",
             "attacker_steam_id": STEAM, "victim": "TeSeS",
             "victim_steam_id": "76561197996678278", "weapon": "ak47",
             "victim_weapon": "Glock-18", "is_bomb": False, "headshot": False,
             "kill_event": {"headshot": False, "distance": 22.0,
                            "hitgroup": "chest", "dmg_health": 100}},
            {"tick": 87800, "round": 12, "attacker": "m0NESY",
             "attacker_steam_id": OPP, "victim": "kyousuke",
             "victim_steam_id": STEAM, "weapon": "m4a1", "victim_weapon": "ak47",
             "is_bomb": False, "headshot": False, "kill_event": {"headshot": False}},
        ],
        "raw_events": {
            "player_hurt": {"rows": [
                {"tick": 86500, "attacker_steam_id": STEAM, "attacker_name": "kyousuke",
                 "user_steamid": OPP, "user_name": "m0NESY", "weapon": "ak47",
                 "dmg_health": 40, "dmg_armor": 0, "hitgroup": "chest",
                 "health": 60, "armor": 0},
                {"tick": 99000, "attacker_steam_id": STEAM, "user_steamid": OPP,
                 "dmg_health": 9},  # outside the window: must be excluded
            ]},
            "weapon_fire": {"rows": [
                {"tick": 86550, "user_steamid": STEAM, "user_name": "kyousuke",
                 "weapon": "weapon_ak47", "silenced": False},
                {"tick": 86555, "user_steamid": STEAM, "user_name": "kyousuke",
                 "weapon": "weapon_ak47", "silenced": False},
                {"tick": 86630, "user_steamid": OPP, "user_name": "m0NESY",
                 "weapon": "weapon_m4a1", "silenced": False},
            ]},
            "player_death": {"rows": []},
        },
        "util_throws": [{"tick": 86300, "round": 12, "player": "kyousuke",
                         "player_steam_id": STEAM, "util": "hegrenade",
                         "x": 100.0, "y": 200.0}],
        "util_damages": [{"tick": 86400, "round": 12, "attacker": "kyousuke",
                          "attacker_steam_id": STEAM, "victim": "m0NESY",
                          "victim_steam_id": OPP, "weapon": "hegrenade",
                          "dmg": 24.0}],
        "bomb_actions": [{"tick": 86700, "round": 12, "type": "plant",
                          "player": "kyousuke", "player_steam_id": STEAM,
                          "site": "BombsiteA"}],
        "blinds": [],
    }
    timeline.update(overrides)
    return timeline


def _alignment(**overrides):
    alignment = {
        "clip_id": "abc123", "demo": "demos/hltv/2399649-a-vs-b-iem-krakw/"
                                     "a-vs-b-m3-inferno.dem",
        "match_id": "2399649", "steamid": STEAM, "player": "kyousuke", "round": 12,
        "tick_start": 86239, "tick_end": 86862,
        "round_tick_start": 80000, "round_tick_end": 87600,
        "kill_ticks": [86559, 86641], "title": "AK-47 4K on Inferno",
        "explicit_kills": 4, "demo_kills": 4, "confidence": "exact",
        "method": "steamid+map+round+kills",
    }
    alignment.update(overrides)
    return alignment


def test_tick_to_seconds_uses_the_declared_tickrate():
    assert tick_to_seconds(640) == 10.0
    assert tick_to_seconds(640, 128) == 5.0
    assert tick_to_seconds(700, 64, origin=636) == 1.0
    assert tick_to_seconds(None) is None


def test_dossier_carries_identity_round_context_and_pacing():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert dossier["dossier_version"] == DOSSIER_VERSION
    ident = dossier["identity"]
    assert ident["player_steam_id"] == STEAM
    assert ident["player_nick"] == "kyousuke"
    assert ident["side"] == "T"                # from sides_by_round, not guessed
    assert ident["team_org_slug"] == "a" and ident["opponent_org_slug"] == "b"
    rc = dossier["round_context"]
    assert rc["round"] == 12 and rc["win_reason"] == "t_killed"
    assert rc["stakes"]["buys"] == {"2": "full", "3": "eco"}
    assert rc["player_round_stats"]["kills"] == 3
    adv = rc["man_advantage_at_first_kill"]
    assert adv["alive_at_first_player_kill"] == {"2": 2, "3": 4}
    assert adv["disadvantage"] == 2
    pacing = dossier["pacing"]
    assert pacing["player_kills"] == 2
    assert pacing["kill_span_seconds"] == round((86641 - 86559) / 64.0, 3)
    assert pacing["player_shots_in_window"] == 2
    assert pacing["shots_per_kill"] == 1.0


def test_bounds_distinguish_inferred_window_from_published_clip():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    bounds = dossier["bounds"]
    assert bounds["kill_window"]["source"] == "kill_anchored_inferred"
    assert bounds["kill_window"]["tick_start"] == 86239
    assert bounds["kill_window"]["kill_ticks"] == [86559, 86641]
    assert bounds["round"]["tick_start"] == 80000
    published = bounds["published_clip_bounds"]
    assert published["known"] is False
    assert "not available" in published["reason"]


def test_clip_verification_is_never_claimed_exact():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    verification = dossier["clip_verification"]
    assert verification["verified"] is False
    assert "plausible evidence, not proof" in verification["note"]
    assert verification["basis"] == "map + player + round + kill-count alignment"


def test_chronology_is_ordered_and_window_bounded():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    ticks = [entry["tick"] for entry in dossier["chronology"]]
    assert ticks == sorted(ticks)
    assert min(ticks) >= 86239 and max(ticks) <= 86862
    kinds = {entry["kind"] for entry in dossier["chronology"]}
    assert {"damage", "kill", "shot", "utility_thrown", "utility_damage",
            "bomb"} <= kinds
    kill = next(e for e in dossier["chronology"] if e["kind"] == "kill")
    assert kill["headshot"] is True and kill["thrusmoke"] is True
    assert kill["victim_weapon"] == "m4a1"
    assert kill["penetrated"] == 1
    # The out-of-window hurt row must not appear.
    assert all(e.get("dmg_health") != 9 for e in dossier["chronology"]
               if e["kind"] == "damage")


def test_missing_state_is_reported_not_invented():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert dossier["player_state"] is None
    assert dossier["availability"]["player_state"] is False
    assert any("positions_health_angles" in item for item in dossier["unknowns"])
    # Baseline unknowns always present: LOS/intent/ammo are never fabricated.
    assert any("line_of_sight" == item for item in dossier["unknowns"])
    assert any("player_intent" == item for item in dossier["unknowns"])


def test_state_rows_are_summarised_with_their_source():
    rows = [{"tick": 86240 + i * 8, "steamid": STEAM, "name": "kyousuke",
             "X": 100.0 + i, "Y": 200.0, "Z": 98.0, "health": 100,
             "yaw": 70.0, "pitch": 3.0, "m_iItemDefinitionIndex": 7}
            for i in range(6)]
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment(),
                            state_rows=rows)
    state = dossier["player_state"]
    assert state["samples"] == 6
    assert state["health_min"] == 100
    assert state["weapon_indices"] == [7]
    assert state["snapshot_sources"] == ["unknown"]
    assert dossier["availability"]["player_state"] is True
    assert not any("positions_health_angles" in item for item in dossier["unknowns"])


def test_missing_round_and_missing_stakes_do_not_break_the_dossier():
    timeline = _timeline()
    timeline["rounds"] = []                       # no round evidence at all
    dossier = build_dossier(timeline=timeline,
                            alignment=_alignment(round=99, kill_ticks=[86559]))
    assert dossier["round_context"]["round"] == 99
    assert dossier["round_context"]["winner_team"] is None
    assert dossier["availability"]["round_stakes"] is False
    assert dossier["bounds"]["kill_window"]["kill_ticks"] == [86559]


def test_dossier_without_alignment_falls_back_to_kill_ticks_only():
    dossier = build_dossier(timeline=_timeline(), alignment=None,
                            source="render_ledger",
                            linkage={"demo_path": "demos/hltv/x/y.dem",
                                     "match_id": "1", "round": 12,
                                     "player_steam_id": STEAM,
                                     "kill_ticks": [86559, 86641]})
    assert dossier["source"] == "render_ledger"
    assert dossier["bounds"]["kill_window"]["tick_start"] == 86559 - 128
    assert dossier["bounds"]["kill_window"]["tick_end"] == 86641 + 64
    assert dossier["bounds"]["kill_window"]["kill_ticks"] == [86559, 86641]
    # A linkage-only moment still gets round context from the timeline.
    assert dossier["round_context"]["winner_team"] == 2


def test_source_hash_ignores_unrelated_fields_and_drives_idempotency(tmp_path):
    first = build_dossier(timeline=_timeline(), alignment=_alignment())
    second = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert dossier_source_hash(first) == dossier_source_hash(second)
    changed = build_dossier(timeline=_timeline(),
                            alignment=_alignment(kill_ticks=[86559]))
    assert dossier_source_hash(changed) != dossier_source_hash(first)

    root = tmp_path / "dossiers"
    status, path = write_dossier(first, root=root)
    assert status == "built" and (root / f"{first['moment_id']}.txt").is_file()
    again, _ = write_dossier(second, root=root)
    assert again == "skipped_current"
    forced, _ = write_dossier(second, root=root, force=True)
    assert forced == "built"


def test_moment_id_is_stable_and_distinguishes_moments():
    a = build_dossier(timeline=_timeline(), alignment=_alignment())
    b = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert moment_id(a) == moment_id(b)
    other = build_dossier(timeline=_timeline(), alignment=_alignment(round=13))
    assert moment_id(other) != moment_id(a)


def test_text_render_is_llm_readable_and_states_unknowns():
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment())
    dossier["moment_id"] = moment_id(dossier)
    text = render_text(dossier)
    assert "chronology" in text.lower()
    assert "INFERRED" in text
    assert "published clip trim: UNKNOWN" in text
    assert "not proof" in text
    assert "Unknowns" in text
    assert "round context" in text.lower()


def test_load_state_rows_prefers_staged_state_and_reports_source(tmp_path):
    import pandas as pd

    clip = tmp_path / "clips" / "c1"
    clip.mkdir(parents=True)
    pd.DataFrame([{"tick": 100, "steamid": STEAM, "health": 100}]).to_parquet(
        clip / "state.parquet", index=False)
    rows = load_state_rows(clip)
    assert rows and rows[0]["_source"] == "state.parquet"
    assert load_state_rows(tmp_path / "clips" / "missing") is None


def test_realistic_staged_clip_produces_a_dossier_from_disk(tmp_path):
    """Against the real artifact shape (packed events.parquet + alignment.json)."""
    import pandas as pd

    clip = tmp_path / "clips" / "real1"
    clip.mkdir(parents=True)
    (clip / "alignment.json").write_text(json.dumps(_alignment()), encoding="utf-8")
    events = {
        "kills": _timeline()["kills_all"],
        "player_hurt": _timeline()["raw_events"]["player_hurt"]["rows"],
        "weapon_fire": _timeline()["raw_events"]["weapon_fire"]["rows"],
    }
    pd.DataFrame([{"key": k, "payload": json.dumps(v)} for k, v in events.items()]
                 ).to_parquet(clip / "events.parquet", index=False)
    timeline_path = tmp_path / "_timelines" / "x.json"
    timeline_path.parent.mkdir(parents=True)
    timeline_path.write_text(json.dumps(_timeline()), encoding="utf-8")

    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    alignment = json.loads((clip / "alignment.json").read_text(encoding="utf-8"))
    dossier = build_dossier(timeline=timeline, alignment=alignment,
                            state_rows=load_state_rows(clip))
    assert dossier["counts"]["player_kills_in_window"] == 2
    assert dossier["availability"]["chronology"] is True
    assert dossier["provenance"]["alignment_confidence"] == "exact"


def test_nan_state_fields_do_not_crash_or_fabricate(tmp_path):
    """Real parquet columns contain NaN; it must read as unavailable, not 0."""
    import pandas as pd

    clip = tmp_path / "clips" / "nan1"
    clip.mkdir(parents=True)
    (clip / "alignment.json").write_text(json.dumps(_alignment()), encoding="utf-8")
    frame = pd.DataFrame([
        {"tick": 86240, "steamid": STEAM, "X": 100.0, "Y": 200.0, "Z": 98.0,
         "health": 100.0, "yaw": 70.0, "pitch": 3.0, "m_iItemDefinitionIndex": 7.0},
        {"tick": 86248, "steamid": STEAM, "X": float("nan"), "Y": float("nan"),
         "Z": float("nan"), "health": float("nan"), "yaw": float("nan"),
         "pitch": float("nan"), "m_iItemDefinitionIndex": float("nan")},
    ])
    frame.to_parquet(clip / "state.parquet", index=False)
    rows = load_state_rows(clip)
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment(),
                            state_rows=rows)
    state = dossier["player_state"]
    assert state["samples"] == 2
    assert state["weapon_indices"] == [7]     # NaN dropped, not coerced to 0
    assert state["health_min"] == 100.0
    assert state["position_x_range"] == [100.0, 100.0]


def test_state_rows_outside_the_window_are_excluded():
    rows = [{"tick": 10, "steamid": STEAM, "X": 1.0, "health": 100},
            {"tick": 86240, "steamid": STEAM, "X": 2.0, "health": 100},
            {"tick": 999999, "steamid": STEAM, "X": 3.0, "health": 100}]
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment(),
                            state_rows=rows)
    assert dossier["player_state"]["samples"] == 1
    assert dossier["player_state"]["position_x_range"] == [2.0, 2.0]


def test_other_players_state_is_not_attributed_to_the_pov_player():
    rows = [{"tick": 86240, "steamid": OPP, "X": 5.0, "health": 40}]
    dossier = build_dossier(timeline=_timeline(), alignment=_alignment(),
                            state_rows=rows)
    assert dossier["player_state"] is None
    assert dossier["availability"]["player_state"] is False


def test_state_source_is_recorded_and_invalidates_the_hash():
    without = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert without["provenance"]["state_source"] == "none"
    rows = [{"tick": 86240 + i * 8, "steamid": STEAM, "X": 1.0, "health": 100,
             "_source": "state_enriched.parquet"} for i in range(4)]
    with_state = build_dossier(timeline=_timeline(), alignment=_alignment(),
                               state_rows=rows)
    assert with_state["provenance"]["state_source"] == "state_enriched.parquet"
    # Adding state must yield a different hash, otherwise write_dossier would
    # silently skip the enriched dossier.
    assert dossier_source_hash(with_state) != dossier_source_hash(without)


def test_hash_ignores_generation_time():
    a = build_dossier(timeline=_timeline(), alignment=_alignment())
    b = build_dossier(timeline=_timeline(), alignment=_alignment())
    assert a["generated_at"] != "" and b["generated_at"] != ""
    assert dossier_source_hash(a) == dossier_source_hash(b)
