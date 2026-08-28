"""Select high-performing and typical videos for opening-sequence review."""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median


def _float(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0


def select(path: Path, channels: list[str]) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if _float(row, "age_days") >= 2 and _float(row, "duration_seconds") >= 300
        ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["channel"]].append(row)

    samples = []
    for channel in channels:
        items = grouped[channel]
        baseline = median(_float(row, "views_per_day") for row in items)
        candidates = sorted(items, key=lambda row: _float(row, "views_per_day"), reverse=True)
        choices = [
            ("breakout", candidates[0]),
            (
                "typical",
                min(items, key=lambda row: abs(_float(row, "views_per_day") - baseline)),
            ),
        ]
        for cohort, row in choices:
            samples.append(
                {
                    "cohort": cohort,
                    "channel": channel,
                    "video_id": row["video_id"],
                    "title": row["title"],
                    "views": int(_float(row, "views")),
                    "views_per_day": round(_float(row, "views_per_day"), 1),
                    "channel_median_views_per_day": round(baseline, 1),
                    "performance_index": round(_float(row, "views_per_day") / baseline, 2),
                    "url": row["url"],
                }
            )
    return samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--channels", nargs="+", required=True)
    args = parser.parse_args()
    samples = select(args.csv, args.channels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(samples, indent=2), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
