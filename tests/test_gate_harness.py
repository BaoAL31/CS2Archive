"""Tests for the gate eval harness: replay tally, ledger, forward metrics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cs2archive.faceit.gate_ledger as ledger
import cs2archive.faceit.replay_gate as replay
from cs2archive import scoring as _scoring
from cs2archive.faceit import gate_forward
from cs2archive.faceit.update_player_demand import breakout_map


def _row(video_id, player, channel, published, views, duration=600):
    return {
        "video_id": video_id,
        "primary_player": player,
        "channel": channel,
        "published_at": published,
        "views": str(views),
        "duration_seconds": str(duration),
    }


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ALIASES = {"donk": "donk", "s1mple": "s1mple", "quiet": "quiet"}


def _filler(channel, n, views, published="2026-06-01T00:00:00+00:00"):
    return [_row(f"fill-{channel}-{i}", "Someone Else", channel,
                 published, views) for i in range(n)]


def test_breakout_map_finds_spike_and_ignores_fresh_and_shorts():
    rows = _filler("chan-a", 10, 100) + [
        _row("spike", "donk", "chan-a", "2026-09-20T00:00:00+00:00", 20_000),
        _row("fresh", "s1mple", "chan-a", "2026-09-30T12:00:00+00:00", 50_000),
        _row("short", "quiet", "chan-a", "2026-09-20T00:00:00+00:00",
             50_000, duration=60),
        _row("old", "quiet", "chan-a", "2026-01-01T00:00:00+00:00", 99_999),
    ]
    got = breakout_map(rows, ALIASES, NOW)
    assert got["donk"] > 100.0
    assert "s1mple" not in got  # 1d old: under the 2d floor
    assert "quiet" not in got  # short excluded, old outside window


def test_breakout_map_unknown_player_ignored():
    rows = _filler("chan-a", 10, 100) + [
        _row("x", "new", "chan-a", "2026-09-20T00:00:00+00:00", 99_999),
    ]
    assert breakout_map(rows, ALIASES, NOW) == {}


def test_breakout_map_seven_day_floor_and_reach():
    # Big-channel baseline (~150 vpd): a 2-day-old 20k-view clip reads
    # ~67x unfloored (would clear the 30x bar) but ~19x 7d-floored.
    rows = _filler("chan-a", 10, 18000, published="2026-06-01T00:00:00+00:00") + [
        _row("fresh", "donk", "chan-a", "2026-09-29T00:00:00+00:00", 20000),
    ]
    assert breakout_map(rows, ALIASES, NOW) == {}
    # Lone video on its channel: no LOO baseline, rejected.
    assert breakout_map(
        [_row("lone", "donk", "chan-b", "2026-09-20T00:00:00+00:00", 99_999)],
        ALIASES, NOW) == {}
    # Lone video on its channel: no LOO baseline, rejected.
    assert breakout_map(
        [_row("lone", "donk", "chan-b", "2026-09-20T00:00:00+00:00", 99_999)],
        ALIASES, NOW) == {}


def test_has_recent_breakout_reads_section(tmp_path):
    from cs2archive import scoring as scoring

    payload = tmp_path / "demand.json"
    payload.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "breakouts": {"donk": 250.0},
        "method": {"rule_version": _scoring.DEMAND_RULE_VERSION},
    }), encoding="utf-8")
    assert scoring.has_recent_breakout("donk", payload) is True
    assert scoring.has_recent_breakout("s1mple", payload) is False
    assert scoring.has_recent_breakout("donk", tmp_path / "missing.json") is False


def test_picks_for_day_solo_dedupe_and_reasons():
    cands = [
        {"id": "m1:donk", "match_id": "m1", "player": "donk",
         "stream": "solo", "date": "2026-09-28", "weight": 500,
         "kills": 20, "deaths": 10, "kd": 2.0, "adr": 100.0, "map": "Mirage"},
        {"id": "m1:magixx", "match_id": "m1", "player": "magixx",
         "stream": "solo", "date": "2026-09-28", "weight": 400,
         "kills": 20, "deaths": 10, "kd": 2.0, "adr": 100.0, "map": "Mirage"},
        {"id": "m2:xyz", "match_id": "m2", "player": "xyz",
         "stream": "multi", "date": "2026-09-28", "weight": 900,
         "kills": 20, "deaths": 10, "kd": 2.0, "adr": 100.0, "map": "Mirage"},
        {"id": "m3:donk", "match_id": "m3", "player": "donk",
         "stream": "solo", "date": "2026-09-29", "weight": 500,
         "kills": 20, "deaths": 10, "kd": 2.0, "adr": 100.0, "map": "Mirage"},
    ]
    picks = replay.picks_for_day(
        cands, "2026-09-28", lambda nick: (True, "star"))
    # one per match (magixx loses m1), multi excluded, other day excluded
    assert [(p["player"], p["gate_reason"]) for p in picks] == [("donk", "star")]
    assert replay.picks_for_day(
        cands, "2026-09-28", lambda nick: (False, "reject")) == []


def test_roster_and_bands():
    payload = {
        "index": {"s1mple": 1.8, "quiet": 1.0},
        "players": {"s1mple": {"videos": 30, "recent_videos": 5},
                    "quiet": {"videos": 30, "recent_videos": 5}},
        "breakouts": {"donk": 250.0},
        "method": {"rule_version": _scoring.DEMAND_RULE_VERSION},
    }
    roster = replay.roster_for_payload(payload)
    assert roster == ["donk", "s1mple"]
    result = {"days": [{}, {}, {}],
              "summary": {"breakout-or": {"total": 3, "mean_per_day": 1.0,
                                          "zero_days": 1, "zero_share": 0.333,
                                          "over2_days": 0}}}
    lines = replay.check_bands(result)
    assert all(line.startswith("[PASS]") for line in lines)
    result["summary"]["breakout-or"]["mean_per_day"] = 2.5
    assert any(line.startswith("[FAIL]") for line in replay.check_bands(result))


def test_ledger_verdict_and_record(tmp_path, monkeypatch):
    import cs2archive.faceit.gate_ledger as gl

    monkeypatch.setattr(gl._scoring, "load_player_demand_index",
                        lambda *a, **k: {"s1mple": 1.8})
    monkeypatch.setattr(gl._scoring, "demand_star_supported",
                        lambda *a, **k: True)
    monkeypatch.setattr(gl._scoring, "load_demand_payload",
                        lambda *a, **k: {
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "breakouts": {"donk": 250.0},
                            "players": {
                                "s1mple": {"videos": 30, "recent_videos": 5}},
                            "method": {
                                "rule_version": _scoring.DEMAND_RULE_VERSION},
                        })
    v = gl.gate_verdict({"player": "s1mple", "match_id": "m",
                         "stream": "solo", "date": "2026-09-28"})
    assert v["verdict"] == "pass" and v["reason"] == "star(ix=1.8)"
    assert v["breakout_or"] is False and v["rule"].startswith("star-or-breakout@")
    v = gl.gate_verdict({"player": "donk", "match_id": "m",
                         "stream": "solo", "date": "2026-09-28"})
    assert v["verdict"] == "pass" and v["reason"] == "breakout(pi=250.0)"
    assert v["breakout_or"] is True
    v = gl.gate_verdict({"player": "nobody", "match_id": "m",
                         "date": "2026-09-28"})
    assert (v["verdict"], v["reason"]) == ("reject", "reject")
    path = tmp_path / "ledger.jsonl"
    gl.record_gate_scrape("test", [
        {"player": "s1mple", "match_id": "m", "date": "2026-09-28"},
        {"player": "donk", "match_id": "m3", "date": "2026-09-28"},
        {"player": "nobody", "match_id": "m2", "date": "2026-09-28"},
    ], path=path)
    lines = [json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "scrape" and lines[0]["qualified"] == 2
    assert {line.get("player") for line in lines[1:]} == {"s1mple", "donk"}
    assert all(line.get("event_id") for line in lines[1:])


def test_forward_perf_ratio():
    now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    comp = [_row(f"c{i}", "donk", "chan", "2026-06-01T00:00:00+00:00", 1220)
            for i in range(5)]
    base = gate_forward.competitor_median_vpd("donk", now=now, rows=comp)
    assert base == {"player": "donk", "videos": 5, "median_vpd": 10.0}
    own = [_row("v1", "donk", "CS2 Archive", "2026-09-20T00:00:00+00:00", 220),
           _row("v0", "donk", "CS2 Archive", "2026-09-20T00:00:00+00:00", 0)]
    got = gate_forward.own_video_pi("v1", now=now, own_rows=own, comp_rows=comp)
    assert got["perf_ratio"] == 2.0
    assert got["own_pi"] == 2.0  # 20 vpd vs own median 10
    assert gate_forward.own_video_pi("missing", own_rows=own)["error"]
    # unaliased rows are skipped, never scored
    assert gate_forward.own_video_pi(
        "v2", now=now,
        own_rows=[_row("v2", "Some Random", "CS2 Archive",
                       "2026-09-20T00:00:00+00:00", 999_999)],
        comp_rows=comp)["error"] == "unaliased player"
