"""Backfill linkage rules: verified-only, single candidate, explicit ambiguity."""

from __future__ import annotations

from cs2archive.shorts.social.backfill import (
    STATUS_AMBIGUOUS, STATUS_LINKED, STATUS_NO_CANDIDATE, STATUS_NO_MAP,
    RoundFacts, demo_mtime_index, parse_signature, resolve_short,
)

STEAM = "76561199032006224"


def _round(demo, *, map_name="Inferno", round_no=3, kills=4, won=True,
           steamid=STEAM, teams=(), clutch=None):
    return RoundFacts(
        demo_path=demo, match_id="1", map=map_name, round=round_no,
        kill_ticks=[1000 + i for i in range(kills)], player_steamid=steamid,
        won=won, teams=teams, clutch_size=clutch, kill_count=kills)


def test_parse_signature_reads_map_kills_and_opponent():
    sig = parse_signature("makeazze's 4K vs Spirit on Inferno #cs2")
    assert sig["map"] == "inferno"
    assert sig["kills"] == 4 and sig["kind"] == "4k"
    assert sig["opponent"] == "spirit"
    assert parse_signature("donk casually drops an ACE")["kills"] == 5
    assert parse_signature("JBa 1v3 clutch")["clutch_size"] == 3


def test_title_only_is_never_accepted():
    result = resolve_short(title="insane play by donk", published_at=None,
                           player_steamid=STEAM, rounds=[])
    assert result.status == STATUS_NO_CANDIDATE
    assert "title-only" in result.reason


def test_missing_map_is_rejected():
    result = resolve_short(title="donk 4K", published_at=None,
                           player_steamid=STEAM,
                           rounds=[_round("demos/hltv/1-a-vs-b-m1-inferno/x.dem")])
    assert result.status == STATUS_NO_MAP
    assert "no map" in result.reason


def test_single_qualifying_round_is_exact():
    rounds = [
        _round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=3, kills=4),
        _round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=7, kills=2),
    ]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_LINKED
    assert result.chosen is not None and result.chosen.round == 3
    assert result.chosen.kill_count == 4


def test_two_qualifying_rounds_are_ambiguous_with_candidates():
    rounds = [
        _round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=3, kills=4),
        _round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=9, kills=4),
    ]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_AMBIGUOUS
    assert len(result.candidates) == 2
    assert result.chosen is None
    assert "exactly one" in result.reason


def test_two_demos_hitting_the_same_signature_are_ambiguous():
    rounds = [
        _round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=3, kills=4),
        _round("demos/hltv/2-c-vs-d-m1-inferno/y.dem", round_no=3, kills=4),
    ]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_AMBIGUOUS
    assert {c.demo_path for c in result.candidates} == {
        "demos/hltv/1-a-vs-b-m1-inferno/x.dem",
        "demos/hltv/2-c-vs-d-m1-inferno/y.dem"}


def test_wrong_kill_signature_is_rejected():
    rounds = [_round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", round_no=3, kills=3)]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_NO_CANDIDATE
    assert "kills=4" in result.reason


def test_map_mismatch_is_rejected():
    rounds = [_round("demos/hltv/1-a-vs-b-m1-mirage/x.dem", map_name="Mirage",
                     round_no=3, kills=4)]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_NO_CANDIDATE


def test_other_player_is_rejected():
    rounds = [_round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", kills=4,
                     steamid="76561190000000001")]
    result = resolve_short(title="donk 4K on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_NO_CANDIDATE


def test_lost_clutch_round_is_rejected():
    rounds = [_round("demos/hltv/1-a-vs-b-m1-inferno/x.dem", kills=3, won=False,
                     clutch=3)]
    result = resolve_short(title="NiKo 1v3 clutch on Inferno", published_at=None,
                           player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_NO_CANDIDATE
    won = resolve_short(title="NiKo 1v3 clutch on Inferno", published_at=None,
                        player_steamid=STEAM,
                        rounds=[_round("demos/hltv/1-a-vs-b-m1-inferno/x.dem",
                                       kills=3, won=True, clutch=3)])
    assert won.status == STATUS_LINKED


def test_opponent_disambiguates_two_matches():
    rounds = [
        _round("demos/hltv/1-a-vs-spirit-m1-inferno/x.dem", round_no=3, kills=4,
               teams=("spirit",)),
        _round("demos/hltv/2-a-vs-furia-m1-inferno/y.dem", round_no=3, kills=4,
               teams=("furia",)),
    ]
    result = resolve_short(title="donk 4K vs Spirit on Inferno",
                           published_at=None, player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_LINKED
    assert result.chosen.demo_path.endswith("1-a-vs-spirit-m1-inferno/x.dem")


def test_demo_must_exist_before_publication(tmp_path):
    from cs2archive.shorts.social.backfill import _window_ok

    ok, why = _window_ok("2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z", 60)
    assert ok and why in {"window-ok", "no-date-evidence"}
    ok, why = _window_ok("2026-09-01T00:00:00Z", "2026-08-10T00:00:00Z", 60)
    assert not ok and why == "demo-local-after-publication"
    ok, why = _window_ok("2026-01-01T00:00:00Z", "2026-08-10T00:00:00Z", 30)
    assert not ok and "older-than" in why
    ok, _ = _window_ok(None, "2026-08-10T00:00:00Z", 60)
    assert ok


def test_demo_mtime_index_is_stable(tmp_path):
    demo = tmp_path / "m1.dem"
    demo.write_bytes(b"x")
    index = demo_mtime_index([str(demo)])
    assert str(demo) in index and index[str(demo)].endswith("Z")


def test_demo_teams_reads_both_sides_of_the_fixture():
    from cs2archive.shorts.social.backfill import demo_teams

    assert demo_teams("demos/hltv/2398717-falcons-vs-tyloo-esl-pro-league-season-24/"
                      "falcons-vs-tyloo-m1-mirage.dem") == ("falcons", "tyloo")
    assert demo_teams("demos/hltv/2400001-the-mongolz-vs-mouz-blast/x.dem") == \
        ("mongolz", "mouz")
    assert demo_teams("demos/hltv/faceit/x.dem") == ()  # no fixture in the name


def test_named_opponent_that_is_absent_from_the_demo_is_rejected():
    """The lesson from the first live backfill run: map+player+kills is not enough."""
    rounds = [_round("demos/hltv/2399005-aurora-vs-spirit-esl-pro-league-season-24/"
                     "aurora-vs-spirit-m2-anubis.dem",
                     map_name="Anubis", round_no=14, kills=4,
                     teams=("aurora", "spirit"))]
    result = resolve_short(title="donk's 4K against Liquid on Anubis #cs2",
                           published_at=None, player_steamid=STEAM, rounds=rounds)
    assert result.status == STATUS_NO_CANDIDATE
    assert "kills=4" in result.reason
    # The same round is accepted when the named opponent really is in the demo.
    ok = resolve_short(title="donk's 4K against Spirit on Anubis #cs2",
                       published_at=None, player_steamid=STEAM, rounds=rounds)
    assert ok.status == STATUS_LINKED


def test_rounds_from_timeline_populates_teams_from_the_folder(tmp_path):
    from cs2archive.shorts.social.backfill import rounds_from_timeline

    timeline = {
        "map": "Dust2",
        "pros": [{"steam_id": STEAM, "name": "zont1x"}],
        "kills_all": [{"tick": 100 + i, "round": 12, "attacker_steam_id": STEAM,
                       "attacker_team_num": 2} for i in range(4)],
        "winner_by_round": {12: 2},
        "rounds": [{"round": 12, "start_tick": 50, "end_tick": 900}],
    }
    facts = rounds_from_timeline(
        timeline,
        demo_path="demos/hltv/2399006-spirit-vs-m80-esl-pro-league-season-24/"
                  "spirit-vs-m80-m1-dust2.dem",
        steamid=STEAM)
    assert len(facts) == 1
    assert facts[0].teams == ("spirit", "m80")
    assert facts[0].won is True


def test_opponent_phrasing_variants_and_non_teams():
    assert parse_signature("donk's 4K against Liquid on Anubis #cs2")["opponent"] == \
        "liquid"
    assert parse_signature("4K vs G2 on Dust2")["opponent"] == "g2"
    assert parse_signature("4K vs. Spirit")["opponent"] == "spirit"
    # Ranked/ELO phrasing names no opponent, so it must not constrain anything.
    assert parse_signature("kyousuke 4K against 3.1k ELOs")["opponent"] is None
    assert parse_signature("4K against level 10 faceit players")["opponent"] is None
