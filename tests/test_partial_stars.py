"""Partial-star fit: 180-day window, no wipe, intercept + Internal factors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.demand_gate import candidate_score
from shorts.fit_partial_stars import (
    DAILY_NEW_MATCHES,
    fit_partial_stars,
    listener_holds_cloak,
    refresh_partial_stars,
    rows_in_window,
)


def test_daily_match_budget_is_between_10_and_20():
    assert 10 <= DAILY_NEW_MATCHES <= 20


def test_rows_older_than_180_days_are_dropped_at_fit_time():
    rows = [
        {"views": 100, "age_days": 10, "kinds": ["ace"]},
        {"views": 1_000_000, "age_days": 181, "kinds": ["ace"]},
    ]
    kept = rows_in_window(rows)
    assert [r["age_days"] for r in kept] == [10]
    stars = fit_partial_stars(rows)
    # Old mega-clip is out; remaining ACE is baseline (one row → intercept only).
    assert stars["kind"] == {}


def test_fit_does_not_put_source_or_age_on_candidate_score():
    rows = [
        {
            "views": 100,
            "age_days": 5,
            "source": "allstar",
            "steamid": "1",
            "kinds": ["ace"],
        },
        {
            "views": 800,
            "age_days": 5,
            "source": "blast_highlights",
            "steamid": "1",
            "kinds": ["ace"],
        },
        {
            "views": 50,
            "age_days": 5,
            "source": "allstar",
            "steamid": "2",
            "kinds": ["4k"],
        },
    ]
    stars = fit_partial_stars(rows)
    cut = {
        "pov_steam_id": "1",
        "short_type": "4k",
        "kill_ticks": [1, 2, 3, 4, 5],
        "source": "allstar",
        "clip_age": 5,
    }
    assert "source" not in stars
    assert candidate_score(cut, stars) == candidate_score(
        {k: v for k, v in cut.items() if k not in {"source", "clip_age"}},
        stars,
    )


def test_youtube_views_refresh_by_stored_video_id():
    from shorts.fit_partial_stars import refresh_youtube_views

    rows = [
        {"clip_id": "yt1", "source": "blast_highlights", "views": 10},
        {"clip_id": "yt2", "source": "allstar", "views": 50},
    ]
    updated = refresh_youtube_views(rows, {"yt1": 18000})
    assert updated[0]["views"] == 18_000
    assert updated[1]["views"] == 50


def test_stored_clips_take_stage_from_row_match_stage(tmp_path: Path):
    from shorts.fit_partial_stars import observations_from_allstar_jsonl

    jsonl = tmp_path / "obs.jsonl"
    jsonl.write_text(json.dumps({
        "match_id": "1",
        "match_stage": "Quarter-final",
        "clips": [{
            "source": "allstar",
            "clip_id": "c1",
            "steamid": "1",
            "kinds": ["ace"],
            "stage": None,
            "views": 100,
        }],
    }) + "\n", encoding="utf-8")
    rows = observations_from_allstar_jsonl(jsonl)
    assert rows[0]["stage"] == "playoff"


def test_known_stages_fill_empty_match_rows():
    from shorts.scrape_allstar_hltv import apply_known_stages

    rows = [
        {"match_id": "1", "slug": "a-vs-b-x", "match_stage": None},
        {"match_id": "2", "slug": "c-vs-d-x", "match_stage": "Grand final"},
    ]
    n = apply_known_stages(rows, {"a-vs-b-x": "Swiss round"})
    assert n == 1
    assert rows[0]["match_stage"] == "Swiss round"
    assert rows[1]["match_stage"] == "Grand final"


def test_refresh_writes_stars_without_deleting_observation_store(tmp_path: Path):
    jsonl = tmp_path / "obs.jsonl"
    jsonl.write_text(json.dumps({
        "match_id": "1",
        "slug": "furia-vs-natus-vincere-x",
        "match_stage": "Grand Final",
        "clips": [{
            "clip_id": "c1",
            "steamid": "76561198850020186",
            "player": "latto",
            "match_id": "1",
            "title": "Dust 2 1V3 Ace Clutch",
            "label": "latto Dust 2 1V3 Ace Clutch",
            "views": 1000,
            "opponent_team": "NaVi",
        }],
    }) + "\n", encoding="utf-8")
    out = tmp_path / "partial_stars.json"
    stars = refresh_partial_stars(
        jsonl=jsonl, out_path=out, to_jsonl=tmp_path / "to.jsonl",
    )
    assert jsonl.is_file()
    assert json.loads(jsonl.read_text(encoding="utf-8"))["clips"]
    assert out.is_file()
    assert "intercept" in stars
    assert stars["_rows"] == 1


def test_refresh_updates_to_views_by_video_id(tmp_path: Path):
    allstar = tmp_path / "obs.jsonl"
    allstar.write_text("", encoding="utf-8")
    to_path = tmp_path / "to.jsonl"
    to_path.write_text(json.dumps({
        "source": "blast_highlights",
        "clip_id": "abcdefghijk",
        "views": 10,
        "kinds": ["ace"],
    }) + "\n", encoding="utf-8")
    out = tmp_path / "partial_stars.json"
    refresh_partial_stars(
        jsonl=allstar,
        out_path=out,
        to_jsonl=to_path,
        views_by_id={"abcdefghijk": 18_000},
    )
    saved = json.loads(to_path.read_text(encoding="utf-8"))
    assert saved["views"] == 18_000
    assert allstar.read_text(encoding="utf-8") == ""


def test_player_partial_star_is_recognised_pro_only():
    rows = [
        {"views": 10, "age_days": 1, "steamid": "999", "kinds": ["ace"]},
        {"views": 10_000, "age_days": 1, "steamid": "999", "kinds": ["4k"]},
        {"views": 80, "age_days": 1, "steamid": "76561198000000001", "kinds": ["ace"]},
        {"views": 90, "age_days": 1, "steamid": "76561198000000001", "kinds": ["4k"]},
    ]
    stars = fit_partial_stars(rows, recognised={"76561198000000001"})
    assert "999" not in stars["player"]
    assert "76561198000000001" in stars["player"]


def test_listener_urls_come_before_popular_backfill():
    from shorts.scrape_allstar_hltv import prioritize_pending

    pending = prioritize_pending(
        [
            {"match_id": "archive-1", "url": "https://hltv.org/matches/1"},
            {"match_id": "live-9", "url": "https://hltv.org/matches/9"},
        ],
        done=set(),
        listener_rows=[{"match_id": "live-9", "url": "https://hltv.org/matches/9"}],
        max_matches=2,
    )
    assert [row["match_id"] for row in pending] == ["live-9", "archive-1"]


def test_listener_skips_non_popular_events(tmp_path: Path):
    from shorts.scrape_allstar_hltv import _listener_unseen

    path = tmp_path / "hltv.json"
    path.write_text(json.dumps({
        "matches": {
            "9": {
                "match": {
                    "url": "https://www.hltv.org/matches/9/x",
                    "slug": "a-vs-b-cct-season-3",
                    "event": "CCT Season 3",
                }
            },
            "8": {
                "match": {
                    "url": "https://www.hltv.org/matches/8/x",
                    "slug": "furia-vs-vitality-blast-open-porto-2026",
                    "event": "BLAST Open Porto 2026",
                }
            },
        }
    }), encoding="utf-8")
    rows = _listener_unseen(set(), path)
    assert [r["match_id"] for r in rows] == ["8"]


def test_cloudflare_rows_are_not_done(tmp_path: Path):
    from shorts.scrape_allstar_hltv import _load_done

    path = tmp_path / "obs.jsonl"
    path.write_text(
        json.dumps({"match_id": "ok", "cloudflare": None}) + "\n"
        + json.dumps({"match_id": "cf", "cloudflare": "timeout"}) + "\n",
        encoding="utf-8",
    )
    assert _load_done(path) == {"ok"}


def test_listener_lock_blocks_harvest(tmp_path: Path):
    lock = tmp_path / "hltv.json.lock"
    lock.write_bytes(b"x")
    import msvcrt
    with lock.open("a+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            assert listener_holds_cloak(lock) is True
        finally:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
