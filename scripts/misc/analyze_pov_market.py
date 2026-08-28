"""Find performance patterns in a POV market CSV snapshot."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import median


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)
    )
    return round(numerator / denominator, 3) if denominator else None


def _group_report(rows: list[dict], key, minimum: int = 3) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        label = key(row)
        if label not in (None, "", "[]"):
            groups[str(label)].append(row)
    report = []
    for label, items in groups.items():
        if len(items) < minimum:
            continue
        report.append(
            {
                "label": label,
                "videos": len(items),
                "median_performance_index": round(
                    median(item["performance_index"] for item in items), 2
                ),
                "median_views_per_day": round(
                    median(item["views_per_day_num"] for item in items), 1
                ),
                "top_quartile_rate": round(
                    sum(item["is_top_quartile"] for item in items) / len(items), 3
                ),
            }
        )
    return sorted(
        report,
        key=lambda group: (group["median_performance_index"], group["videos"]),
        reverse=True,
    )


def analyze(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))

    rows = []
    excluded_fresh = 0
    excluded_short = 0
    for source in source_rows:
        age = _number(source.get("age_days"))
        velocity = _number(source.get("views_per_day"))
        duration = _number(source.get("duration_seconds")) or 0
        if duration < 300:
            excluded_short += 1
            continue
        if age is None or velocity is None or age < 2:
            excluded_fresh += 1
            continue
        row = dict(source)
        row["age_days_num"] = age
        row["views_per_day_num"] = velocity
        row["views_num"] = _number(source.get("views")) or 0
        rows.append(row)

    by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_channel[row["channel"]].append(row)

    channel_summary = []
    for channel, items in by_channel.items():
        velocities = [item["views_per_day_num"] for item in items]
        baseline = median(velocities)
        top_threshold = _quantile(velocities, 0.75)
        for item in items:
            item["performance_index"] = item["views_per_day_num"] / baseline
            item["is_top_quartile"] = item["views_per_day_num"] >= top_threshold
        channel_summary.append(
            {
                "channel": channel,
                "videos_analyzed": len(items),
                "median_views_per_day": round(baseline, 1),
                "top_quartile_threshold_views_per_day": round(top_threshold, 1),
            }
        )

    title_length = lambda row: (
        "short (<55)"
        if (_number(row.get("title_characters")) or 0) < 55
        else "medium (55-79)"
        if (_number(row.get("title_characters")) or 0) < 80
        else "long (80+)"
    )
    hour_bucket = lambda row: (
        "00-05 UTC"
        if int(float(row["publish_hour_utc"])) < 6
        else "06-11 UTC"
        if int(float(row["publish_hour_utc"])) < 12
        else "12-17 UTC"
        if int(float(row["publish_hour_utc"])) < 18
        else "18-23 UTC"
    )
    party = lambda row: row.get("party_type") or "solo"
    groups = {
        "primary_players": _group_report(rows, lambda row: row.get("primary_player"), 3),
        "party_type": _group_report(rows, party, 3),
        "maps": _group_report(rows, lambda row: row.get("map"), 3),
        "title_length": _group_report(rows, title_length, 3),
        "publish_weekday": _group_report(rows, lambda row: row.get("publish_weekday"), 3),
        "publish_hour_utc": _group_report(rows, hour_bucket, 3),
        "voicecomms": _group_report(
            rows, lambda row: "voicecomms" if row.get("has_voicecomms") == "True" else "no voicecomms"
        ),
        "faceit": _group_report(
            rows, lambda row: "FACEIT in title" if row.get("has_faceit") == "True" else "no FACEIT"
        ),
    }

    stopwords = {
        "the", "with", "and", "for", "pov", "cs2", "demo", "faceit", "stream",
        "plays", "play", "kills", "elo", "avg", "top", "new", "voicecomms",
    }
    token_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        tokens = {
            token
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]+", row["title"].lower())
            if token not in stopwords and len(token) >= 3
        }
        for token in tokens:
            token_rows[token].append(row)
    title_terms = []
    for token, items in token_rows.items():
        if len(items) < 4:
            continue
        title_terms.append(
            {
                "term": token,
                "videos": len(items),
                "median_performance_index": round(
                    median(item["performance_index"] for item in items), 2
                ),
                "top_quartile_rate": round(
                    sum(item["is_top_quartile"] for item in items) / len(items), 3
                ),
            }
        )
    title_terms.sort(
        key=lambda term: (term["median_performance_index"], term["videos"]),
        reverse=True,
    )

    correlations = {}
    numeric_fields = {
        "title_characters": "title_characters",
        "kills": "kills",
        "deaths": "deaths",
        "elo": "elo",
        "duration_seconds": "duration_seconds",
    }
    for label, field in numeric_fields.items():
        pairs = []
        for row in rows:
            value = _number(row.get(field))
            if value is not None and row["performance_index"] > 0:
                pairs.append((value, math.log(row["performance_index"])))
        correlations[label] = {"r": _pearson(pairs), "videos": len(pairs)}

    top_videos = sorted(rows, key=lambda row: row["performance_index"], reverse=True)[:12]
    top_videos = [
        {
            "channel": row["channel"],
            "title": row["title"],
            "primary_player": row.get("primary_player"),
            "party_type": row.get("party_type"),
            "secondary_players": json.loads(row.get("secondary_players") or "[]"),
            "map": row.get("map"),
            "age_days": round(row["age_days_num"], 1),
            "views": int(row["views_num"]),
            "views_per_day": round(row["views_per_day_num"], 1),
            "performance_index": round(row["performance_index"], 2),
            "url": row["url"],
        }
        for row in top_videos
    ]

    return {
        "source": str(path),
        "method": {
            "source_videos": len(source_rows),
            "videos_analyzed": len(rows),
            "excluded_under_5_minutes": excluded_short,
            "excluded_younger_than_2_days": excluded_fresh,
            "performance_index": "video views/day divided by its channel median views/day",
            "minimum_group_size": 3,
        },
        "channels": sorted(channel_summary, key=lambda item: item["channel"]),
        "groups": groups,
        "title_terms": title_terms[:20],
        "correlations_with_log_performance": correlations,
        "top_videos": top_videos,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = analyze(args.csv)
    output = args.out or args.csv.with_name(f"{args.csv.stem}_analysis.json")
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
