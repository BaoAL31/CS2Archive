import json
from datetime import datetime, timedelta, timezone

import pytest

from cs2archive.shorts.shorts_picker import (
    PickerState, candidate_id, picker_lock, render_timeline, scan_candidates, sydney_day,
)

MODEL = dict(intercept=5.0, player={"1": 0, "2": 2, "3": 3}, opponent={}, stage={},
             kind={}, source={"allstar": 0}, clip_age=0, reference_age_days=0,
             model_version=2, training_rows=30)
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def pool(tmp_path, multi=False):
    demo = tmp_path / "demo.dem"
    demo.touch()
    shorts = [dict(short_type="wallbang", pov_steam_id=str(i), pov_nick=f"p{i}",
                   start_tick=10, end_tick=20, kill_ticks=[15]) for i in (1, 2, 3)]
    if multi:
        folder = tmp_path / "renders" / "a"
        folder.mkdir(parents=True)
        (folder / "short_timeline.json").write_text(json.dumps(dict(demo_path=str(demo), shorts=shorts)))
    else:
        for i, short in enumerate(shorts):
            folder = tmp_path / "renders" / str(i)
            folder.mkdir(parents=True)
            (folder / "short_timeline.json").write_text(json.dumps(dict(demo_path=str(demo), shorts=[short])))
    return scan_candidates(tmp_path / "renders", MODEL, {"1", "2", "3"}, complete=lambda p: p.exists())[0]


def test_global_rank_beats_filesystem_order_and_has_demo_aware_identity(tmp_path):
    candidates = pool(tmp_path)
    assert [c.short["pov_steam_id"] for c in candidates] == ["3", "2", "1"]
    state = PickerState(tmp_path / "state.json")
    picked, _ = state.select(list(reversed(candidates)), now=NOW)
    assert [c.short["pov_steam_id"] for c in picked] == ["3", "2"]
    assert candidate_id(tmp_path / "other.dem", candidates[0].short) != candidates[0].key


def test_multi_cut_timeline_selects_individual_cuts_without_rendering_lower_rank(tmp_path):
    candidates = pool(tmp_path, multi=True)
    tl = render_timeline(candidates[0])
    assert len(json.loads(tl.read_text())["shorts"]) == 1
    assert json.loads(tl.read_text())["shorts"][0]["pov_steam_id"] == "3"
    assert len(json.loads(candidates[0].timeline.read_text())["shorts"]) == 3


def test_success_quota_failed_render_retry_expiry_and_next_day(tmp_path):
    candidates = pool(tmp_path)
    path = tmp_path / "state.json"
    state = PickerState(path)
    picked, _ = state.select(candidates, now=NOW)
    state.reserve(picked[0], now=NOW, model=MODEL)
    # A crash retains the reservation and prioritizes the identical cut.
    state = PickerState(path)
    resumed, _ = state.select(candidates, now=NOW)
    assert resumed[0].key == picked[0].key
    state.fail(picked[0].key, "driver reset")
    assert state.completed_today(NOW) == 0
    for candidate in picked:
        state.reserve(candidate, now=NOW, model=MODEL)
        state.finish(candidate.key, now=NOW)
    assert state.select(candidates, now=NOW)[0] == []
    assert len(state.select(candidates, now=NOW + timedelta(days=1))[0]) == 1
    _, stats = state.select(candidates, now=NOW + timedelta(days=8))
    assert stats["expired_stale"] == 1


def test_corrupt_ledger_and_concurrent_picker_fail_closed(tmp_path):
    ledger = tmp_path / "state.json"
    ledger.write_text('{"version": 2,')
    with pytest.raises(ValueError):
        PickerState(ledger)
    with picker_lock(ledger):
        with pytest.raises(OSError):
            with picker_lock(ledger):
                pass


def test_sydney_midnight_and_readonly_reconciliation(tmp_path):
    assert sydney_day(datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)) == "2026-10-06"
    candidates = pool(tmp_path)
    state = PickerState(tmp_path / "state.json")
    state.select(candidates, now=NOW)
    state.reserve(candidates[0], now=NOW, model=MODEL)
    before = state.path.read_bytes()
    candidates[0].video.touch()
    state.reconcile(lambda p: p.exists(), now=NOW, persist=False)
    assert state.entries[candidates[0].key]["state"] == "rendered"
    assert state.path.read_bytes() == before


def test_completed_multi_cut_meta_and_legacy_videos_are_not_rerendered(tmp_path):
    candidates = pool(tmp_path, multi=True)
    original = candidates[0].timeline.parent
    from cs2archive.shorts.output_paths import short_output_path
    short_output_path(original, candidates[0].short).touch()
    pending, stats = scan_candidates(tmp_path / "renders", MODEL, {"1", "2", "3"}, complete=lambda p: p.exists())
    assert candidates[0].key not in {c.key for c in pending}
    assert stats["rendered"] == 1
    (original / "upload_meta_shorts.json").write_text(json.dumps({"upload_status": "completed"}))
    pending, stats = scan_candidates(tmp_path / "renders", MODEL, {"1", "2", "3"}, complete=lambda p: p.exists())
    assert pending == []
    assert stats["uploaded_or_skipped"] == 3


def test_missing_candidate_and_abandoned_reservation_expire_with_reason(tmp_path):
    candidates = pool(tmp_path)
    state = PickerState(tmp_path / "state.json")
    state.select(candidates, now=NOW)
    state.reserve(candidates[0], now=NOW, model=MODEL)
    picks, stats = state.select([], now=NOW + timedelta(days=8))
    assert picks == []
    assert stats["absent_expired"] == 3
    assert all(e["state"] == "expired" and e["reason"] == "expired_stale" for e in state.entries.values())


def test_picker_cli_failure_retry_and_repeated_passes_enforce_two_successes(tmp_path, monkeypatch):
    import sys
    from cs2archive.shorts import demand_gate, fit_partial_stars, render_pending_shorts as runner
    from cs2archive.shorts import scrape_allstar_hltv
    from cs2archive.shorts.output_paths import short_output_path
    candidates = pool(tmp_path)
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(runner, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_any_blocking", lambda: [])
    monkeypatch.setattr(runner, "_complete", lambda p: p.exists())
    monkeypatch.setattr(demand_gate, "load_partial_stars", lambda: MODEL)
    monkeypatch.setattr(fit_partial_stars, "_recognised_steamids", lambda: {"1", "2", "3"})
    monkeypatch.setattr(scrape_allstar_hltv, "load_ratings_stages", lambda: {})
    from cs2archive.shorts import allstar_selector
    monkeypatch.setattr(allstar_selector, "rank_candidates_by_allstar",
                        lambda cands, rows=None: ([(c, {"views": int(c.short["pov_steam_id"])})
                                                   for c in sorted(cands, key=lambda c: c.short["pov_steam_id"], reverse=True)], {}))
    calls = []
    def render(timeline, _args):
        short = json.loads(timeline.read_text())["shorts"][0]
        calls.append(short["pov_steam_id"])
        if len(calls) == 1:
            return 1  # First attempt fails; no quota is consumed.
        short_output_path(timeline.parent, short).touch()
        return 0
    monkeypatch.setattr(runner, "render_one", render)
    monkeypatch.setattr(sys, "argv", ["picker", "--ledger", str(ledger)])
    assert runner.main() == 1
    assert runner.main() == 0
    assert runner.main() == 0
    assert calls == ["3", "2", "3"]  # The lowest prediction was never rendered.
    state = PickerState(ledger)
    assert sum(e["state"] == "rendered" for e in state.entries.values()) == 2
    before = ledger.read_bytes()
    monkeypatch.setattr(sys, "argv", ["picker", "--ledger", str(ledger), "--dry-run"])
    assert runner.main() == 0
    assert ledger.read_bytes() == before
    assert candidates[-1].key in state.entries


def test_picker_uses_fixture_fallback_with_one_unranked_team(tmp_path):
    demo = tmp_path / "unknown-lan-team-vs-natus-vincere-m1-nuke.dem"
    demo.touch()
    folder = tmp_path / "renders" / "pov"
    folder.mkdir(parents=True)
    short = dict(short_type="wallbang", pov_steam_id="1", pov_nick="p1", pov_team="Unknown Lan Team",
                 start_tick=1, end_tick=20, kill_ticks=[15])
    (folder / "short_timeline.json").write_text(json.dumps(dict(demo_path=str(demo), shorts=[short])))
    candidates, _ = scan_candidates(tmp_path / "renders", MODEL, {"1"}, complete=lambda p: False)
    assert candidates[0].features["opponent"] == "Natus Vincere"


def test_loop_retries_failed_attempt_without_consuming_daily_slots(tmp_path, monkeypatch):
    import sys
    from cs2archive.shorts import demand_gate, fit_partial_stars, render_pending_shorts as runner
    from cs2archive.shorts import scrape_allstar_hltv
    from cs2archive.shorts.output_paths import short_output_path
    pool(tmp_path)
    ledger = tmp_path / "loop-state.json"
    monkeypatch.setattr(runner, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_any_blocking", lambda: [])
    monkeypatch.setattr(runner, "_complete", lambda p: p.exists())
    monkeypatch.setattr(demand_gate, "load_partial_stars", lambda: MODEL)
    monkeypatch.setattr(fit_partial_stars, "_recognised_steamids", lambda: {"1", "2", "3"})
    monkeypatch.setattr(scrape_allstar_hltv, "load_ratings_stages", lambda: {})
    from cs2archive.shorts import allstar_selector
    monkeypatch.setattr(allstar_selector, "rank_candidates_by_allstar",
                        lambda cands, rows=None: ([(c, {"views": int(c.short["pov_steam_id"])})
                                                   for c in sorted(cands, key=lambda c: c.short["pov_steam_id"], reverse=True)], {}))
    calls = []
    def render(timeline, _args):
        short = json.loads(timeline.read_text())["shorts"][0]
        calls.append(short["pov_steam_id"])
        if len(calls) == 1:
            return 1
        short_output_path(timeline.parent, short).touch()
        return 0
    class StopLoop(Exception):
        pass
    sleeps = []
    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            raise StopLoop
    monkeypatch.setattr(runner, "render_one", render)
    monkeypatch.setattr(runner.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["picker", "--ledger", str(ledger), "--loop"])
    with pytest.raises(StopLoop):
        runner.main()
    assert calls == ["3", "2", "3"]
    assert sum(e["state"] == "rendered" for e in PickerState(ledger).entries.values()) == 2
