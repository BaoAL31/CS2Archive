"""One-time: scrape map scores + winner + OT flags for rated HLTV matches.

Reads match URLs from demos/analysis/*ratings*.json, caches to
.data/match_scores.json keyed by HLTV match id. Rerun to extend.

Usage:
    python scripts/shorts/scrape_match_scores.py [--limit N]
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from cloakbrowser import launch_persistent_context  # noqa: E402
from shorts.scrape_allstar_hltv import _goto  # noqa: E402

OUT = ROOT / ".data" / "match_scores.json"
SCORE_RE = re.compile(r"(\d{1,2})\s*[-–:—]\s*(\d{1,2})")


def parse_scores(html: str) -> dict:
    """Map scores, winner side, OT flag from an HLTV match page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    maps: list[dict] = []
    for holder in soup.select("div.mapholder"):
        name = holder.select_one("div.mapname")
        sides = holder.select("div.results-team-score")
        if len(sides) < 2:
            continue
        try:
            left, right = int(sides[0].get_text()), int(sides[1].get_text())
        except ValueError:
            continue
        teams = [t.get_text(strip=True) for t in
                 holder.select("div.results-teamname")]
        won_left = "won" in " ".join(holder.select_one(
            "div.results-left").get("class", [])) if holder.select_one(
            "div.results-left") else False
        maps.append({
            "map": name.get_text(strip=True) if name else "",
            "left_team": teams[0] if len(teams) > 0 else "",
            "right_team": teams[1] if len(teams) > 1 else "",
            "left": left, "right": right,
            "winner_side": ("left" if left > right else
                              "right" if right > left else ""),
            "ot": left + right > 24 or max(left, right) > 13})
    left_maps = sum(1 for entry in maps if entry["winner_side"] == "left")
    right_maps = sum(1 for entry in maps if entry["winner_side"] == "right")
    return {"maps": maps,
            "decider": len(maps) >= 3,
            "ot": any(entry["ot"] for entry in maps),
            "left_maps": left_maps, "right_maps": right_maps,
            "winner_side": ("left" if left_maps > right_maps else
                              "right" if right_maps > left_maps else "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    try:
        cache = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    urls: dict[str, str] = {}
    for path in glob.glob(str(ROOT / "demos" / "analysis" / "*ratings*.json")):
        try:
            url = json.loads(Path(path).read_text(encoding="utf-8")).get("url") or ""
        except (OSError, json.JSONDecodeError):
            continue
        hit = re.search(r"/matches/(\d+)/", url)
        if hit and hit.group(1) not in cache:
            urls[hit.group(1)] = url
    items = list(urls.items())
    if args.limit:
        items = items[:args.limit]
    print(f"cached={len(cache)} todo={len(items)}", flush=True)
    if not items:
        return 0
    ctx = launch_persistent_context(
        str((ROOT / ".sessions" / "hltv-cloak").resolve()),
        headless=True, viewport={"width": 1920, "height": 1080},
        humanize=True, channel="chrome")
    page = ctx.new_page()
    try:
        for i, (mid, url) in enumerate(items, 1):
            html, cf = _goto(page, url)
            if cf:
                print(f"[CF] {mid}", flush=True)
                continue
            cache[mid] = {"url": url, **parse_scores(html)}
            print(f"[{i}/{len(items)}] {mid} maps={len(cache[mid]['maps'])} "
                  f"decider={cache[mid]['decider']} ot={cache[mid]['ot']}",
                  flush=True)
            time.sleep(2)
            if i % 10 == 0:
                OUT.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    OUT.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    print(f"wrote {OUT} ({len(cache)} matches)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
