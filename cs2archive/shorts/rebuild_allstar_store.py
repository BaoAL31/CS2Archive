"""One-off rebuild of the Allstar probe store (unanimous council 2026-10-07).

Rules: dedupe clips by clip_id keeping the max-views snapshot (file order as
recency tiebreak); re-derive each clip's match from clip.match_id; drop
cross-playlist attachments; drop clipless ok=False rows; re-derive event_slug
from the match slug (null when unresolvable, never wrong); stamp observed_at
from the source row; views_per_day stays null without a publication time.

Usage: python -m cs2archive.shorts.rebuild_allstar_store [--dry-run]
Writes .data/allstar_rebuild_report.json. The previous file is backed up to
.data/allstar_hltv_probe.jsonl.bak before replacement.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / ".data" / "allstar_hltv_probe.jsonl"

_EVENT_PATTERNS = (
    "blast-open", "blast-premier", "blast-bounty", "blast-tv",
    "esports-world-cup", "ewc", "major", "iem-", "esl-pro-league",
    "pgl-", "starladder", "star-ladder", "rmr",
)


def derive_event_slug(slug: str) -> str | None:
    low = str(slug or "").lower()
    for pat in _EVENT_PATTERNS:
        i = low.find(pat)
        if i >= 0:
            return low[i:]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    rows: list[dict] = []
    corrupt = 0
    for line in STORE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if isinstance(r, dict):
            rows.append(r)

    # Clip-level dedupe: max views wins; later file order wins ties (recency).
    best = {}
    for order, row in enumerate(rows):
        for clip in row.get("clips") or []:
            if not isinstance(clip, dict) or not clip.get("clip_id"):
                continue
            try:
                views = float(clip.get("views") or 0)
            except (TypeError, ValueError):
                continue
            key = str(clip["clip_id"])
            prev = best.get(key)
            if prev is None or views > prev[0] or (views == prev[0] and order >= prev[1]):
                best[key] = (views, order, clip, row)

    groups: dict = {}
    dropped_no_match = 0
    for _views, _order, clip, row in best.values():
        mid = str(clip.get("match_id") or "")
        if not mid:
            dropped_no_match += 1
            continue
        clip = dict(clip)
        if not clip.get("observed_at"):
            clip["observed_at"] = row.get("scraped_at")
        clip.setdefault("views_per_day", None)
        clip.setdefault("views_per_day_provenance", "unavailable-no-publication-time")
        g = groups.setdefault(mid, {"clips": [], "rows": []})
        g["clips"].append(clip)
        g["rows"].append(row)

    out_rows = []
    dropped_empty = 0
    for mid in sorted(groups, key=lambda m: (int(m) if m.isdigit() else 0, m)):
        g = groups[mid]
        # Metadata from this match's own fixture row first (latest ok),
        # else any row that carried its clips. Never inherit a foreign slug.
        own = [r for r in g["rows"] if str(r.get("match_id")) == mid]
        cands = [r for r in own if r.get("ok")] or own or [r for r in g["rows"] if r.get("ok")] or g["rows"]
        meta = cands[-1]
        slug = str(meta.get("slug") or "")
        if str(meta.get("match_id")) != mid:
            slug = ""  # foreign row: do not inherit its slug/url/stage
        out_rows.append({
            "match_id": mid,
            "slug": slug,
            "url": meta.get("url") or (f"https://www.hltv.org/matches/{mid}/{slug}" if slug else ""),
            "event_slug": derive_event_slug(slug),
            "ok": bool(meta.get("ok")),
            "cloudflare": meta.get("cloudflare"),
            "playlist_id": meta.get("playlist_id"),
            "clips": sorted(g["clips"], key=lambda c: -float(c.get("views") or 0)),
            "match_stage": meta.get("match_stage"),
            "scraped_at": meta.get("scraped_at"),
        })
        if not g["clips"]:
            dropped_empty += 1

    report = {
        "input_rows": len(rows),
        "corrupt_lines": corrupt,
        "unique_clips": len(best),
        "dropped_no_match_id": dropped_no_match,
        "output_match_rows": len(out_rows),
        "output_clips": sum(len(r["clips"]) for r in out_rows),
        "null_event_slugs": sum(1 for r in out_rows if not r["event_slug"]),
    }
    print(json.dumps(report, indent=2))
    (ROOT / ".data" / "allstar_rebuild_report.json").write_text(json.dumps(report, indent=2))
    if args.dry_run:
        return 0
    shutil.copy2(STORE, STORE.with_suffix(".jsonl.bak"))
    tmp = STORE.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_rows), encoding="utf-8")
    tmp.replace(STORE)
    print(f"replaced {STORE} (backup at .bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
