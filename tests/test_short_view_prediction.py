import json
import math

import pytest

from cs2archive.shorts.allstar_data import load_allstar_dataset
from cs2archive.shorts.clip_observation import kinds_from_cut, parse_kinds
from cs2archive.shorts.eval_short_score import evaluate, match_fold
from cs2archive.shorts.fit_partial_stars import fit_partial_stars
from cs2archive.shorts.view_prediction import predict_log_views


def test_complete_saved_fit_reproduces_design_matrix_prediction():
    rows = [dict(views=100 + i * 30, steamid=str(i % 2), source="allstar" if i % 2 else "to",
                 stage="group", opponent="Spirit", kinds=["ace"], age_days=i)
            for i in range(12)]
    model = fit_partial_stars(rows, recognised={"0", "1"})
    restored = json.loads(json.dumps(model))
    row = rows[3]
    expected = (restored["intercept"] + restored["player"]["1"] + restored["source"]["allstar"]
                + restored["stage"]["group"] + restored["opponent"]["Spirit"]
                + restored["kind"]["ace"] + 3 * restored["clip_age"])
    assert predict_log_views(row, restored) == pytest.approx(expected)
    assert predict_log_views({**row, "kinds": ["ace", "ace"]}, restored) == pytest.approx(expected)


def test_three_kill_clutch_does_not_invent_a_multikill_label_bonus():
    assert kinds_from_cut(dict(short_type="clutch", clutch_initial_count="1v3", kill_ticks=[1, 2, 3])) == parse_kinds("1V3 Clutch")
    assert kinds_from_cut(dict(short_type="perfect_shots", kill_ticks=[1, 2, 3])) == ("3k", "perfect_shots")
    assert parse_kinds("1V3 3K Clutch") == ("1v3_won", "3k")  # Explicit compounds remain valid.


def test_allstar_rejects_wrong_fixture_before_inheriting_context_and_dedups(tmp_path):
    path = tmp_path / "obs.jsonl"
    def record(mid, views, observed):
        return dict(match_id="correct", slug="spirit-vs-furia-iem-katowice-2026",
                    event_slug="iem-katowice-2026", match_stage="Grand final", scraped_at=observed,
                    clips=[dict(clip_id="clip", steamid="1", player="p", match_id=mid,
                                title="ACE", views=views)])
    path.write_text("\n".join(json.dumps(r) for r in [
        record("wrong", 999999, "2026-10-05T00:00:00Z"),
        record("correct", 100, "2026-10-03T00:00:00Z"),
        record("correct", 200, "2026-10-04T00:00:00Z"),
        record("correct", 50, "2026-10-02T00:00:00Z"),
    ]), encoding="utf-8")
    rows, stats = load_allstar_dataset(path)
    assert len(rows) == 1
    assert rows[0]["views"] == 200
    assert rows[0]["stage"] == "grand_final"
    assert rows[0]["published_at"] is None  # Scrape time is not publication time.
    assert stats["fixture_conflicts"] == 1
    assert stats["duplicate_snapshots"] == 2


def test_grouped_evaluation_never_fits_test_match_and_has_train_only_baseline(monkeypatch):
    import cs2archive.shorts.eval_short_score as ev
    rows = [dict(match_id=str(i), views=math.exp(2 + i / 100), kinds=[], source="allstar")
            for i in range(30) for _ in range(2)]
    original = ev.fit_partial_stars
    train_groups = []
    def spy(train, **kwargs):
        train_groups.append({r["match_id"] for r in train})
        return original(train, **kwargs)
    monkeypatch.setattr(ev, "fit_partial_stars", spy)
    result = evaluate(rows, folds=3, recognised=set())
    assert result["model"]["n"] == len(rows)
    for fold, groups in enumerate(train_groups):
        assert all(match_fold(g, seed="shorts-v2", folds=3) != fold for g in groups)
    assert result["model"]["log_rmse"] == pytest.approx(result["geometric_baseline"]["log_rmse"])
