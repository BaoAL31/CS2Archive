"""Allstar Trending clips become Clip Observations (kinds, opponent, stage)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.clip_observation import (
    canonical_ranking_name,
    kinds_from_cut,
    observation_from_allstar,
    observation_from_to,
    observations_from_match_row,
    opponent_of_cut,
    parse_kinds,
    parse_stage,
)


LATTO_CLIP = {
    "clip_id": "6a95e7c2a21320c31c2d71cf",
    "steamid": "76561198850020186",
    "player": "latto",
    "match_id": "2396941",
    "title": "Dust 2 1V3 Ace Clutch",
    "label": "latto Dust 2 1V3 Ace Clutch",
    "views": 146082,
    "round": 4,
    "opponent_team": "Vitality",
}


def test_latto_1v3_ace_clutch_is_both_kinds_not_fused():
    obs = observation_from_allstar(LATTO_CLIP)
    assert obs is not None
    assert obs["kinds"] == ("1v3_won", "ace")


def test_allstar_observation_keeps_steam64_hltv_nick_and_match():
    obs = observation_from_allstar(LATTO_CLIP)
    assert obs["steamid"] == "76561198850020186"
    assert obs["player"] == "latto"
    assert obs["match_id"] == "2396941"


def test_clutch_kinds_are_mutex_higher_disadvantage_wins():
    clip = {**LATTO_CLIP, "label": "latto Dust 2 1V4 1V3 Ace Clutch"}
    obs = observation_from_allstar(clip)
    assert obs["kinds"] == ("1v4_won", "ace")
    assert "1v3_won" not in obs["kinds"]


def test_five_kills_is_ace_not_also_4k():
    clip = {**LATTO_CLIP, "label": "latto Dust 2 1V3 Ace 4K Clutch"}
    obs = observation_from_allstar(clip)
    assert obs["kinds"] == ("1v3_won", "ace")
    assert "4k" not in obs["kinds"]


def test_almost_and_nearly_are_not_kinds():
    obs = observation_from_allstar({**LATTO_CLIP, "label": "latto Dust 2 almost ACE"})
    assert obs["kinds"] == ("ace",)
    assert "nearly" not in obs["kinds"]
    assert "nearly" not in parse_kinds("nearly 4K")


def test_demo_kinds_fill_empty_categories_without_replacing_label_clutch():
    from shorts.clip_observation import merge_label_and_demo_kinds

    assert merge_label_and_demo_kinds(
        ("1v3_won", "ace"),
        ("1v5_won", "flick", "perfect_shots", "defuse"),
    ) == ("1v3_won", "ace", "flick", "perfect_shots", "defuse")
    assert merge_label_and_demo_kinds(("4k",), ("1v5_won", "4k", "flick")) == (
        "4k", "1v5_won", "flick",
    )
    assert merge_label_and_demo_kinds((), ("2vx_won", "defuse")) == (
        "2vx_won", "defuse",
    )


def test_flick_perfect_shots_wallbang_knife_defuse_stack_with_clutch_and_multikill():
    clip = {
        **LATTO_CLIP,
        "label": "latto Dust 2 1V3 Ace perfect flick shots wallbang knife defuse",
    }
    obs = observation_from_allstar(clip)
    assert obs["kinds"] == (
        "1v3_won",
        "ace",
        "flick",
        "perfect_shots",
        "wallbang",
        "knife",
        "defuse",
    )


def test_navi_opponent_folds_to_natus_vincere_from_fixture():
    match = {
        "match_id": "2396941",
        "slug": "furia-vs-natus-vincere-blast-open-porto-2026",
        "stage": "Opening Stage",
    }
    clip = {**LATTO_CLIP, "opponent_team": "NaVi"}
    obs = observation_from_allstar(clip, match)
    assert obs["opponent"] == "Natus Vincere"


def test_unresolved_opponent_stays_unset():
    match = {
        "match_id": "2396941",
        "slug": "furia-vs-natus-vincere-blast-open-porto-2026",
    }
    unknown = observation_from_allstar(
        {**LATTO_CLIP, "opponent_team": "AcademyKids"}, match
    )
    assert unknown["opponent"] is None
    mismatched = observation_from_allstar(
        {**LATTO_CLIP, "opponent_team": "Vitality"}, match
    )
    assert mismatched["opponent"] is None
    no_match = observation_from_allstar(LATTO_CLIP)
    assert no_match["opponent"] is None


def test_stage_from_joined_match_not_round_number():
    clip = {**LATTO_CLIP, "round": 4}
    opening = observation_from_allstar(clip, {"stage": "Opening Stage"})
    assert opening["stage"] == "group"
    swiss = observation_from_allstar(clip, {"stage": "Swiss Round 3"})
    assert swiss["stage"] == "group"
    qf = observation_from_allstar(clip, {"stage": "Quarter-final"})
    assert qf["stage"] == "playoff"
    sf = observation_from_allstar(clip, {"stage": "Semifinals"})
    assert sf["stage"] == "playoff"
    gf = observation_from_allstar(clip, {"stage": "Grand Final"})
    assert gf["stage"] == "grand_final"
    unset = observation_from_allstar(clip, {"stage": ""})
    assert unset["stage"] is None
    no_match = observation_from_allstar(clip)
    assert no_match["stage"] is None
    assert parse_stage("Group B lower bracket final") == "group"
    assert parse_stage("Round of 16") == "playoff"
    assert parse_stage("Stage 1 round") == "group"
    assert parse_stage("3rd place decider") == "playoff"
    assert parse_stage("Grand final 1") == "grand_final"
    assert parse_stage("Quarter-final TBA TBA Watch") == "playoff"


def test_hltv_highlight_boxes_are_not_clip_observations():
    box = {
        "clip_id": "twitch-m1r7",
        "steamid": "76561198850020186",
        "player": "latto",
        "match_id": "2396941",
        "title": "M1R7 | latto — 1v3 clutch",
        "label": "M1R7 | latto — 1v3 clutch",
        "views": 0,
    }
    assert observation_from_allstar(box) is None
    grid = {
        **LATTO_CLIP,
        "title": "M2R12 | ZywOo — ACE",
        "label": "M2R12 | ZywOo — ACE",
    }
    assert observation_from_allstar(grid) is None


def test_unmatched_label_does_not_force_3k():
    clip = {**LATTO_CLIP, "title": "Dust 2 AK-47 2K", "label": "latto Dust 2 AK-47 2K"}
    obs = observation_from_allstar(clip)
    assert obs["kinds"] == ()


def test_deferred_1v2_is_not_a_clutch_kind():
    clip = {**LATTO_CLIP, "label": "HeavyGod Mirage 1V2 4K Clutch"}
    obs = observation_from_allstar(clip)
    assert obs["kinds"] == ("4k",)
    assert "1v3_won" not in obs["kinds"]


def test_match_row_drops_highlight_boxes_and_fills_kinds():
    row = {
        "match_id": "2396941",
        "slug": "furia-vs-natus-vincere-blast-open-porto-2026",
        "match_stage": "Grand Final",
        "clips": [
            {**LATTO_CLIP, "opponent_team": "NaVi"},
            {
                **LATTO_CLIP,
                "clip_id": "box",
                "title": "M1R7 | latto — ACE",
                "label": "M1R7 | latto — ACE",
            },
        ],
    }
    obs = observations_from_match_row(row)
    assert len(obs) == 1
    assert obs[0]["kinds"] == ("1v3_won", "ace")
    assert obs[0]["opponent"] == "Natus Vincere"
    assert obs[0]["stage"] == "grand_final"


def test_esl_in_3_mins_is_not_a_clip_observation():
    assert observation_from_to({
        "video_id": "abc",
        "title": "FURIA vs NAVI | Match in 3 mins",
        "views": 152_000,
        "channel": "ESL CS2 Highlights",
    }) is None


def test_blast_highlights_1v3_ace_is_its_own_observation():
    obs = observation_from_to(
        {
            "video_id": "yt1",
            "title": "latto 1V3 Ace Clutch vs NaVi",
            "views": 18_000,
            "channel": "BLAST CS2 Highlights",
        },
        match={
            "slug": "furia-vs-natus-vincere-blast-open-porto-2026",
            "stage": "Grand Final",
            "teams": ("FURIA", "Natus Vincere"),
        },
        player_hint="latto",
        opponent_hint="NaVi",
        recognised={"latto": "76561198850020186"},
    )
    assert obs is not None
    assert obs["source"] == "blast_highlights"
    assert obs["views"] == 18_000
    assert obs["kinds"] == ("1v3_won", "ace")
    assert obs["player"] == "latto"
    assert obs["steamid"] == "76561198850020186"
    assert obs["opponent"] == "Natus Vincere"
    assert obs["stage"] == "grand_final"


def test_to_at_handle_is_not_opponent():
    obs = observation_from_to(
        {
            "video_id": "yt2",
            "title": "ZywOo 1v3",
            "views": 20_000,
            "channel": "BLAST",
        },
        match={"teams": ("Vitality", "FUT"), "stage": "Playoff"},
        opponent_hint="@TeamVitalityCS",
    )
    assert obs is not None
    assert obs["opponent"] is None
    assert obs["kinds"] == ("1v3_won",)


def test_unresolved_to_player_stays_unset_but_row_remains():
    obs = observation_from_to(
        {
            "video_id": "yt3",
            "title": "random 4K on Mirage",
            "views": 900,
            "channel": "PGL CS2 Highlights",
        },
        player_hint="notAPro",
        recognised={"latto": "76561198850020186"},
    )
    assert obs is not None
    assert obs["player"] is None
    assert obs["steamid"] is None
    assert obs["kinds"] == ("4k",)


def test_highlight_aliases_fold_onto_ranking_names():
    assert canonical_ranking_name("NIP") == "Ninjas in Pyjamas"
    assert canonical_ranking_name("mongolz") == "The MongolZ"
    assert canonical_ranking_name("natusvincere") == "Natus Vincere"


def test_cut_opponent_is_the_other_fixture_side():
    cut = {"pov_nick": "latto", "pov_team": "FURIA"}
    assert opponent_of_cut(cut, ["FURIA", "NaVi"]) == "Natus Vincere"
    assert opponent_of_cut({"opponent": "NaVi"}, ["FURIA", "Vitality"]) == "Natus Vincere"
    assert opponent_of_cut({"pov_team": "FURIA"}, ["FURIA", "NaVi"]) == "Natus Vincere"
    assert opponent_of_cut({}, ["FURIA", "NaVi"]) is None


def test_kinds_from_cut_five_clean_taps_are_ace_and_perfect_shots():
    assert kinds_from_cut({
        "short_type": "perfect_shots",
        "kill_ticks": [1, 2, 3, 4, 5],
    }) == ("ace", "perfect_shots")
    clutch = kinds_from_cut({
        "short_type": "clutch",
        "clutch_initial_count": "1v3",
        "kill_ticks": [1, 2, 3, 4, 5],
        "perfect_shots": True,
    })
    assert clutch == ("1v3_won", "ace", "perfect_shots")


def test_kinds_from_cut_stacks_flick():
    assert kinds_from_cut({
        "short_type": "flick",
        "kill_ticks": [1],
        "flick": True,
    }) == ("flick",)
    stacked = kinds_from_cut({
        "short_type": "clutch",
        "clutch_initial_count": "1v3",
        "kill_ticks": [1, 2, 3],
        "flick": True,
    })
    assert stacked == ("1v3_won", "flick")


def test_quickscope_label_is_not_flick():
    assert "flick" not in parse_kinds("m0NESY Mirage Quickscope")
    assert parse_kinds("insane flick") == ("flick",)


def test_talk_and_ewc_without_cs2_are_not_observations():
    assert observation_from_to({
        "video_id": "t1",
        "title": "Talent desk rumours after the Grand Final",
        "views": 10,
        "channel": "BLAST",
    }) is None
    assert observation_from_to({
        "video_id": "t2",
        "title": "Insane 1v3 clutch",
        "views": 10,
        "channel": "EWC Extra",
    }) is None
    assert observation_from_to({
        "video_id": "t3",
        "title": "Insane 1v3 clutch #cs2",
        "views": 10,
        "channel": "EWC Extra",
    }) is not None


def test_probe_slug_and_upsert_roundtrip(tmp_path):
    import json
    from shorts.scrape_allstar_hltv import _probe_slug, upsert_row
    path = tmp_path / "probe.jsonl"
    assert _probe_slug(path, "2396947") is None
    upsert_row(path, {"match_id": "2396947", "slug": "s1", "clips": []})
    assert _probe_slug(path, "2396947") == "s1"
    upsert_row(path, {"match_id": "2396947", "slug": "s2", "clips": [{"a": 1}]})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["slug"] == "s2"


def test_playlist_id_parsing():
    from shorts.scrape_allstar_hltv import _playlist_id
    html = '<iframe src="https://allstar.gg/iframe?playlist=6a9c957875f9014d58a945c1&x=1">'
    assert _playlist_id(html) == "6a9c957875f9014d58a945c1"
    assert _playlist_id("<html>no embeds here</html>") is None
