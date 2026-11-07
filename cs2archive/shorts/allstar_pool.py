"""The Allstar clip pool: incremental scrape, view refresh, eligibility.

Why this module exists
----------------------
``.data/allstar_hltv_probe.jsonl`` is upsert-per-match: re-scraping a match
replaces its row, so the previous view count is destroyed. That is why the
store held 28k clips and not one of them had two snapshots — no view rate was
computable. This module adds the two things the picker needs:

* an append-only snapshot store (``.data/allstar_view_snapshots.jsonl``), so a
  clip's views over time survive a re-scrape;
* an incremental scrape that only visits matches from the last N days, plus a
  browser-free view refresh for clips that are still eligible.

The scrape window is a fixed 14 days and the scrape runs every 2 days, so
consecutive runs overlap by 12 days. That overlap is deliberate: CS2 has
2-3 week stretches with no pro matches at all, and a window that only looked
at the last 2 days would have nothing to find during a break and could miss
the resumption entirely. Re-covering the whole fortnight is cheap because
already-stored match ids are skipped — only genuinely new fixtures cost a
match-page request.

Eligibility is a separate concern from scraping. A 14-day scrape window
surfaces clips that are dead for Shorts, so the pool caps at ``--days``
(default 7) from the match date and drops anything already rendered.

Usage:
    python -m cs2archive.shorts.allstar_pool --scrape          # last 14 days
    python -m cs2archive.shorts.allstar_pool --refresh         # views only
    python -m cs2archive.shorts.allstar_pool --pool --limit 25 # eligible clips
    python -m cs2archive.shorts.allstar_pool --scrape --refresh --pool
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cs2archive.shorts.popular_events import is_popular_event

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / ".data" / "allstar_hltv_probe.jsonl"
SNAPSHOTS = ROOT / ".data" / "allstar_view_snapshots.jsonl"
EVIDENCE_DB = ROOT / "data" / "social-sweep" / "shorts_evidence.sqlite3"

DEFAULT_DAYS = 7
DEFAULT_SCRAPE_DAYS = 14
RESULTS_PAGE_SIZE = 100
RESULTS_MAX_PAGES = 12

MATCH_HREF = re.compile(r"/matches/(\d+)/([^/?#]+)")
_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)", re.I)


# ── timestamps ────────────────────────────────────────────────────────────
def _stamp(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat()


# ── snapshot store ────────────────────────────────────────────────────────
def read_snapshots(path: Path = SNAPSHOTS) -> list[dict]:
    """Every stored snapshot row, in file order. Malformed lines are skipped."""
    if not Path(path).is_file():
        return []
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("clip_id") and row.get("observed_at"):
            rows.append(row)
    return rows


def append_snapshots(rows: list[dict], path: Path = SNAPSHOTS) -> int:
    """Append view snapshots, skipping any (clip_id, observed_at) already stored.

    Idempotent on purpose: a re-run of the scrape or the refresh must not
    fabricate a second data point for the same instant, or the view rate
    collapses to zero.
    """
    path = Path(path)
    seen = {(str(r["clip_id"]), str(r["observed_at"])) for r in read_snapshots(path)}
    fresh: list[dict] = []
    for row in rows:
        clip_id = str(row.get("clip_id") or "")
        observed_at = row.get("observed_at")
        views = row.get("views")
        if not clip_id or not observed_at or views is None:
            continue
        key = (clip_id, str(observed_at))
        if key in seen:
            continue
        seen.add(key)
        fresh.append({
            "clip_id": clip_id,
            "match_id": str(row.get("match_id") or "") or None,
            "views": int(views),
            "observed_at": str(observed_at),
            "source": str(row.get("source") or "scrape"),
        })
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in fresh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(fresh)


def load_snapshots(path: Path = SNAPSHOTS) -> dict[str, list[dict]]:
    """clip_id -> snapshots sorted oldest first."""
    out: dict[str, list[dict]] = {}
    for row in read_snapshots(path):
        out.setdefault(str(row["clip_id"]), []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: str(r["observed_at"]))
    return out


def snapshot_rate(snaps: list[dict], *, min_hours: float = 6.0) -> float | None:
    """Views per hour between the first and last snapshot, or None.

    Needs two snapshots at distinct instants, at least ``min_hours`` apart.
    Two snapshots taken minutes apart measure nothing but noise — a clip that
    gained no views in five minutes is not a clip with a 0/h rate — so a short
    span returns None rather than a confident zero.
    """
    if len(snaps) < 2:
        return None
    first, last = snaps[0], snaps[-1]
    start, end = _stamp(first.get("observed_at")), _stamp(last.get("observed_at"))
    if start is None or end is None:
        return None
    hours = (end - start).total_seconds() / 3600.0
    if hours < min_hours:
        return None
    try:
        gained = int(last["views"]) - int(first["views"])
    except (KeyError, TypeError, ValueError):
        return None
    return round(gained / hours, 2)


# ── probe store ───────────────────────────────────────────────────────────
def load_probe_rows(path: Path = PROBE) -> list[dict]:
    """One row per match_id, keeping the most recently scraped copy.

    The store is upsert-per-match, but the sweep path appends, so duplicates
    exist. The newest row is the only one whose clip views are current.
    """
    path = Path(path)
    if not path.is_file():
        return []
    best: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        mid = str(row.get("match_id") or "")
        if not mid:
            continue
        old = best.get(mid)
        if old is None or str(row.get("scraped_at") or "") >= str(old.get("scraped_at") or ""):
            best[mid] = row
    return list(best.values())


def match_date_of(row: dict, snaps: dict[str, list[dict]] | None = None) -> datetime | None:
    """When the match was played, from evidence — never from when we looked.

    Preference order: the ``match_date`` the incremental scrape records from
    the HLTV match page, then the earliest clip publication time in the row
    (Allstar publishes within hours of the match).

    Deliberately NOT a fallback: the snapshot time or the row's ``scraped_at``.
    Those say when we *observed* the match, and using them makes a month-old
    clip look brand new — which is exactly how a dead clip slips past the age
    cap. ``snaps`` is accepted for call-site symmetry and is unused.
    """
    direct = _stamp(row.get("match_date"))
    if direct is not None:
        return direct
    published = [
        _stamp(clip.get("published_at"))
        for clip in row.get("clips") or []
        if isinstance(clip, dict)
    ]
    known = [stamp for stamp in published if stamp is not None]
    return min(known) if known else None


# ── HLTV results page ─────────────────────────────────────────────────────
def parse_results_headline_date(text: str) -> date | None:
    """Parse HLTV result group titles like 'Results for August 31st 2026'."""
    raw = re.sub(r"^results for\s+", "", (text or "").strip(), flags=re.I)
    raw = _ORDINAL.sub(r"\1", raw)
    try:
        return datetime.strptime(raw, "%B %d %Y").date()
    except ValueError:
        return None


def oldest_headline_date(html: str) -> date | None:
    """Oldest ``.standard-headline`` date on a ``/results`` page, or None.

    Pagination stops on this: HLTV lists results newest-first, so once a page's
    oldest group is older than the window start there is nothing further back
    worth fetching.
    """
    soup = BeautifulSoup(html, "lxml")
    holder = soup.select_one(".results-holder")
    if holder is None:
        return None
    dates = [
        parse_results_headline_date(node.get_text(" ", strip=True))
        for node in holder.select(".standard-headline")
    ]
    known = [stamp for stamp in dates if stamp is not None]
    return min(known) if known else None


def parse_results_matches(
    html: str,
    base_url: str,
    *,
    on_dates: set[date] | None = None,
) -> list[dict]:
    """Completed matches from an HLTV ``/results`` page, optionally date-filtered.

    Deliberately a local copy of the listener's parser rather than an import:
    ``cs2archive.listener.daemon`` calls ``os.chdir(ROOT)`` at module scope, and
    a library module must not move the process's working directory.

    HLTV ignores ``?date=`` on ``/results`` (the SPA always returns the latest
    dump), so filtering uses the on-page ``.standard-headline`` groups.
    """
    soup = BeautifulSoup(html, "lxml")
    holder = soup.select_one(".results-holder")
    found: list[dict] = []
    seen: set[str] = set()
    if holder is not None:
        headline_date: date | None = None
        for node in holder.descendants:
            if not getattr(node, "name", None):
                continue
            classes = node.get("class") or []
            if "standard-headline" in classes:
                headline_date = parse_results_headline_date(node.get_text(" ", strip=True))
                continue
            if node.name != "div" or "result-con" not in classes:
                continue
            # Featured results (and any block with no parseable date) are the
            # live dump's latest completed matches; only dated headlines
            # outside on_dates are skipped.
            if on_dates is not None and headline_date is not None and headline_date not in on_dates:
                continue
            link = node.select_one('a[href*="/matches/"]')
            if link is None:
                continue
            hit = MATCH_HREF.search(link.get("href") or "")
            if not hit:
                continue
            mid, slug = hit.group(1), hit.group(2)
            if mid in seen:
                continue
            seen.add(mid)
            event_node = node.select_one(".event-name")
            found.append({
                "match_id": mid,
                "slug": slug,
                "url": urljoin(base_url, link["href"]),
                "event": event_node.get_text(" ", strip=True) if event_node else "",
                "result_date": headline_date.isoformat() if headline_date else None,
            })
        return found
    for link in soup.select('a[href*="/matches/"]'):
        hit = MATCH_HREF.search(link.get("href") or "")
        if not hit:
            continue
        mid, slug = hit.group(1), hit.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        found.append({
            "match_id": mid,
            "slug": slug,
            "url": urljoin(base_url, link["href"]),
            "event": "",
            "result_date": None,
        })
    return found


def match_date_from_html(html: str) -> str | None:
    """Match date from an HLTV match page, as an ISO date, or None.

    The header carries a ``data-unix`` epoch-ms stamp. Selectors are tried most
    specific first so a page-level stamp elsewhere cannot win.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    for selector in (".match-header-date [data-unix]", ".match-header-date",
                     "[data-zonedgrouping-entry-unix]", "[data-unix]"):
        node = soup.select_one(selector)
        if node is None:
            continue
        raw = node.get("data-unix") or node.get("data-zonedgrouping-entry-unix")
        if not raw:
            continue
        try:
            unix = int(raw)
        except (TypeError, ValueError):
            continue
        if unix < 10_000_000_000:
            unix *= 1000
        return datetime.fromtimestamp(unix / 1000, tz=timezone.utc).date().isoformat()
    return None


def scrape_dates(
    days: int = DEFAULT_SCRAPE_DAYS,
    *,
    now: datetime | None = None,
) -> tuple[date, set[date]]:
    """The fixed scrape window: ``days`` dates ending today, and its start.

    A fixed fortnight rather than a window derived from the store, because the
    cadence is fixed too: every run re-covers the same 14 days and the
    incremental skip makes the overlap free.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    start = today - timedelta(days=max(1, days) - 1)
    return start, {start + timedelta(days=i) for i in range(max(1, days))}


# ── incremental scrape ────────────────────────────────────────────────────
def scrape_recent(
    days: int = DEFAULT_SCRAPE_DAYS,
    *,
    out: Path = PROBE,
    snapshots: Path = SNAPSHOTS,
    sleep: float = 2.0,
    max_matches: int = 0,
    headed: bool = False,
    abort_after_cf: int = 5,
    now: datetime | None = None,
) -> dict:
    """Scrape matches completed in the last ``days`` days (default 14).

    Never re-fetches a match page already in the probe store, so re-covering
    the whole fortnight every run costs only the new fixtures. ``/results`` is
    paginated because two weeks of pro matches do not fit on one page.
    """
    from cloakbrowser import launch_persistent_context

    out, snapshots = Path(out), Path(snapshots)
    from cs2archive.config import settings
    from cs2archive.shorts.scrape_allstar_hltv import (
        PROFILE_DIR, _goto, _load_done, _playlist_id, _stamp_match, _store_clips,
        fetch_playlist_clips, match_stage_from_html, upsert_row,
    )

    base = settings.hltv_base_url.rstrip("/")
    window_start, on_dates = scrape_dates(days, now=now)
    done = _load_done(out)
    stats = {
        "window_days": days,
        "window_start": window_start.isoformat(),
        "results_pages": 0,
        "results_seen": 0,
        "already_scraped": 0,
        "non_popular": 0,
        "pending": 0,
        "scraped": 0,
        "cloudflare": 0,
        "no_playlist": 0,
        "clips": 0,
        "snapshots": 0,
    }

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = launch_persistent_context(
        str(PROFILE_DIR.resolve()),
        headless=not headed,
        viewport={"width": 1920, "height": 1080},
        humanize=True,
        channel="chrome",
    )
    cf_streak = 0
    try:
        page = ctx.new_page()
        # A 14-day window does not fit on one /results page, so page back until
        # the oldest headline group on a page predates the window start.
        matches: list[dict] = []
        seen_ids: set[str] = set()
        for page_no in range(RESULTS_MAX_PAGES):
            offset = page_no * RESULTS_PAGE_SIZE
            url = f"{base}/results" if offset == 0 else f"{base}/results?offset={offset}"
            html, cf = _goto(page, url)
            if cf:
                stats["cloudflare"] += 1
                stats["error"] = cf
                if not matches:
                    return stats
                break
            stats["results_pages"] += 1
            fresh = [
                match for match in parse_results_matches(html, base, on_dates=on_dates)
                if match["match_id"] not in seen_ids
            ]
            for match in fresh:
                seen_ids.add(match["match_id"])
            matches.extend(fresh)
            oldest = oldest_headline_date(html)
            if not fresh or oldest is None or oldest < window_start:
                break
        stats["results_seen"] = len(matches)
        pending: list[dict] = []
        for match in matches:
            if match["match_id"] in done:
                stats["already_scraped"] += 1
                continue
            if not is_popular_event(match["slug"], match.get("event") or ""):
                stats["non_popular"] += 1
                continue
            pending.append(match)
        if max_matches:
            pending = pending[:max_matches]
        stats["pending"] = len(pending)

        for i, match in enumerate(pending, 1):
            if cf_streak >= abort_after_cf:
                stats["aborted"] = "consecutive cloudflare"
                break
            time.sleep(sleep + random.random())
            html, cf = _goto(page, match["url"])
            if cf:
                stats["cloudflare"] += 1
                cf_streak += 1
                print(f"[CF] {match['match_id']} {cf}  ({i}/{len(pending)})", flush=True)
                continue
            cf_streak = 0
            # Allstar iframes are loading="lazy" — scroll so they enter the DOM.
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2500)
                html = page.content()
            except Exception:
                pass
            row = {
                **match,
                "ok": False,
                "cloudflare": None,
                "playlist_id": None,
                "clips": [],
                "iframe_error": None,
                "iframe_sample": "",
                "match_stage": match_stage_from_html(html),
                "match_date": match_date_from_html(html) or match.get("result_date"),
                "scraped_at": _iso(datetime.now(timezone.utc)),
            }
            pid = _playlist_id(html)
            row["playlist_id"] = pid
            if not pid:
                stats["no_playlist"] += 1
                upsert_row(out, row)
                print(f"[--] {match['match_id']} {match['slug']}: no playlist  "
                      f"({i}/{len(pending)})", flush=True)
                continue
            clips, err = fetch_playlist_clips(pid)
            row["iframe_error"] = err
            row["clips"] = _store_clips(row, _stamp_match(clips, match["match_id"]))
            row["ok"] = True
            upsert_row(out, row)
            stats["scraped"] += 1
            stats["clips"] += len(row["clips"])
            stats["snapshots"] += append_snapshots(
                [{"clip_id": c.get("clip_id"), "match_id": match["match_id"],
                  "views": c.get("views"), "observed_at": row["scraped_at"],
                  "source": "scrape"} for c in row["clips"]],
                snapshots,
            )
            print(f"[ok] {match['match_id']} {match['slug']} clips={len(row['clips'])} "
                  f"date={row['match_date']}  ({i}/{len(pending)})", flush=True)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return stats


# ── view refresh ──────────────────────────────────────────────────────────
def refresh_views(
    days: int = DEFAULT_DAYS,
    *,
    out: Path = PROBE,
    snapshots: Path = SNAPSHOTS,
    sleep: float = 0.5,
    max_matches: int = 0,
    now: datetime | None = None,
) -> dict:
    """Re-poll views for matches still inside the eligibility window.

    No browser and no HLTV: the playlist API is keyed by the ``playlist_id``
    already stored on the row, so this is a plain HTTP call per match. The
    probe row's clip views are rewritten in one pass at the end so the older
    selectors see current numbers too.
    """
    from cs2archive.shorts.scrape_allstar_hltv import (
        _rewrite_jsonl, _stamp_match, _store_clips, fetch_playlist_clips,
    )

    out, snapshots = Path(out), Path(snapshots)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    snaps = load_snapshots(snapshots)
    rows = load_probe_rows(out)
    targets = []
    for row in rows:
        if not row.get("playlist_id") or row.get("playlist_dead_at"):
            continue
        played = match_date_of(row, snaps)
        # Undated rows are refreshed too: the playlist payload carries each
        # clip's publication time, which is the only age signal those rows can
        # ever get. Refreshing is one HTTP call, so learning the date is cheap.
        if played is not None and played < cutoff:
            continue
        targets.append((played, row))
    # Newest first: an expired playlist 404s, and the recent matches are the
    # ones whose view counts still matter.
    targets.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    targets = [row for _played, row in targets]
    if max_matches:
        targets = targets[:max_matches]

    stats = {"eligible_matches": len(targets), "refreshed": 0, "errors": 0,
             "dead_playlists": 0, "snapshots": 0, "clips": 0}
    updated: dict[str, dict] = {}
    for i, row in enumerate(targets, 1):
        mid = str(row.get("match_id") or "")
        clips, err = fetch_playlist_clips(str(row["playlist_id"]))
        if err:
            stats["errors"] += 1
            if "404" in err:
                # Allstar expires old playlists. Remember it, or every refresh
                # re-asks the same dead URL forever.
                stats["dead_playlists"] += 1
                updated[mid] = {**row, "playlist_dead_at": _iso(datetime.now(timezone.utc))}
            print(f"[refresh] {mid}: {err}  ({i}/{len(targets)})", flush=True)
            time.sleep(sleep)
            continue
        observed_at = _iso(datetime.now(timezone.utc))
        stamped = _stamp_match(clips, mid)
        stats["snapshots"] += append_snapshots(
            [{"clip_id": c.get("clip_id"), "match_id": mid, "views": c.get("views"),
              "observed_at": observed_at, "source": "refresh"} for c in stamped],
            snapshots,
        )
        stats["clips"] += len(stamped)
        stats["refreshed"] += 1
        # Stamp the row with the new observation time *before* deriving the
        # clip observations, or they inherit the previous scrape's timestamp.
        refreshed_row = {**row, "scraped_at": observed_at}
        updated[mid] = {**refreshed_row, "clips": _store_clips(refreshed_row, stamped)}
        print(f"[refresh] {mid} clips={len(stamped)}  ({i}/{len(targets)})", flush=True)
        time.sleep(sleep)

    if updated:
        merged = [updated.get(str(r.get("match_id") or ""), r) for r in rows]
        _rewrite_jsonl(out, merged)
    return stats


# ── eligibility ───────────────────────────────────────────────────────────
def rendered_clip_ids(db: Path = EVIDENCE_DB) -> set[str]:
    """Allstar clip ids we have already rendered or staged for render.

    Two queryable sources, both keyed by the Allstar clip id:

    * ``clip_identity_links`` rows written at upload time
      (``method='render_upload_linkage'``) — the exact link;
    * ``short_linkage`` rows whose evidence carries ``allstar_clip_id`` — a
      render that was staged from an Allstar clip, linked or still pending.

    A missing database is not an error: it means nothing has been rendered
    through the social pipeline yet.
    """
    db = Path(db)
    if not db.is_file():
        return set()
    out: set[str] = set()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for (clip_id,) in conn.execute(
            "SELECT DISTINCT foundation_clip_id FROM clip_identity_links "
            "WHERE method = 'render_upload_linkage' AND foundation_clip_id IS NOT NULL"
        ):
            out.add(str(clip_id))
        for (evidence,) in conn.execute(
            "SELECT evidence FROM short_linkage WHERE evidence LIKE '%allstar_clip_id%'"
        ):
            try:
                payload = json.loads(evidence or "{}")
            except (TypeError, ValueError):
                continue
            clip_id = (payload or {}).get("allstar_clip_id")
            if clip_id:
                out.add(str(clip_id))
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def eligible_clips(
    days: int = DEFAULT_DAYS,
    *,
    out: Path = PROBE,
    snapshots: Path = SNAPSHOTS,
    db: Path = EVIDENCE_DB,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """Clips worth nominating: inside the age cap, not already rendered.

    Returns the clips sorted by views, plus a report of what was dropped and
    why. Views come from the newest snapshot when one exists, otherwise from
    the probe row.
    """
    from cs2archive.shorts.join_clips_to_demos import map_slug_from_title

    out, snapshots, db = Path(out), Path(snapshots), Path(db)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    snaps = load_snapshots(snapshots)
    rendered = rendered_clip_ids(db)
    stats = {"matches": 0, "too_old": 0, "clips_no_date": 0, "clips": 0,
             "already_rendered": 0, "eligible": 0, "with_rate": 0,
             "with_recent_rate": 0}

    clips: list[dict] = []
    for row in load_probe_rows(out):
        if not row.get("ok") or not row.get("clips"):
            continue
        if not is_popular_event(str(row.get("slug") or ""), str(row.get("event_slug") or "")):
            continue
        stats["matches"] += 1
        played = match_date_of(row, snaps)
        if played is not None and played < cutoff:
            stats["too_old"] += 1
            continue
        for clip in row["clips"]:
            if not isinstance(clip, dict) or not clip.get("clip_id"):
                continue
            stats["clips"] += 1
            clip_id = str(clip["clip_id"])
            if clip_id in rendered:
                stats["already_rendered"] += 1
                continue
            published = _stamp(clip.get("published_at"))
            # The match date is the cap the product asked for. When the row has
            # no date at all (older collector rows), the clip's own publication
            # time is the only age evidence there is — use it rather than
            # silently keeping a clip that could be a month old.
            if played is None:
                if published is None or published < cutoff:
                    stats["clips_no_date"] += 1
                    continue
            history = snaps.get(clip_id) or []
            views = history[-1]["views"] if history else clip.get("views")
            if views is None:
                continue
            age_hours = round((now - published).total_seconds() / 3600.0, 1) if published else None
            # Two different numbers, both honest:
            #   views_per_hour        — lifetime average, views over the clip's
            #                           real age. Available from one snapshot
            #                           because Allstar gives us createdDate.
            #   recent_views_per_hour — growth between snapshots, only when the
            #                           span is long enough to mean something.
            rate = round(int(views) / age_hours, 2) if age_hours and age_hours > 0 else None
            recent = snapshot_rate(history)
            if rate is not None:
                stats["with_rate"] += 1
            if recent is not None:
                stats["with_recent_rate"] += 1
            title = str(clip.get("title") or "")
            clips.append({
                "clip_id": clip_id,
                "player": clip.get("player"),
                "steamid": clip.get("steamid"),
                "opponent": clip.get("opponent"),
                "match_id": str(row.get("match_id") or ""),
                "hltv_match": f"https://www.hltv.org/matches/{row.get('match_id')}",
                "event_slug": row.get("event_slug"),
                "map": map_slug_from_title(title) or None,
                "round": clip.get("round"),
                "kinds": clip.get("kinds") or [],
                "title": title,
                "label": clip.get("label"),
                "stage": clip.get("stage") or row.get("match_stage"),
                "views": int(views),
                "views_per_hour": rate,
                "recent_views_per_hour": recent,
                "snapshots": len(history),
                "published_at": clip.get("published_at"),
                "clip_length": clip.get("clip_length"),
                "score_line": clip.get("score_line"),
                "series_game_label": clip.get("series_game_label"),
                "match_date": played.date().isoformat() if played else None,
                "age_days": round((now - played).total_seconds() / 86400.0, 2) if played else None,
                "clip_age_days": round((now - published).total_seconds() / 86400.0, 2)
                if published else None,
                "observed_at": history[-1]["observed_at"] if history else clip.get("observed_at"),
            })
    clips.sort(key=lambda c: -c["views"])
    stats["eligible"] = len(clips)
    return clips, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Allstar clip pool: scrape, refresh, eligibility")
    ap.add_argument("--scrape", action="store_true", help="Incremental scrape of recent matches")
    ap.add_argument("--refresh", action="store_true", help="Re-poll views for eligible clips")
    ap.add_argument("--pool", action="store_true", help="Print the eligible clip pool")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Eligibility age cap")
    ap.add_argument("--scrape-days", type=int, default=DEFAULT_SCRAPE_DAYS,
                    help="Fixed scrape window in days (default 14; re-covered every run)")
    ap.add_argument("--limit", type=int, default=0, help="Cap the printed pool")
    ap.add_argument("--max-matches", type=int, default=0, help="Cap matches per pass")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", type=Path, default=PROBE)
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--db", type=Path, default=EVIDENCE_DB)
    args = ap.parse_args()

    if not (args.scrape or args.refresh or args.pool):
        ap.error("nothing to do: pass --scrape, --refresh or --pool")

    if args.scrape:
        stats = scrape_recent(args.scrape_days, out=args.out, snapshots=args.snapshots,
                              sleep=args.sleep, max_matches=args.max_matches,
                              headed=args.headed)
        print(json.dumps({"scrape": stats}))
    if args.refresh:
        stats = refresh_views(args.days, out=args.out, snapshots=args.snapshots,
                              sleep=min(args.sleep, 1.0), max_matches=args.max_matches)
        print(json.dumps({"refresh": stats}))
    if args.pool:
        clips, stats = eligible_clips(args.days, out=args.out, snapshots=args.snapshots,
                                      db=args.db)
        print(json.dumps({"pool": stats}))
        shown = clips[: args.limit] if args.limit else clips
        for clip in shown:
            rate = clip["views_per_hour"]
            print(f"  {clip['views']:>8}  {rate if rate is not None else '?':>8}/h  "
                  f"{str(clip['player'] or '?'):12s} {str(clip['map'] or '?'):8s} "
                  f"R{clip['round']}  {clip['title']}")
        print(f"{len(clips)} eligible clips ({len(shown)} shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
