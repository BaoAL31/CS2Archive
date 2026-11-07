from datetime import datetime, timedelta, timezone

from cs2archive.faceit.pov_trends import find_early_trends, read_early_trend, TREND_VERSION

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def row(vid, age, views, player="someone", channel="POV channel", capture=NOW):
    return dict(video_id=vid, published_at=(NOW - timedelta(hours=age)).isoformat(),
                captured_at=capture.isoformat(), views=views, duration_seconds=800,
                primary_player=player, channel=channel)


def peers():
    return [row(str(i), 72 + i, 6000) for i in range(6)]


def test_under_two_day_winner_is_detected_without_velocity_extrapolation():
    trends = find_early_trends(peers() + [row("new", 12, 16000, "ZywOo")], {"zywoo": "ZywOo"}, NOW)
    assert trends["zywoo"]["ratio"] > 2.6
    assert trends["zywoo"]["peer_videos"] == 6
    assert trends["zywoo"]["video_id"] == "new"


def test_rejects_first_hour_noise_low_volume_and_sparse_baselines():
    for candidate in (row("new", 1, 16000, "ZywOo"), row("new", 12, 200, "ZywOo")):
        assert find_early_trends(peers() + [candidate], {"zywoo": "ZywOo"}, NOW) == {}
    assert find_early_trends(peers()[:2] + [row("new", 12, 16000, "ZywOo")], {"zywoo": "ZywOo"}, NOW) == {}


def test_frozen_young_counts_are_not_relabelled_as_older_peer_evidence():
    frozen = [row(str(i), 72, 500, capture=NOW - timedelta(hours=70)) for i in range(6)]
    assert find_early_trends(frozen + [row("new", 12, 16000, "ZywOo")], {"zywoo": "ZywOo"}, NOW) == {}


def test_own_channel_shorts_unknown_players_and_stale_candidates_do_not_trigger():
    aliases = {"zywoo": "ZywOo"}
    candidates = [row("own", 12, 16000, "ZywOo", "CS2 Archive"),
                  row("stale", 12, 16000, "ZywOo", capture=NOW - timedelta(hours=5)),
                  dict(row("short", 12, 16000, "ZywOo"), duration_seconds=60),
                  row("unknown", 12, 16000, "unrecognised")]
    assert find_early_trends(peers() + candidates, aliases, NOW) == {}


def test_reader_expires_evidence_instead_of_reusing_old_trends():
    trends = find_early_trends(peers() + [row("new", 12, 16000, "ZywOo")], {"zywoo": "ZywOo"}, NOW)
    payload = {"method": {"early_trend_version": TREND_VERSION}, "early_trends": trends}
    assert read_early_trend(payload, "ZYWOO", NOW)
    assert read_early_trend(payload, "ZywOo", NOW + timedelta(hours=4)) is None


def test_both_live_pickers_and_ledger_accept_and_identify_early_demand(monkeypatch):
    from cs2archive import scoring
    from cs2archive.faceit import scrape_notable, gate_ledger
    from cs2archive.listener import daemon
    now = datetime.now(timezone.utc)
    record = dict(video_id="new", ratio=2.7, views=16000, peer_videos=6,
                  observed_at=now.isoformat(), expires_at=(now + timedelta(hours=4)).isoformat())
    payload = {"updated_at": now.isoformat(), "method": {
        "rule_version": scoring.DEMAND_RULE_VERSION, "early_trend_version": TREND_VERSION},
        "early_trends": {"zywoo": record}}
    monkeypatch.setattr(scoring, "load_demand_payload", lambda *a, **k: payload)
    monkeypatch.setattr(scoring, "load_player_demand_index", lambda *a, **k: {})
    assert scrape_notable.is_good_faceit_pov({"player": "ZywOo"})
    assert daemon._star_eligible("ZywOo")[0]
    verdict = gate_ledger.gate_verdict({"player": "ZywOo", "match_id": "example"})
    assert verdict["verdict"] == "pass"
    assert verdict["reason"].startswith("early_trend")
    assert verdict["early_trend"]["video_id"] == "new"


def test_new_demand_reconsiders_cached_star_gated_matches_without_downloading(monkeypatch, tmp_path):
    from cs2archive import scoring
    from cs2archive.listener import daemon
    import json
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(scoring, "load_demand_payload", lambda *a, **k: {"updated_at": "2026-10-28T04:00:00Z"})
    monkeypatch.setattr(daemon, "_star_eligible", lambda nick: (True, ""))
    match = daemon.Match("123", "https://www.hltv.org/matches/123/team-vs-other", "team-vs-other", "team", "other")
    folder = tmp_path / "backlog/123-team-vs-other/high"
    folder.mkdir(parents=True)
    card = folder / "zywoo-inferno.json"
    card.write_text(json.dumps({"player": "ZywOo", "map": "Inferno", "rating": 1.9}))
    state = daemon.State(tmp_path / "state.json")
    state.data["result_baseline_ids"] = ["123"]
    record = {"status": "completed", "skip_reason": "star gate", "demand_checked_at": "2026-10-28T02:00:00Z"}
    state.data["matches"]["123"] = record
    assert daemon._actionable_matches(state, [match]) == [match]
    daemon._recheck_star_gated_match(state, match, record, None)
    assert record["status"] == "queued"
    assert len(state.data["queue"]) == 1
    # Same demand snapshot cannot repeatedly action the match.
    record.update(status="completed", skip_reason="star gate")
    assert daemon._actionable_matches(state, [match]) == []


def test_refresh_evaluates_new_captures_after_scrape_completion(monkeypatch, tmp_path):
    from cs2archive.faceit import update_player_demand as demand
    clock = [NOW - timedelta(minutes=1)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    async def scrape(*args):
        clock[0] = NOW + timedelta(seconds=1)
        return peers() + [row("new", 12, 16000, "ZywOo")]
    monkeypatch.setattr(demand, "datetime", Clock)
    monkeypatch.setattr(demand, "scrape_new", scrape)
    monkeypatch.setattr(demand, "load_seed_rows", lambda: [])
    monkeypatch.setattr(demand, "HISTORY_PATH", tmp_path / "history.csv")
    monkeypatch.setattr(demand, "DEMAND_PATH", tmp_path / "demand.json")
    monkeypatch.setattr(demand, "recognised_aliases", lambda: {"zywoo": "ZywOo"})
    monkeypatch.setattr(demand, "analyze_rows", lambda *a, **k: {"method": {
        "videos_analyzed": 7, "performance_index": "test", "excluded_under_5_minutes": 0,
        "excluded_younger_than_2_days": 1}})
    monkeypatch.setattr(demand, "build_index", lambda *a: ({}, {}))
    assert demand.refresh()["early_trends"]["zywoo"]["video_id"] == "new"


def test_live_queue_prioritises_fresh_trend_then_rating(monkeypatch, tmp_path):
    from cs2archive import scoring
    from cs2archive.listener import daemon
    import json
    now = datetime.now(timezone.utc)
    record = dict(ratio=2.7, views=16000, peer_videos=6, observed_at=now.isoformat(),
                  expires_at=(now + timedelta(hours=4)).isoformat())
    payload = {"method": {"early_trend_version": TREND_VERSION}, "early_trends": {"zywoo": record}}
    monkeypatch.setattr(scoring, "load_demand_payload", lambda *a, **k: payload)
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    cards = []
    for player, rating in [("PR", 1.66), ("ZywOo", 1.58), ("NiKo", 1.8)]:
        name = f"{player}.json"
        (tmp_path / name).write_text(json.dumps({"player": player, "rating": rating}))
        cards.append(name)
    assert daemon._sort_queue(cards) == ["ZywOo.json", "NiKo.json", "PR.json"]
    record["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    assert daemon._sort_queue(cards) == ["NiKo.json", "PR.json", "ZywOo.json"]
