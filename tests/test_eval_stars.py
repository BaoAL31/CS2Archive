"""Seams for the star-system evaluator (competitor POV performance ground truth)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cs2archive.hltv.eval_stars import (
    NEUTRAL_STAR,
    attach_performance,
    evaluate,
    fit_index_at,
    format_report,
    gate_block,
    load_videos,
    permutation_p,
    precision_at_k,
    predict_star,
    split_rows,
    usable_rows,
)

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
# Players are looked up through aliases; a private table keeps the test
# independent of .data/player_accounts.json drift.
ALIASES = {"hero": "Hero", "bust": "Bust", "late": "Late", "bench": "Bench"}


def _row(player: str, channel: str, vpd: float, published: datetime, *,
         duration: int = 600, videos: int = 1) -> list[dict]:
    return [
        {
            "video_id": f"{player}-{channel}-{published.date()}-{i}",
            "channel": channel,
            "primary_player": player,
            "captured_at": "2026-09-01T00:00:00+00:00",
            "published_at": published.isoformat(),
            "duration_seconds": str(duration),
            "subscribers": "1000",
            "views": str(int(vpd)),
            "views_per_day": str(vpd),
            "age_days": str((NOW - published).total_seconds() / 86400),
        }
        for i in range(videos)
    ]


def _frame(days_before_cutoff_hero: float = 10.0):
    """Fit fold: Hero runs 3x the Bust baseline in a shared channel (so the
    channel median puts Hero at 1.5x and Bust at 0.5x). Held-out: Hero cools."""
    cutoff = NOW - timedelta(days=30)
    rows = []
    for i in range(10):
        rows += _row("hero", "chan", 3000.0, cutoff - timedelta(days=days_before_cutoff_hero + i))
        rows += _row("bust", "chan", 1000.0, cutoff - timedelta(days=days_before_cutoff_hero + i))
    for i in range(6):
        rows += _row("hero", "chan", 600.0, cutoff + timedelta(days=i + 1))
        rows += _row("bust", "chan", 900.0, cutoff + timedelta(days=i + 1))
        rows += _row("late", "chan", 5000.0, cutoff + timedelta(days=i + 1))
        rows += _row("bench", "chan", 800.0, cutoff + timedelta(days=i + 1))
    return rows, cutoff


def test_usable_rows_drops_short_young_and_unknown():
    published = NOW - timedelta(days=5)
    rows = (
        _row("hero", "chan", 1000.0, published)
        + _row("hero", "chan", 1000.0, published, duration=120)
        + _row("hero", "chan", 1000.0, NOW - timedelta(hours=6))
        + _row("stranger", "chan", 1000.0, published)
    )
    kept = usable_rows(rows, aliases=ALIASES)
    assert [row["player"] for row in kept] == ["Hero"]


def test_attach_performance_normalises_within_channel():
    published = NOW - timedelta(days=5)
    rows = usable_rows(
        _row("hero", "a", 3000.0, published, videos=1)
        + _row("bust", "a", 1000.0, published, videos=3)
        + _row("hero", "b", 100.0, published, videos=4),
        aliases=ALIASES,
    )
    scored = attach_performance(rows)
    by_key = {(row["player"], row["channel"]): row for row in scored}
    assert by_key[("Bust", "a")]["performance_index"] == pytest.approx(1.0)
    assert by_key[("Hero", "a")]["performance_index"] == pytest.approx(3.0)
    assert by_key[("Hero", "b")]["performance_index"] == pytest.approx(1.0)
    # channel-relative top quartile: only the 3x video in channel "a"
    assert by_key[("Hero", "a")]["channel_top_quartile"] is True


def test_predict_star_is_casefolded_and_neutral_when_absent():
    index = {"hero": 1.5}
    assert predict_star(index, {"player": "Hero"}) == 1.5
    assert predict_star(index, {"player": "Nobody"}) == NEUTRAL_STAR


def test_split_rows_is_strict_and_disjoint():
    rows, cutoff = _frame()
    prepared = usable_rows(rows, aliases=ALIASES)
    fit, holdout = split_rows(prepared, cutoff)
    assert fit and holdout
    assert len(fit) + len(holdout) == len(prepared)
    assert max(row["published_at"] for row in fit) < min(row["published_at"] for row in holdout)


def test_fit_index_ignores_post_cutoff_rows():
    rows, cutoff = _frame()
    # Deliberately hand it the WHOLE frame: the fit must drop future rows itself.
    index, details = fit_index_at(usable_rows(rows, aliases=ALIASES), cutoff,
                                  aliases=ALIASES)
    assert "late" not in index, "post-cutoff rows leaked into the star fit"
    # Channel median is shared, so Hero lands at 1.5x and Bust at 0.5x.
    assert index["hero"] == 1.5
    assert index.get("bust") is None  # under the 1.08 floor
    assert details["Hero"]["videos"] == 10
    assert details["Bust"]["median_performance_index"] == 0.5


def test_evaluate_time_split_reports_and_does_not_use_future_stars():
    rows, _cutoff = _frame()
    result = evaluate(rows, eval_days=30, now=NOW, aliases=ALIASES)
    assert result["mode"] == "time-split"
    assert result["protocol"]["circular"] is False
    assert result["fit"]["videos"] == 20
    assert result["holdout"]["videos"] == 24
    assert set(result) >= {"correlations", "gate", "precision_at_k", "bands",
                           "sweep", "players", "player_level", "caveats"}
    # Hero's star is fitted on its hot pre-cutoff run, graded on the cold run.
    assert result["gate"]["flagged"] > 0
    assert result["correlations"]["star_all"]["n"] == 24
    players = {item["player"].casefold(): item for item in result["players"]}
    assert players["late"]["star"] is None, "a post-cutoff-only player must be unrated"
    assert players["hero"]["star"] == 1.5


def test_evaluate_rejects_thin_holdout_and_short_window():
    with pytest.raises(ValueError):
        evaluate(_frame()[0], eval_days=7, now=NOW, aliases=ALIASES)
    with pytest.raises(ValueError):
        evaluate(_frame()[0], eval_days=200, now=NOW, aliases=ALIASES)


def test_gate_block_math():
    rows = [
        {"player": "Hero", "performance_index": 2.0, "channel_top_quartile": True},
        {"player": "Hero", "performance_index": 1.0, "channel_top_quartile": False},
        {"player": "Bust", "performance_index": 1.0, "channel_top_quartile": False},
        {"player": "Bust", "performance_index": 2.0, "channel_top_quartile": True},
    ]
    index = {"hero": 1.5, "bust": 1.0}
    details = {
        "Hero": {"videos": 30, "recent_videos": 5},
        "Bust": {"videos": 30, "recent_videos": 5},
    }
    block = gate_block(rows, index, threshold=1.25, details=details)
    assert (block["flagged"], block["base_rate"]) == (2, 0.5)
    assert block["precision"] == pytest.approx(0.5)
    assert block["recall"] == pytest.approx(0.5)
    assert block["lift"] == pytest.approx(1.0)


def test_precision_at_k_ties_break_without_peeking_at_target():
    rows = [
        {"player": name, "performance_index": 1.0, "channel_top_quartile": hit}
        for name, hit in (("Aaa", True), ("Bbb", True), ("Ccc", False), ("Ddd", False))
    ]
    index = {name.casefold(): 1.5 for name, _ in (("Aaa", 1), ("Bbb", 1), ("Ccc", 1), ("Ddd", 1))}
    block = precision_at_k(rows, index, ks=(2,))
    # Alphabetical tie order picks Aaa+Bbb; had the target leaked it would be 1.0.
    assert block[0]["k"] == 2
    assert block[0]["precision"] == pytest.approx(1.0)
    block = precision_at_k(
        [dict(row, channel_top_quartile=row["player"] in {"Ccc", "Ddd"}) for row in rows],
        index, ks=(2,),
    )
    assert block[0]["precision"] == pytest.approx(0.0)


def test_permutation_p_detects_and_clears_rank_order():
    import random

    rng = random.Random(3)
    xs = [float(i) for i in range(12)]
    ys = [float(i) for i in range(12)]
    assert permutation_p(xs, ys, permutations=200) < 0.02
    noise = [rng.random() for _ in range(12)]
    assert permutation_p(xs, noise, permutations=200) > 0.05


def test_load_videos_dedupes_on_video_id(tmp_path):
    csv_path = tmp_path / "history.csv"
    csv_path.write_text(
        "captured_at,video_id,channel,primary_player,title,published_at,views,"
        "duration_seconds,subscribers\n"
        "2026-09-01T00:00:00+00:00,v1,chan,hero,t,2026-09-01T00:00:00+00:00,100,600,10\n"
        "2026-09-20T00:00:00+00:00,v1,chan,hero,t,2026-09-01T00:00:00+00:00,900,600,10\n"
        "2026-09-20T00:00:00+00:00,v2,chan,hero,t,2026-09-02T00:00:00+00:00,50,600,10\n",
        encoding="utf-8",
    )
    rows = load_videos(history_path=csv_path, now=NOW)
    assert len(rows) == 2
    assert {row["video_id"]: float(row["views"]) for row in rows} == {"v1": 900.0, "v2": 50.0}


def test_format_report_renders_time_split_and_flags_circular_mode():
    rows, _cutoff = _frame()
    text = format_report(evaluate(rows, eval_days=30, now=NOW, aliases=ALIASES))
    assert "mode=time-split" in text
    assert "CIRCULAR" not in text
    assert "player level" in text
