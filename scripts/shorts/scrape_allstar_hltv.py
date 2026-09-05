"""Probe: scrape Allstar playlists from HLTV match pages of Popular events.

Uses one CloakBrowser persistent context (same `.sessions/hltv-cloak/` as
demo download) to find match playlist ids, then the Allstar playlist API for
clips (steamid, HLTV nick, match id, views). Writes JSONL as it goes.

    python scripts/shorts/scrape_allstar_hltv.py
    python scripts/shorts/scrape_allstar_hltv.py --max-matches 10
    python scripts/shorts/scrape_allstar_hltv.py --enrich
    python scripts/shorts/scrape_allstar_hltv.py --fill-stage
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from config import settings  # noqa: E402
from shorts.popular_events import is_popular_event  # noqa: E402
from shorts.clip_observation import observations_from_match_row, parse_stage  # noqa: E402
from scrapers.ratings import extract_match_stage  # noqa: E402

PROFILE_DIR = ROOT / ".sessions" / "hltv-cloak"
OUT_DEFAULT = ROOT / ".data" / "allstar_hltv_probe.jsonl"
EVENTS_DEFAULT = ROOT / ".data" / "allstar_hltv_events.json"

MATCH_HREF = re.compile(r"/matches/(\d+)/([^/?#]+)")
EVENT_HREF = re.compile(r"/events/(\d+)/([^/?#]+)")
PLAYLIST_RE = re.compile(
    r"(?:allstar\.gg/iframe\?[^\"'\s>]*|[\?&])playlist=([0-9a-f]{16,})",
    re.I,
)
PLAYLIST_API = "https://prt.allstar.gg/playlist"
CF_MARKERS = (
    "just a moment",
    "attention required",
    "cf-challenge",
    "checking your browser",
    "sorry, you have been blocked",
    "enable javascript and cookies",
)
CF_ERR = ("ERR_CONNECTION_RESET", "ERR_HTTP2_PROTOCOL_ERROR", "ERR_TUNNEL")


def _cf_reason(html: str, err: str | None = None) -> str | None:
    if err:
        up = err.upper()
        for token in CF_ERR:
            if token in up:
                return token
    low = (html or "")[:8000].lower()
    for marker in CF_MARKERS:
        if marker in low:
            return marker
    title = ""
    if html:
        m = re.search(r"<title>([^<]*)</title>", html, re.I)
        title = (m.group(1) if m else "").strip().lower()
    if title in {"hltv.org", "access denied", "attention required! | hltv.org"}:
        if "hltv" not in low[400:]:
            return f"thin-page:{title or 'empty'}"
    return None


def _goto(page, url: str, timeout_ms: int = 60_000) -> tuple[str, str | None]:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
        html = page.content()
        return html, _cf_reason(html)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        return "", _cf_reason("", msg) or msg


def _parse_events(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/events/"]'):
        href = a.get("href") or ""
        m = EVENT_HREF.search(href)
        if not m:
            continue
        eid, slug = m.group(1), m.group(2)
        if slug in {"matches", "stats", "highlights", "archive"} or eid in seen:
            continue
        seen.add(eid)
        name = a.get_text(" ", strip=True)
        found.append({
            "event_id": eid,
            "slug": slug,
            "name": name,
            "url": urljoin(base, f"/events/{eid}/{slug}"),
            "popular": is_popular_event(slug, name),
        })
    return found


def _parse_match_urls(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/matches/"]'):
        href = a.get("href") or ""
        m = MATCH_HREF.search(href)
        if not m:
            continue
        mid, slug = m.group(1), m.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        found.append({
            "match_id": mid,
            "slug": slug,
            "url": urljoin(base, f"/matches/{mid}/{slug}"),
        })
    return found


def _playlist_id(html: str) -> str | None:
    m = PLAYLIST_RE.search(html or "")
    return m.group(1) if m else None


def match_stage_from_html(html: str) -> str | None:
    """Event stage from the joined HLTV match page (not map/round)."""
    text = extract_match_stage(html)
    return text or None


def _store_clips(row: dict, clips: list[dict]) -> list[dict]:
    return observations_from_match_row({**row, "clips": clips})


def _partner_meta(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.get("metadata") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        val = item.get("value")
        if val is None:
            continue
        out[str(key)] = str(val)
    return out


def clip_from_allstar(raw: dict) -> dict | None:
    """One Trending clip: steam64 + HLTV nick + match id, or None if unidentifiable."""
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    views = raw.get("views")
    if not title or views is None:
        return None
    meta = _partner_meta(raw)
    steamid = str(raw.get("steamid") or raw.get("playerGameIdentifier") or "").strip()
    player = (meta.get("PARTNER_playerName") or "").strip()
    if not steamid and not player:
        return None
    try:
        views_n = int(views)
    except (TypeError, ValueError):
        return None
    match_id = (meta.get("PARTNER_matchId") or "").strip() or None
    opponent_team = (meta.get("PARTNER_opponentTeamName") or "").strip() or None
    label = f"{player} {title}".strip() if player else str(title)
    round_n = raw.get("roundNumber")
    try:
        round_n = int(round_n) if round_n is not None else None
    except (TypeError, ValueError):
        round_n = None
    return {
        "clip_id": str(raw.get("_id") or raw.get("internalId") or ""),
        "steamid": steamid or None,
        "player": player or None,
        "match_id": match_id,
        "title": str(title),
        "label": label,
        "views": views_n,
        "round": round_n,
        "opponent_team": opponent_team,
    }


def clips_from_playlist_payload(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(raw_clips, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_clips:
        rec = clip_from_allstar(raw) if isinstance(raw, dict) else None
        if not rec:
            continue
        key = rec["clip_id"] or f"{rec.get('steamid')}:{rec.get('title')}:{rec.get('round')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def fetch_playlist_clips(playlist_id: str) -> tuple[list[dict], str | None]:
    try:
        r = httpx.get(
            PLAYLIST_API,
            params={"name": playlist_id, "platform": "HLTV.ORG", "fmt": "iframe"},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        return clips_from_playlist_payload(r.json()), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _collect_archive_events(page, base: str, start: date, end: date) -> tuple[list[dict], int]:
    """Paginate HLTV events archive. Date query alone only returns the first page."""
    events: list[dict] = []
    seen: set[str] = set()
    cf_hits = 0
    offset = 0
    for _ in range(40):
        url = (
            f"{base}/events/archive?startDate={start.isoformat()}"
            f"&endDate={end.isoformat()}&offset={offset}"
        )
        html, cf = _goto(page, url)
        if cf:
            print(f"[CF] archive offset={offset}: {cf}")
            cf_hits += 1
            break
        batch = _parse_events(html, base)
        new = [e for e in batch if e["event_id"] not in seen]
        if not new:
            if offset == 0:
                html2, cf2 = _goto(page, f"{base}/events/archive?offset={offset}")
                if cf2:
                    print(f"[CF] archive no-dates: {cf2}")
                    cf_hits += 1
                    break
                batch = _parse_events(html2, base)
                new = [e for e in batch if e["event_id"] not in seen]
            if not new:
                break
        for e in new:
            seen.add(e["event_id"])
            events.append(e)
        print(f"[probe] archive offset={offset} +{len(new)} (total {len(events)})")
        offset += max(len(batch), 50)
    return events, cf_hits


def _collect_event_matches(page, ev: dict, base: str, sleep: float) -> tuple[list[dict], int]:
    """Results tab for this event; keep new match ids from that event's pages."""
    rows: list[dict] = []
    seen: set[str] = set()
    cf_hits = 0
    offset = 0
    slug = ev["slug"]
    for _ in range(20):
        time.sleep(sleep + random.random())
        url = f"{base}/results?event={ev['event_id']}&offset={offset}"
        html, cf = _goto(page, url)
        if cf:
            print(f"[CF] results {slug} offset={offset}: {cf}")
            cf_hits += 1
            break
        parsed = _parse_match_urls(html, base)
        slug_hits = [r for r in parsed if slug in r["slug"]]
        use = slug_hits if slug_hits else parsed
        new = []
        for r in use:
            if r["match_id"] in seen:
                continue
            seen.add(r["match_id"])
            r["event_id"] = ev["event_id"]
            r["event_slug"] = slug
            new.append(r)
        if not new:
            break
        rows.extend(new)
        if len(new) < 40:
            break
        offset += 100
    return rows, cf_hits


def _stamp_match(clips: list[dict], match_id: str | None) -> list[dict]:
    if not match_id:
        return clips
    for rec in clips:
        if not rec.get("match_id"):
            rec["match_id"] = match_id
    return clips


def _load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = str(row.get("match_id") or "")
        if not mid or row.get("cloudflare"):
            continue
        done.add(mid)
    return done


def _listener_unseen(done: set[str], path: Path | None = None) -> list[dict]:
    """New listener match URLs first (ticket: listener first, then backfill)."""
    dest = path or (ROOT / ".listener" / "hltv.json")
    if not dest.is_file():
        return []
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    out: list[dict] = []
    for mid, rec in (data.get("matches") or {}).items():
        if str(mid) in done:
            continue
        match = rec.get("match") if isinstance(rec, dict) else None
        if not isinstance(match, dict):
            continue
        url = match.get("url")
        if not url:
            continue
        slug = str(match.get("slug") or "")
        event = str(match.get("event") or match.get("event_slug") or "")
        if not is_popular_event(slug, event):
            continue
        out.append({
            "match_id": str(mid),
            "slug": str(match.get("slug") or ""),
            "url": str(url),
            "event_id": str(match.get("event_id") or ""),
            "event_slug": str(match.get("event") or ""),
        })
    return out


def prioritize_pending(
    matches: list[dict],
    done: set[str],
    *,
    listener_rows: list[dict] | None = None,
    max_matches: int = 0,
) -> list[dict]:
    ordered: list[dict] = []
    seen: set[str] = set()
    for row in list(listener_rows or []) + [
        m for m in matches if m.get("match_id") not in done
    ]:
        mid = str(row.get("match_id") or "")
        if not mid or mid in seen or mid in done:
            continue
        seen.add(mid)
        ordered.append(row)
    if max_matches:
        return ordered[:max_matches]
    return ordered


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_ratings_stages(analysis_dir: Path | None = None) -> dict[str, str]:
    """match_stage from backlog ratings JSON, keyed by slug and match id."""
    from shorts.clip_observation import clean_hltv_stage

    dest = analysis_dir or (ROOT / "demos" / "analysis")
    known: dict[str, str] = {}
    if not dest.is_dir():
        return known
    for path in dest.glob("*_ratings.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        stage = clean_hltv_stage(data.get("match_stage"))
        if not parse_stage(stage):
            continue
        known[path.name[: -len("_ratings.json")]] = stage
        m = MATCH_HREF.search(str(data.get("url") or ""))
        if m:
            known[m.group(1)] = stage
    return known


def apply_known_stages(rows: list[dict], known: dict[str, str]) -> int:
    filled = 0
    for row in rows:
        if parse_stage(row.get("match_stage") or row.get("stage")):
            continue
        slug = str(row.get("slug") or "")
        mid = str(row.get("match_id") or "")
        stage = known.get(slug) or known.get(mid)
        if not parse_stage(stage):
            continue
        row["match_stage"] = stage
        filled += 1
    return filled


def _rewrite_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        tmp.replace(path)
    except PermissionError:
        # Windows: another process (listener) holds the target open without
        # share-delete; fall back to an in-place rewrite (same bytes).
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        try:
            tmp.unlink()
        except OSError:
            pass


def _fetch_missing_stages(
    pending: list[dict], *, sleep: float, abort_after_cf: int,
    on_progress=None,
) -> dict:
    from cloakbrowser import launch_persistent_context

    stats = {"fetched": 0, "cloudflare": 0}
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = launch_persistent_context(
        str(PROFILE_DIR.resolve()),
        headless=True,
        viewport={"width": 1920, "height": 1080},
        humanize=True,
        channel="chrome",
    )
    cf_streak = 0
    try:
        page = ctx.new_page()
        for i, row in enumerate(pending, 1):
            if parse_stage(row.get("match_stage")):
                continue
            url = row.get("url")
            if not url:
                continue
            time.sleep(sleep + random.random())
            html, cf = _goto(page, url)
            if cf:
                stats["cloudflare"] += 1
                cf_streak += 1
                print(f"[CF] {row.get('match_id')} {cf}  ({i}/{len(pending)})")
                if cf_streak >= abort_after_cf:
                    print("[fill-stage] abort: consecutive Cloudflare")
                    break
                continue
            cf_streak = 0
            stage = match_stage_from_html(html)
            if parse_stage(stage):
                row["match_stage"] = stage
                stats["fetched"] += 1
                print(f"[ok] {row.get('match_id')} {stage}  ({i}/{len(pending)})")
                if on_progress and stats["fetched"] % 20 == 0:
                    on_progress()
            else:
                print(f"[--] {row.get('match_id')} no stage  ({i}/{len(pending)})")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return stats


def fill_jsonl_stages(
    path: Path,
    *,
    fetch: bool = True,
    sleep: float = 2.0,
    abort_after_cf: int = 5,
) -> dict:
    """Write match_stage onto an existing Allstar JSONL. Does not wipe clips."""
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    stats = {
        "rows": len(rows),
        "from_ratings": 0,
        "fetched": 0,
        "cloudflare": 0,
        "still_empty": 0,
    }
    stats["from_ratings"] = apply_known_stages(rows, load_ratings_stages())
    _rewrite_jsonl(path, rows)
    pending = [
        r for r in rows if not parse_stage(r.get("match_stage") or r.get("stage"))
    ]
    if fetch and pending:
        from shorts.fit_partial_stars import listener_holds_cloak

        if listener_holds_cloak():
            print("[fill-stage] skip fetch: listener holds Cloak")
        else:
            fetched = _fetch_missing_stages(
                pending, sleep=sleep, abort_after_cf=abort_after_cf,
                on_progress=lambda: _rewrite_jsonl(path, rows),
            )
            stats["fetched"] = fetched.get("fetched", 0)
            stats["cloudflare"] = fetched.get("cloudflare", 0)
    stats["still_empty"] = sum(
        1 for r in rows if not parse_stage(r.get("match_stage") or r.get("stage"))
    )
    _rewrite_jsonl(path, rows)
    return stats


def enrich_jsonl(path: Path, sleep: float) -> dict:
    """Re-fetch playlists for rows that already have playlist_id. No HLTV."""
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    stats = {"rows": len(rows), "fetched": 0, "clips": 0, "errors": 0, "with_player": 0}
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for i, row in enumerate(rows, 1):
            pid = row.get("playlist_id")
            mid = str(row.get("match_id") or "")
            if pid:
                clips, err = fetch_playlist_clips(str(pid))
                clips = _stamp_match(clips, mid)
                row["clips"] = _store_clips(row, clips)
                row["iframe_error"] = err
                row["iframe_sample"] = ""
                stats["fetched"] += 1
                stats["clips"] += len(row["clips"])
                stats["with_player"] += sum(
                    1 for c in row["clips"] if c.get("steamid") or c.get("player")
                )
                if err:
                    stats["errors"] += 1
                    print(f"[enrich] {mid} {pid}: {err}")
                else:
                    print(f"[enrich] {mid} clips={len(clips)}  ({i}/{len(rows)})")
                time.sleep(sleep)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return stats


def _probe_slug(path: Path, mid: str) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("match_id")) == str(mid):
                return str(row.get("slug") or "") or None
    return None


def upsert_row(path: Path, row: dict) -> None:
    """Replace the row for this match_id, or append if new."""
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(parsed.get("match_id")) != str(row.get("match_id")):
                    rows.append(parsed)
    rows.append(row)
    _rewrite_jsonl(path, rows)


def scrape_one_match(page, match_ref: str, base: str,
                     *, out: Path, skip_iframe: bool = False) -> dict:
    """Scrape a single HLTV match page by id or URL; upsert probe row."""
    ref = (match_ref or "").strip()
    hit = MATCH_HREF.search(ref if ref.startswith("http") else f"/matches/{ref}/x")
    if not hit:
        raise SystemExit(f"[match] can't parse match id from {match_ref!r}")
    mid, slug = hit.group(1), hit.group(2)
    if slug == "x":
        # Bare /matches/<id> serves a thin shell without embeds — resolve
        # the slug from the existing probe row.
        slug = _probe_slug(out, mid) or "x"
        if slug == "x":
            raise SystemExit(
                f"[match] unknown slug for {mid}; pass the full HLTV URL")
    url = f"{base}/matches/{mid}/{slug}"
    html, cf = _goto(page, url)
    html, cf = _goto(page, url)
    if not cf:
        # Allstar iframes are loading="lazy" — scroll so they enter the DOM.
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2500)
            html = page.content()
        except Exception:
            pass
    row = {
        "match_id": mid,
        "slug": slug if slug != "x" else "",
        "url": url,
        "ok": False,
        "cloudflare": cf,
        "playlist_id": None,
        "clips": [],
        "iframe_error": None,
        "iframe_sample": "",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    if cf:
        upsert_row(out, row)
        return row
    row["match_stage"] = match_stage_from_html(html)
    pid = _playlist_id(html)
    row["playlist_id"] = pid
    if pid and not skip_iframe:
        clips, icf = fetch_playlist_clips(pid)
        clips = _stamp_match(clips, mid)
        row["iframe_error"] = icf
        row["clips"] = _store_clips(row, clips)
    row["ok"] = True
    upsert_row(out, row)
    own = sum(1 for c in row["clips"] if str(c.get("match_id")) == mid)
    print(f"[match] {mid} playlist={pid} clips={len(row['clips'])} own={own}")
    top = sorted(row["clips"], key=lambda c: -(c.get("views") or 0))[:5]
    for c in top:
        print(f"   {c.get('views', 0):>8}  {c.get('player', '?'):12s} "
              f"R{c.get('round', '?')}  {c.get('label') or c.get('title')}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Allstar/HLTV Cloudflare probe (Popular events)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--max-matches", type=int, default=0, help="0 = all")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--abort-after-cf", type=int, default=5)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--match", default="",
        help="Single HLTV match id or URL (skips events sweep, upserts row)",
    )
    ap.add_argument("--skip-iframe", action="store_true", help="HLTV pages only; skip playlist fetch")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--events-out", type=Path, default=EVENTS_DEFAULT)
    ap.add_argument("--fresh", action="store_true", help="Wipe previous JSONL before scraping")
    ap.add_argument(
        "--enrich",
        action="store_true",
        help="Re-fetch Allstar playlists into an existing JSONL (no HLTV)",
    )
    ap.add_argument(
        "--fill-stage",
        action="store_true",
        help="Fill match_stage on an existing JSONL from ratings + HLTV match pages",
    )
    ap.add_argument(
        "--no-fetch",
        action="store_true",
        help="With --fill-stage, only use local ratings JSON (no Cloak)",
    )
    args = ap.parse_args()

    if args.fill_stage:
        stats = fill_jsonl_stages(
            args.out,
            fetch=not args.no_fetch,
            sleep=args.sleep,
            abort_after_cf=args.abort_after_cf,
        )
        print(json.dumps(stats))
        print(f"[fill-stage] wrote {args.out}")
        from shorts.fit_partial_stars import refresh_partial_stars

        stars = refresh_partial_stars()
        print(json.dumps({
            "stage": stars.get("stage"),
            "rows": stars.get("_rows"),
        }))
        return 0 if stats["cloudflare"] == 0 or stats["fetched"] or stats["from_ratings"] else 2

    if args.enrich:
        stats = enrich_jsonl(args.out, sleep=max(args.sleep, 0.2))
        print(json.dumps(stats))
        print(f"[enrich] wrote {args.out}")
        return 0 if stats["errors"] == 0 else 2

    from cloakbrowser import launch_persistent_context

    base = settings.hltv_base_url.rstrip("/")
    end = date.today()
    start = end - timedelta(days=args.days)
    if args.fresh and args.out.exists():
        args.out.unlink()
        print(f"[probe] wiped {args.out}")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[probe] cloak profile {PROFILE_DIR.resolve()}")
    print(f"[probe] window {start} .. {end}")

    ctx = launch_persistent_context(
        str(PROFILE_DIR.resolve()),
        headless=not args.headed,
        viewport={"width": 1920, "height": 1080},
        humanize=True,
        channel="chrome",
    )
    page = ctx.new_page()
    if args.match:
        try:
            row = scrape_one_match(page, args.match, base, out=args.out,
                                     skip_iframe=args.skip_iframe)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
        return 0 if row.get("ok") else 2
    stats = {
        "events_seen": 0,
        "events_popular": 0,
        "matches": 0,
        "ok": 0,
        "cloudflare": 0,
        "no_playlist": 0,
        "iframe_ok": 0,
        "clips": 0,
    }
    cf_streak = 0
    try:
        events, archive_cf = _collect_archive_events(page, base, start, end)
        stats["cloudflare"] += archive_cf
        if archive_cf and not events:
            print(json.dumps(stats))
            return 2
        seen_e = {e["event_id"] for e in events}
        for extra_url in (f"{base}/events", f"{base}/stats/events?years={end.year}"):
            time.sleep(args.sleep + random.random())
            html, cf = _goto(page, extra_url)
            if cf:
                print(f"[CF] {extra_url}: {cf}")
                stats["cloudflare"] += 1
                continue
            added = 0
            for e in _parse_events(html, base):
                if e["event_id"] in seen_e:
                    continue
                seen_e.add(e["event_id"])
                events.append(e)
                added += 1
            print(f"[probe] extra {extra_url} +{added}")
        stats["events_seen"] = len(events)
        popular = [e for e in events if e["popular"]]
        stats["events_popular"] = len(popular)
        args.events_out.parent.mkdir(parents=True, exist_ok=True)
        args.events_out.write_text(
            json.dumps(
                {"start": start.isoformat(), "end": end.isoformat(),
                 "events": events, "popular": popular},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[probe] archive events={len(events)} popular={len(popular)}")
        for e in popular:
            print(f"  keep {e['event_id']} {e['slug']}")
        if not popular:
            print("[probe] no popular events parsed from archive; stopping")
            print(json.dumps(stats))
            return 1

        matches: list[dict] = []
        seen_m: set[str] = set()
        for ev in popular:
            batch, mcf = _collect_event_matches(page, ev, base, args.sleep)
            stats["cloudflare"] += mcf
            if mcf:
                cf_streak += mcf
                if cf_streak >= args.abort_after_cf:
                    print("[probe] abort: consecutive Cloudflare")
                    break
            else:
                cf_streak = 0
            added = 0
            for row in batch:
                if row["match_id"] in seen_m:
                    continue
                seen_m.add(row["match_id"])
                matches.append(row)
                added += 1
            print(f"[probe] {ev['slug']}: +{added} (cumulative {len(matches)})")

        done = _load_done(args.out)
        pending = prioritize_pending(
            matches, done,
            listener_rows=_listener_unseen(done),
            max_matches=args.max_matches,
        )
        print(f"[probe] matches total={len(matches)} pending={len(pending)} already={len(done)}")

        for i, m in enumerate(pending, 1):
            if cf_streak >= args.abort_after_cf:
                break
            time.sleep(args.sleep + random.random())
            html, cf = _goto(page, m["url"])
            stats["matches"] += 1
            row = {
                **m,
                "ok": False,
                "cloudflare": cf,
                "playlist_id": None,
                "clips": [],
                "iframe_error": None,
                "iframe_sample": "",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            if cf:
                stats["cloudflare"] += 1
                cf_streak += 1
                print(f"[CF] {m['match_id']} {m['slug']}: {cf}  ({i}/{len(pending)})")
                _append(args.out, row)
                continue
            cf_streak = 0
            row["match_stage"] = match_stage_from_html(html)
            pid = _playlist_id(html)
            row["playlist_id"] = pid
            if not pid:
                stats["no_playlist"] += 1
                _append(args.out, row)
                print(f"[--] {m['match_id']} {m['slug']}: no Allstar playlist  ({i}/{len(pending)})")
                continue
            if not args.skip_iframe:
                clips, icf = fetch_playlist_clips(pid)
                clips = _stamp_match(clips, str(m["match_id"]))
                row["iframe_error"] = icf
                row["clips"] = _store_clips(row, clips)
                if not icf:
                    stats["iframe_ok"] += 1
                    stats["clips"] += len(row["clips"])
            row["ok"] = True
            stats["ok"] += 1
            _append(args.out, row)
            nclips = len(row["clips"])
            print(
                f"[ok] {m['match_id']} {m['slug']} playlist={pid} clips={nclips}  "
                f"({i}/{len(pending)})"
            )

        print(json.dumps(stats))
        print(f"[probe] wrote {args.out}")
        return 0 if stats["cloudflare"] == 0 or stats["ok"] else 2
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
