"""Probe: scrape Allstar playlists from HLTV match pages of Popular events.

Uses one CloakBrowser persistent context (same `.sessions/hltv-cloak/` as
demo download) to find match playlist ids, then the Allstar playlist API for
clips (steamid, HLTV nick, match id, views). Writes JSONL as it goes.

    python scripts/shorts/scrape_allstar_hltv.py
    python scripts/shorts/scrape_allstar_hltv.py --max-matches 10
    python scripts/shorts/scrape_allstar_hltv.py --enrich
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
from shorts.clip_observation import observations_from_match_row  # noqa: E402

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
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("div.match-info-box div.text") or soup.select_one(
        "div.map-info-wrap ul li"
    )
    if not el:
        return None
    text = el.get_text(strip=True)
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Allstar/HLTV Cloudflare probe (Popular events)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--max-matches", type=int, default=0, help="0 = all")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--abort-after-cf", type=int, default=5)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--skip-iframe", action="store_true", help="HLTV pages only; skip playlist fetch")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--events-out", type=Path, default=EVENTS_DEFAULT)
    ap.add_argument("--fresh", action="store_true", help="Wipe previous JSONL before scraping")
    ap.add_argument(
        "--enrich",
        action="store_true",
        help="Re-fetch Allstar playlists into an existing JSONL (no HLTV)",
    )
    args = ap.parse_args()

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
