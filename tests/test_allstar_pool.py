"""Allstar pool: snapshot history, incremental scrape parsing, eligibility."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from cs2archive.shorts.allstar_pool import (
    DEFAULT_SCRAPE_DAYS, append_snapshots, eligible_clips, load_probe_rows,
    load_snapshots, match_date_from_html, match_date_of, oldest_headline_date,
    parse_results_headline_date, parse_results_matches, read_snapshots,
    refresh_views, rendered_clip_ids, scrape_dates, snapshot_rate,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _iso(delta_days: float = 0.0) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


def _clip(clip_id: str, views: int, *, published_at: str | None = None,
          round_no: int = 5, title: str = "Mirage AK-47 4K") -> dict:
    return {
        "source": "allstar", "clip_id": clip_id, "steamid": "76561198000000001",
        "player": "donk", "match_id": "2400001", "kinds": ["4k"],
        "opponent": "Vitality", "stage": "playoff", "label": f"donk {title}",
        "views": views, "title": title, "round": round_no,
        "published_at": published_at, "observed_at": _iso(),
    }


def _row(match_id: str = "2400001", *, clips=None, match_date: str | None = None,
         scraped_at: str | None = None, slug: str = "a-vs-b-iem-beijing-2026",
         event_slug: str = "iem-beijing-2026", ok: bool = True,
         playlist_id: str = "6aed2ebf4fe60dada44628f1") -> dict:
    return {
        "match_id": match_id, "slug": slug, "event_slug": event_slug,
        "url": f"https://www.hltv.org/matches/{match_id}/{slug}",
        "ok": ok, "cloudflare": None, "playlist_id": playlist_id,
        "clips": clips if clips is not None else [_clip("c1", 1000)],
        "match_stage": "Playoffs", "match_date": match_date,
        "scraped_at": scraped_at or _iso(),
    }


def _write_probe(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# ── snapshot store ────────────────────────────────────────────────────────
def test_append_snapshots_is_idempotent_per_instant(tmp_path):
    path = tmp_path / "snaps.jsonl"
    rows = [{"clip_id": "c1", "match_id": "m1", "views": 10, "observed_at": _iso()}]
    assert append_snapshots(rows, path) == 1
    # Same instant again: a re-run must not fabricate a second data point.
    assert append_snapshots(rows, path) == 0
    assert len(read_snapshots(path)) == 1
    # A later instant is a real second point.
    later = [{"clip_id": "c1", "match_id": "m1", "views": 40,
              "observed_at": _iso(0.5)}]
    assert append_snapshots(later, path) == 1
    assert [r["views"] for r in load_snapshots(path)["c1"]] == [10, 40]


def test_append_snapshots_skips_rows_without_a_usable_instant(tmp_path):
    path = tmp_path / "snaps.jsonl"
    assert append_snapshots([{"clip_id": "c1", "views": 5}], path) == 0
    assert append_snapshots([{"clip_id": "", "views": 5, "observed_at": _iso()}], path) == 0
    assert append_snapshots([{"clip_id": "c1", "views": None, "observed_at": _iso()}], path) == 0


def test_snapshot_rate_needs_a_span_long_enough_to_mean_something():
    short = [{"views": 100, "observed_at": _iso()},
             {"views": 100, "observed_at": _iso(0.01)}]  # ~15 minutes
    assert snapshot_rate(short) is None
    long = [{"views": 100, "observed_at": _iso()},
            {"views": 700, "observed_at": _iso(1.0)}]  # 24h, +600 views
    assert snapshot_rate(long) == 25.0
    assert snapshot_rate([{"views": 1, "observed_at": _iso()}]) is None


# ── probe store ──────────────────────────────────────────────────────────
def test_load_probe_rows_keeps_the_newest_row_per_match(tmp_path):
    path = _write_probe(tmp_path / "probe.jsonl", [
        _row("2400001", clips=[_clip("c1", 10)], scraped_at=_iso(-1)),
        _row("2400001", clips=[_clip("c1", 99)], scraped_at=_iso()),
        _row("2400002", clips=[_clip("c2", 5)], scraped_at=_iso()),
    ])
    rows = {r["match_id"]: r for r in load_probe_rows(path)}
    assert len(rows) == 2
    assert rows["2400001"]["clips"][0]["views"] == 99


def test_match_date_prefers_the_match_page_then_clip_publication():
    assert match_date_of(_row(match_date="2026-11-04")).date().isoformat() == "2026-11-04"
    published = _row(clips=[_clip("c1", 1, published_at=_iso(-2)),
                            _clip("c2", 1, published_at=_iso(-3))])
    # Earliest clip publication is the closest evidence of when the match ran.
    assert match_date_of(published).date().isoformat() == "2026-11-03"


def test_match_date_never_falls_back_to_when_we_looked():
    """A month-old clip must not look new because we scraped it today."""
    row = _row(clips=[_clip("c1", 1)], scraped_at=_iso())
    assert match_date_of(row) is None


# ── HLTV results parsing ─────────────────────────────────────────────────
RESULTS_HTML = """
<div class="results-holder">
  <div class="standard-headline">Results for November 5th 2026</div>
  <div class="result-con">
    <a href="/matches/2400001/a-vs-b-iem-beijing-2026">A vs B</a>
    <div class="event-name">IEM Beijing 2026</div>
  </div>
  <div class="standard-headline">Results for October 1st 2026</div>
  <div class="result-con">
    <a href="/matches/2390001/c-vs-d-blast-open-2026">C vs D</a>
    <div class="event-name">BLAST Open 2026</div>
  </div>
</div>
"""


def test_parse_results_headline_date_reads_ordinals():
    assert parse_results_headline_date("Results for November 5th 2026").isoformat() == "2026-11-05"
    assert parse_results_headline_date("Results for August 31st 2026").isoformat() == "2026-08-31"
    assert parse_results_headline_date("nonsense") is None


def test_parse_results_matches_filters_by_headline_date():
    from datetime import date

    everything = parse_results_matches(RESULTS_HTML, "https://www.hltv.org")
    assert [m["match_id"] for m in everything] == ["2400001", "2390001"]
    assert everything[0]["event"] == "IEM Beijing 2026"
    assert everything[0]["result_date"] == "2026-11-05"

    recent = parse_results_matches(RESULTS_HTML, "https://www.hltv.org",
                                   on_dates={date(2026, 11, 5)})
    assert [m["match_id"] for m in recent] == ["2400001"]


def test_oldest_headline_date_drives_the_pagination_stop():
    from datetime import date

    assert oldest_headline_date(RESULTS_HTML) == date(2026, 10, 1)
    assert oldest_headline_date("<div class='results-holder'></div>") is None
    # A 14-day window ending 2026-11-06 starts 2026-10-24, so this page has
    # already paged past the window and the scrape must stop here.
    window_start, _dates = scrape_dates(now=NOW)
    assert window_start == date(2026, 10, 24)
    assert oldest_headline_date(RESULTS_HTML) < window_start


def test_scrape_window_is_a_fixed_fortnight():
    from datetime import date

    start, dates = scrape_dates(now=NOW)
    assert DEFAULT_SCRAPE_DAYS == 14
    assert start == date(2026, 10, 24)
    assert len(dates) == 14
    assert max(dates) == date(2026, 11, 6)
    assert min(dates) == start


def test_match_date_from_html_reads_the_header_stamp():
    html = ('<div class="match-header-date">'
            '<div data-unix="1793836800000">18:00</div></div>')
    assert match_date_from_html(html) == "2026-11-05"
    assert match_date_from_html("") is None
    assert match_date_from_html("<div>no stamp</div>") is None


# ── eligibility ──────────────────────────────────────────────────────────
def _evidence_db(path, *, upload_links=(), staged=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE clip_identity_links (foundation_clip_id TEXT, method TEXT)")
    conn.execute("CREATE TABLE short_linkage (evidence TEXT)")
    for clip_id in upload_links:
        conn.execute("INSERT INTO clip_identity_links VALUES (?, 'render_upload_linkage')",
                     (clip_id,))
    for clip_id in staged:
        conn.execute("INSERT INTO short_linkage VALUES (?)",
                     (json.dumps({"allstar_clip_id": clip_id}),))
    conn.commit()
    conn.close()
    return path


def test_rendered_clip_ids_reads_both_sources_and_tolerates_a_missing_db(tmp_path):
    db = _evidence_db(tmp_path / "ev.sqlite3", upload_links=["c1"], staged=["c2"])
    assert rendered_clip_ids(db) == {"c1", "c2"}
    assert rendered_clip_ids(tmp_path / "absent.sqlite3") == set()


def test_eligible_clips_drops_old_rendered_and_undated(tmp_path):
    probe = _write_probe(tmp_path / "probe.jsonl", [
        _row("2400001", match_date="2026-11-04",
             clips=[_clip("fresh", 5000, published_at=_iso(-2)),
                    _clip("rendered", 9000, published_at=_iso(-2))]),
        _row("2400002", match_date="2026-10-01",
             clips=[_clip("stale", 99999, published_at=_iso(-36))]),
        _row("2400003", clips=[_clip("undated", 8000)]),
    ])
    db = _evidence_db(tmp_path / "ev.sqlite3", upload_links=["rendered"])
    clips, stats = eligible_clips(7, out=probe, snapshots=tmp_path / "snaps.jsonl",
                                  db=db, now=NOW)
    assert [c["clip_id"] for c in clips] == ["fresh"]
    assert stats["too_old"] == 1
    assert stats["already_rendered"] == 1
    assert stats["clips_no_date"] == 1
    assert clips[0]["views_per_hour"] == round(5000 / 48.0, 2)


def test_eligible_clips_uses_the_newest_snapshot_and_ignores_non_popular_events(tmp_path):
    probe = _write_probe(tmp_path / "probe.jsonl", [
        _row("2400001", match_date="2026-11-04", clips=[_clip("c1", 100, published_at=_iso(-1))]),
        _row("2400002", match_date="2026-11-04", slug="a-vs-b-esl-challenger-league",
             event_slug="esl-challenger-league", clips=[_clip("c2", 99999)]),
    ])
    snaps = tmp_path / "snaps.jsonl"
    append_snapshots([{"clip_id": "c1", "match_id": "2400001", "views": 400,
                       "observed_at": _iso(-0.5)}], snaps)
    clips, _stats = eligible_clips(7, out=probe, snapshots=snaps,
                                   db=tmp_path / "absent.sqlite3", now=NOW)
    assert [c["clip_id"] for c in clips] == ["c1"]
    assert clips[0]["views"] == 400
    assert clips[0]["snapshots"] == 1


def test_refresh_skips_dead_playlists_and_old_matches(tmp_path, monkeypatch):
    from cs2archive.shorts import allstar_pool

    probe = _write_probe(tmp_path / "probe.jsonl", [
        _row("2400001", match_date="2026-11-05", clips=[_clip("c1", 10)]),
        _row("2400002", match_date="2026-10-01", clips=[_clip("c2", 10)]),
        _row("2400003", match_date="2026-11-05", clips=[_clip("c3", 10)],
             playlist_id="dead"),
    ])
    calls: list[str] = []

    def fake_fetch(playlist_id):
        calls.append(playlist_id)
        if playlist_id == "dead":
            return [], "HTTPStatusError: Client error '404 Not Found'"
        return [{"clip_id": "c1", "views": 777, "title": "t", "steamid": "1",
                 "player": "p", "round": 1}], None

    monkeypatch.setattr("cs2archive.shorts.scrape_allstar_hltv.fetch_playlist_clips",
                        fake_fetch)
    stats = allstar_pool.refresh_views(7, out=probe, snapshots=tmp_path / "snaps.jsonl",
                                       sleep=0, now=NOW)
    assert calls == ["6aed2ebf4fe60dada44628f1", "dead"]
    assert stats["refreshed"] == 1
    assert stats["dead_playlists"] == 1
    rows = {r["match_id"]: r for r in load_probe_rows(probe)}
    assert rows["2400003"]["playlist_dead_at"]
    assert rows["2400001"]["clips"][0]["views"] == 777
    # A dead playlist is not retried on the next pass.
    calls.clear()
    allstar_pool.refresh_views(7, out=probe, snapshots=tmp_path / "snaps.jsonl",
                               sleep=0, now=NOW)
    assert calls == ["6aed2ebf4fe60dada44628f1"]
