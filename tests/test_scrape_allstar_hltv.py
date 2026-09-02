"""Allstar playlist clips must identify player (steam64) and HLTV match."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.scrape_allstar_hltv import clip_from_allstar, clips_from_playlist_payload, match_stage_from_html


SAMPLE_CLIP = {
    "_id": "6a95e7c2a21320c31c2d71cf",
    "title": "Dust 2 1V3 Ace Clutch",
    "views": 146082,
    "username": "lattofps",
    "steamid": "76561198850020186",
    "playerGameIdentifier": "76561198850020186",
    "roundNumber": 4,
    "metadata": [
        {"key": "PARTNER_matchId", "value": "2396941"},
        {"key": "PARTNER_playerId", "value": "19045"},
        {"key": "PARTNER_playerName", "value": "latto"},
        {"key": "PARTNER_opponentTeamName", "value": "Vitality"},
        {"key": "CS_Situational", "value": "1V3"},
        {"key": "CS_Kill Count", "value": "5"},
    ],
}


def test_clip_keeps_steamid_hltv_nick_and_match():
    rec = clip_from_allstar(SAMPLE_CLIP)
    assert rec["steamid"] == "76561198850020186"
    assert rec["player"] == "latto"
    assert rec["match_id"] == "2396941"
    assert rec["title"] == "Dust 2 1V3 Ace Clutch"
    assert rec["label"] == "latto Dust 2 1V3 Ace Clutch"
    assert rec["views"] == 146082
    assert rec["round"] == 4
    assert rec["clip_id"] == "6a95e7c2a21320c31c2d71cf"
    assert rec["opponent_team"] == "Vitality"


def test_clip_keeps_hltv_nick_not_allstar_handle():
    raw = {**SAMPLE_CLIP, "metadata": [
        {"key": "PARTNER_matchId", "value": "2396941"},
        {"key": "PARTNER_playerName", "value": "latto"},
    ]}
    rec = clip_from_allstar(raw)
    assert rec["player"] == "latto"
    assert rec["player"] != "lattofps"


def test_clip_without_partner_nick_keeps_steam64_player_unset():
    raw = {**SAMPLE_CLIP, "metadata": []}
    rec = clip_from_allstar(raw)
    assert rec["steamid"] == "76561198850020186"
    assert rec["player"] is None
    assert rec["match_id"] is None
    assert rec["label"] == "Dust 2 1V3 Ace Clutch"


def test_playlist_payload_reads_data_clips_not_bare_title():
    payload = {"data": {"clips": [SAMPLE_CLIP, {"title": "noise"}]}}
    clips = clips_from_playlist_payload(payload)
    assert len(clips) == 1
    assert clips[0]["player"] == "latto"
    assert clips[0]["steamid"] == "76561198850020186"


def test_title_only_dict_is_not_a_clip():
    assert clip_from_allstar({"title": "Dust 2 1V3 Ace Clutch", "views": 99}) is None


def test_match_page_stage_comes_from_info_box_not_round():
    html = """
    <div class="match-info-box"><div class="text">Grand Final</div></div>
    <div class="map-info-wrap"><ul><li>Mirage</div></div>
    <div class="highlights">M1R7 | latto — ACE</div>
    """
    assert match_stage_from_html(html) == "Grand Final"
