"""Sole candidate selector: the match's most-viewed Allstar clip we can render.

The probe store (.data/allstar_hltv_probe.jsonl) holds, per HLTV match, the
Allstar Trending playlist scraped from the match page (clip_id, steamid,
player, match_id, round, views, ...). For every match we have demos and a
pending recognized-pro candidate for, take that match's most-viewed clip
whose (steamid, round) matches a candidate, then rank those match winners
globally by clip views. No view-prediction model involved.
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / ".data" / "allstar_hltv_probe.jsonl"


def _match_id_from_demo(demo: Path) -> str | None:
    try:
        parent = demo.resolve().parent.name
    except OSError:
        return None
    head = parent.split("-", 1)[0]
    return head if head.isdigit() else None


def load_probe_rows(path: Path = PROBE) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ok") and row.get("clips"):
            rows.append(row)
    return rows


def rank_candidates_by_allstar(candidates, rows: list[dict] | None = None):
    """Filter to candidate<->Allstar matches; per match keep its top clip's
    candidate; rank globally by that clip's views. Returns (ranked, report)."""
    rows = load_probe_rows() if rows is None else rows
    by_match: dict[str, list[dict]] = {}
    for row in rows:
        mid = str(row.get("match_id") or "")
        if mid:
            by_match.setdefault(mid, []).extend(c for c in row["clips"] if isinstance(c, dict))
    # candidate lookup: (match_id, steamid, round) -> candidate
    cand_by_key: dict[tuple[str, str, object], object] = {}
    for c in candidates:
        demo = Path(c.payload.get("demo_path") or "")
        if not demo.is_absolute():
            demo = ROOT / demo
        mid = _match_id_from_demo(demo)
        if mid is None:
            continue
        short = c.short
        try:
            key = (mid, str(short.get("pov_steam_id")), int(short.get("round")))
        except (TypeError, ValueError):
            continue
        cand_by_key.setdefault(key, c)
    picked: dict[str, tuple[object, dict]] = {}
    for mid, clips in by_match.items():
        for clip in sorted(clips, key=lambda c: -(c.get("views") or 0)):
            try:
                key = (mid, str(clip.get("steamid") or ""), int(clip.get("round")))
            except (TypeError, ValueError):
                continue
            cand = cand_by_key.get(key)
            if cand is not None:
                picked[mid] = (cand, clip)
                break
    ranked = sorted(picked.values(), key=lambda pair: (-(pair[1].get("views") or 0), pair[0].key))
    report = {
        "probe_matches": len(by_match),
        "candidates": len(candidates),
        "matched": len(ranked),
        "unmatched": len(candidates) - len({id(c) for c, _ in ranked}),
    }
    out = []
    for cand, clip in ranked:
        # Surface the Allstar views through the existing Candidate fields so
        # the ledger keeps an honest, human-readable number.
        new = replace(cand, log_views=math.log(max(clip.get("views") or 1, 1)))
        out.append((new, clip))
    return out, report
