"""Download unique HLTV matches for LIM POV dataset rows (Cloak).

Reads lobby mapstats URLs from video_history.csv, resolves /matches/<id>/
from the stats page (one browser session), skips folders that already have
a .dem, then acquire_match for each unique match.

Usage:
    python scripts/shorts/download_pov_demos.py [--limit N]
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

HISTORY = ROOT / "exports" / "pov_market" / "video_history.csv"
CACHE = ROOT / ".data" / "pov_mapstats_matches.json"
_LOBBY = re.compile(
    r"https?://(?:www\.)?hltv\.org/stats/matches/mapstatsid/\d+/[^\s<>\"]+",
    re.I,
)
_MATCH = re.compile(
    r"(?:https?://(?:www\.)?hltv\.org)?/matches/(\d+)/([a-z0-9\-]+)", re.I)


_STATS_TEAMS = re.compile(r"mapstatsid/\d+/([^/?#]+)", re.I)


def match_url_from_stats_html(html: str, stats_url: str = "") -> str | None:
    """Pick the match-page URL, not a short related-match href.

    Stats pages list a 6-digit `/matches/122371/nrg-vs-falcons` first. The
    real CS2 match is `/matches/2389652/nrg-vs-falcons-iem-krakw-2026`.
    """
    want = ""
    hit = _STATS_TEAMS.search(stats_url or "")
    if hit:
        want = hit.group(1).lower()
    best: str | None = None
    best_key = (-1, -1)
    for found in _MATCH.finditer(html or ""):
        mid, slug = found.group(1), found.group(2)
        sl = slug.lower()
        if slug.isdigit() or "mapstatsid" in sl or "-vs-" not in sl:
            continue
        if want and "-vs-" in want:
            t1, t2 = want.split("-vs-", 1)
            if not (sl.startswith(f"{t1}-vs-{t2}-")
                    or sl.startswith(f"{t2}-vs-{t1}-")):
                continue
        elif sl.count("-") < 3:
            continue
        key = (len(slug), int(mid))
        if key > best_key:
            best_key = key
            best = f"https://www.hltv.org/matches/{mid}/{slug}"
    return best


def _cache_is_match_page(url: str | None) -> bool:
    if not url:
        return False
    found = _MATCH.search(url)
    if not found:
        return False
    return len(found.group(1)) >= 7 and found.group(2).count("-") >= 3


def lobby_urls(path: Path | None = None) -> list[str]:
    seen: list[str] = []
    got: set[str] = set()
    with (path or HISTORY).open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("channel") or "") != "LIM-CS POV | Pro Tournaments":
                continue
            hit = _LOBBY.search(str(row.get("description") or ""))
            if not hit:
                continue
            url = hit.group(0).rstrip(").,")
            if url not in got:
                got.add(url)
                seen.append(url)
    return seen


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_match_urls(stats_urls: list[str]) -> dict[str, str | None]:
    """One Cloak session for all stats pages. Cache to disk as we go."""
    from cloakbrowser import launch_persistent_context
    from scrapers.hltv_acquire import DEFAULT_PROFILE_DIR, _navigate_with_retry

    cache = _load_cache()
    missing = [u for u in stats_urls if not _cache_is_match_page(cache.get(u))]
    print(f"resolve stats: cached={len(stats_urls)-len(missing)} "
          f"todo={len(missing)}", flush=True)
    if not missing:
        return cache
    profile = DEFAULT_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    ctx = launch_persistent_context(
        str(profile.resolve()),
        headless=True,
        viewport={"width": 1920, "height": 1080},
        humanize=True,
        channel="chrome",
    )
    try:
        page = ctx.new_page()
        for i, url in enumerate(missing, 1):
            print(f"  resolve [{i}/{len(missing)}] {url}", flush=True)
            try:
                html = _navigate_with_retry(
                    page, url,
                    wait_selector='a[href*="/matches/"]',
                    timeout_ms=60_000,
                )
                cache[url] = match_url_from_stats_html(html, url)
                print(f"    -> {cache[url] or 'NONE'}", flush=True)
            except Exception as exc:
                print(f"    FAIL {exc}", flush=True)
                cache[url] = None
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return cache


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resolve-only", action="store_true")
    args = ap.parse_args()
    urls = lobby_urls()
    if args.limit:
        urls = urls[: args.limit]
    print(f"unique mapstats urls={len(urls)}", flush=True)

    from scrapers.hltv_acquire import acquire_match, match_demo_dir, match_slug_from_url
    from models import DownloadStatus

    cache = resolve_match_urls(urls)
    if args.resolve_only:
        n = sum(1 for u in urls if cache.get(u))
        print(f"resolved {n}/{len(urls)}", flush=True)
        return 0

    matches: list[str] = []
    seen: set[str] = set()
    for stats_url in urls:
        match_url = cache.get(stats_url)
        if not match_url or match_url in seen:
            continue
        seen.add(match_url)
        matches.append(match_url)
    print(f"unique matches={len(matches)}", flush=True)

    ok = skip = fail = 0
    for i, match_url in enumerate(matches, 1):
        slug = match_slug_from_url(match_url)
        folder = match_demo_dir(slug)
        print(f"[{i}/{len(matches)}] {slug}", flush=True)
        if list(folder.glob("*.dem")):
            print("  SKIP demos exist", flush=True)
            skip += 1
            continue
        try:
            result = acquire_match(match_url, force=False, headless=True)
            if result.status == DownloadStatus.FAILED:
                print(f"  FAIL {result.error}", flush=True)
                fail += 1
                continue
            if list(folder.glob("*.dem")):
                ok += 1
                print("  OK", flush=True)
            elif result.status == DownloadStatus.SKIPPED:
                skip += 1
                print("  SKIP history (no .dem in folder)", flush=True)
            else:
                fail += 1
                print("  FAIL no .dem after acquire", flush=True)
        except Exception as exc:
            print(f"  FAIL {exc}", flush=True)
            fail += 1
    print(f"done ok={ok} skip={skip} fail={fail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
