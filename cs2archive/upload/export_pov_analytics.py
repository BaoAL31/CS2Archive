"""Read-only POV outcomes over seven complete Pacific calendar days.

Run after ``python -m cs2archive.upload.auth_youtube_analytics``:
    python -m cs2archive.upload.export_pov_analytics --days 60

The partial publication day is excluded. A three-day reporting buffer avoids
grading the newest uploads. Analytics values remain estimates. Impressions and
thumbnail CTR require Studio; this exporter never substitutes views for them.
API contract: https://developers.google.com/youtube/analytics/channel_reports
Day boundaries: https://developers.google.com/youtube/analytics/dimensions#day
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PACIFIC = ZoneInfo("America/Los_Angeles")
METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"


def analysis_window(published_at: str, now: datetime) -> tuple[str, str] | None:
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ValueError("publication timestamp must include a timezone")
    start = published.astimezone(PACIFIC).date() + timedelta(days=1)
    end = start + timedelta(days=6)
    if end > now.astimezone(PACIFIC).date() - timedelta(days=3):
        return None
    return start.isoformat(), end.isoformat()


def report_values(payload: dict) -> dict:
    """An empty report is unavailable, not proof of zero activity."""
    rows = payload.get("rows") or []
    if not rows:
        return {}
    if len(rows) != 1:
        raise ValueError("expected one aggregate Analytics row")
    return dict(zip([column["name"] for column in payload["columnHeaders"]], rows[0]))


def execute(request):
    from googleapiclient.errors import HttpError
    for attempt in range(1, 4):
        print(f"[analytics] request attempt {attempt}/3", file=sys.stderr)
        try:
            return request.execute(num_retries=0)
        except HttpError as exc:
            if exc.resp.status not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            print(f"[analytics] attempt {attempt}/3 failed; retrying", file=sys.stderr)
            time.sleep(attempt)
    raise RuntimeError("Analytics retry exhausted")


def services():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token = ROOT / "token_youtube_analytics.json"
    guidance = "Run python -m cs2archive.upload.auth_youtube_analytics and complete Google consent."
    if not token.is_file():
        raise RuntimeError(f"Analytics token is missing. {guidance}")
    credentials = Credentials.from_authorized_user_file(str(token))
    try:
        if not credentials.valid:
            credentials.refresh(Request())
    except RefreshError:
        raise RuntimeError(f"Analytics login expired. {guidance}") from None
    return (build("youtube", "v3", credentials=credentials, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False))


def collect(history: list[dict], youtube, analytics, now: datetime, days: int,
            *, diagnostics: bool = False) -> dict:
    from cs2archive.faceit.update_player_demand import recognised_aliases
    from cs2archive.faceit.gate_forward import OWN_CHANNEL
    channels = execute(youtube.channels().list(part="id", mine=True)).get("items", [])
    if len(channels) != 1:
        raise RuntimeError("Expected one authenticated YouTube channel")
    channel_id = channels[0]["id"]
    latest = {}
    for row in sorted(history, key=lambda item: str(item.get("captured_at") or "")):
        if row.get("channel") == OWN_CHANNEL and row.get("video_id"):
            latest[row["video_id"]] = row
    aliases = recognised_aliases()
    outcomes, pending = [], []
    for row in sorted(latest.values(), key=lambda item: item.get("published_at", "")):
        if row.get("channel_id") != channel_id:
            raise RuntimeError("Analytics login does not match the own-channel history; refusing to mix channels")
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        if published < now - timedelta(days=days):
            continue
        player = aliases.get(str(row.get("primary_player") or "").casefold())
        title = row.get("title", "")
        if (not player or float(row.get("duration_seconds") or 0) < 300
                or not ("faceit" in title.lower() or " vs " in title.lower())):
            continue
        window = analysis_window(row["published_at"], now)
        if window is None:
            pending.append(row["video_id"])
            continue
        start, end = window
        payload = execute(analytics.reports().query(
            ids=f"channel=={channel_id}", startDate=start, endDate=end,
            filters=f"video=={row['video_id']}", metrics=METRICS))
        values = report_values(payload)
        outcomes.append(dict(video_id=row["video_id"], title=title, player=player,
                             format="FACEIT" if "faceit" in title.lower() else "HLTV",
                             published_at=row["published_at"], start_date=start, end_date=end,
                             status="reported" if values else "unavailable", metrics=values,
                             raw_report=payload))
        if diagnostics:
            for label, dimension, metrics in (
                ("traffic_sources", "insightTrafficSourceType", "views,estimatedMinutesWatched"),
                ("retention", "elapsedVideoTimeRatio", "audienceWatchRatio,relativeRetentionPerformance"),
            ):
                outcomes[-1][label] = execute(analytics.reports().query(
                    ids=f"channel=={channel_id}", startDate=start, endDate=end,
                    filters=f"video=={row['video_id']}", dimensions=dimension, metrics=metrics))
    groups = {}
    for label in ("all", "FACEIT", "HLTV"):
        items = [item for item in outcomes if item["status"] == "reported"
                 and (label == "all" or item["format"] == label)]
        groups[label] = dict(videos=len(items), median_views=median([x["metrics"]["views"] for x in items]) if items else None,
                            watch_minutes=sum(x["metrics"]["estimatedMinutesWatched"] for x in items))
    return dict(captured_at=now.isoformat(), channel_id=channel_id,
                measurement="seven complete Pacific days after publication day; three-day reporting buffer",
                unavailable_metrics=["thumbnail_impressions", "thumbnail_ctr"],
                pending=pending, groups=groups, outcomes=outcomes)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=ROOT / "exports/pov_market/own/video_history.csv")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--diagnostics", action="store_true", help="also export traffic sources and retention curves")
    parser.add_argument("--out", type=Path, default=ROOT / "exports/pov_market/pov_analytics.json")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    youtube, analytics = services()
    with args.history.open(encoding="utf-8-sig", newline="") as handle:
        history = list(csv.DictReader(handle))
    report = collect(history, youtube, analytics, datetime.now(timezone.utc), args.days,
                     diagnostics=args.diagnostics)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(".tmp")
    temp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temp.replace(args.out)
    print(json.dumps(report["groups"], indent=2))
    print(args.out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
