"""Build the pro POV dataset: one row per LIM pro POV video.

Target: views / views_per_day. Features: player, org, map, opp, opp_tier,
rating (HLTV ratings join), stage, tier. Writes .data/pro_pov_dataset.jsonl.

Usage:
    python scripts/shorts/build_pro_pov_dataset.py
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

from shorts.fit_clip_weights import load_roster_orgs  # noqa: E402
from shorts.pro_context import recognised_aliases  # noqa: E402
from shorts.pro_context import (derby_heat, event_tier, index_ratings,
                                kd_bucket, load_match_scores, lookup_rating,
                                normalize_stage, parse_kd_ratio,
                                parse_pro_title, rating_bucket,
                                series_context)

CHANNELS = (
    "LIM-CS POV | Pro Tournaments",
    "CAL CS POV",
    "CS2 POV DEMOS",
    "EDCS - POV",
    "LIM-CS POV",
)
# Own channel excluded from training: tiny base, promo-driven views
# (val MSE 1.2 vs 0.2-0.4 elsewhere). Revisit at 10x subs.
_FACEIT_RES = re.compile(r"\bfaceit\b|\belo\b|soloq", re.I)
HISTORY = ROOT / "exports" / "pov_market" / "video_history.csv"
OUT = ROOT / ".data" / "pro_pov_dataset.jsonl"
RANKING_PATH = ROOT / ".data" / "hltv_team_ranking.json"


def is_pro_style(title: str, channel: str) -> bool:
    if channel == "LIM-CS POV | Pro Tournaments":
        return True
    text = title or ""
    return bool(re.search(r"\bvs\b", text, re.I)) and not _FACEIT_RES.search(text)


def build() -> tuple[list[dict], dict]:
    orgs = load_roster_orgs()
    aliases = recognised_aliases(orgs)
    try:
        ranking = (json.loads(RANKING_PATH.read_text(encoding="utf-8"))
                   .get("teams") or {})
    except (OSError, json.JSONDecodeError, AttributeError):
        ranking = {}
    ratings = index_ratings()
    scores = load_match_scores()
    try:
        from hltv.update_team_demand import load_team_demand
        fixtures = load_team_demand().get("fixtures") or []
    except Exception:
        fixtures = []
    rows: list[dict] = []
    skipped = {"no_player": 0, "no_views": 0}
    with HISTORY.open(encoding="utf-8-sig", newline="") as fh:
        for source in csv.DictReader(fh):
            if str(source.get("channel") or "") not in CHANNELS:
                continue
            if not is_pro_style(str(source.get("title") or ""),
                                 str(source.get("channel") or "")):
                skipped["not_pro"] = skipped.get("not_pro", 0) + 1
                continue
            try:
                views = int(float(source.get("views") or 0))
                vpd = float(source.get("views_per_day") or 0)
            except (TypeError, ValueError):
                continue
            if views <= 0 or vpd <= 0:
                skipped["no_views"] += 1
                continue
            canon = aliases.get(str(source.get("primary_player") or "")
                                .strip().casefold())
            if not canon:
                skipped["no_player"] += 1
                continue
            player = canon.lower()
            org = orgs.get(player)
            parsed = parse_pro_title(str(source.get("title") or ""))
            game_map = parsed["map"] or str(source.get("map") or "").lower()
            team1, team2 = parsed["team1"], parsed["team2"]
            mine = (org or "").lower()
            opp = ""
            if team1 and team2:
                opp = team2 if team1.lower() == mine else team1
            elif team2:
                opp = team2
            stats, file_stage, entry = lookup_rating(ratings, team1, team2,
                                                      game_map, player)
            rating = stats.get("rating") or None
            rating_source = "hltv" if rating else ""
            if rating is None and isinstance(parsed.get("title_rating"),
                                             float):
                rating = parsed["title_rating"]
                rating_source = "title"
            kd = stats.get("kd")
            if kd is None and isinstance(parsed.get("title_kd"), float):
                kd = parsed["title_kd"]
            stage = parsed["stage"]
            if stage == "other" and file_stage:
                stage = normalize_stage(file_stage)
            series = series_context(entry, scores, org)
            heat = derby_heat(team1, team2, fixtures)
            rank = ranking.get(opp or "", 0) or 0
            opp_tier = ("top5" if rank and rank <= 5 else
                        "top10" if rank and rank <= 10 else
                        "top20" if rank and rank <= 20 else
                        "top30" if rank else "unranked")
            rows.append({
                "video_id": source.get("video_id"),
                "channel": source.get("channel"),
                "published_at": source.get("published_at"),
                "publish_weekday": source.get("publish_weekday"),
                "publish_hour_utc": source.get("publish_hour_utc"),
                "age_days": source.get("age_days"),
                "target_views": views,
                "target_vpd": round(vpd, 2),
                "player": player,
                "org": org,
                "map": game_map or "unknown",
                "opp": opp or "unknown",
                "opp_tier": opp_tier,
                "rating": rating,
                "rating_source": rating_source,
                "rating_bucket": rating_bucket(rating),
                "kd": kd,
                "kd_bucket": kd_bucket(kd),
                "decider": series["decider"],
                "won": series["won"],
                "ot": series["ot"],
                "derby_views": heat,
                "stage": stage,
                "tier": event_tier(str(source.get("title") or "")),
                "title": source.get("title"),
            })
    return rows, skipped


def main() -> int:
    rows, skipped = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with_rating = sum(1 for r in rows if r["rating"] is not None)
    print(f"rows={len(rows)} with_hltv_rating={with_rating} skipped={skipped}")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
