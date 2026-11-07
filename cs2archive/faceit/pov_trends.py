"""Early POV demand: conservative total-view outperformance, no maturity wait."""
from datetime import datetime, timedelta
from statistics import median
import math

TREND_VERSION = 1
MIN_HOURS = 6
MAX_DAYS = 7
BASELINE_DAYS = 30
MIN_PEERS = 5
MIN_VIEWS = 5000
MIN_RATIO = 2.0
MAX_CAPTURE_HOURS = 4


def _stamp(raw):
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except ValueError:
        return None


def find_early_trends(rows: list[dict], aliases: dict[str, str], now: datetime) -> dict:
    """Compare a fresh upload to older observed uploads on its own channel.

    Older-peer total views are a conservative hurdle, not an estimate of final
    views. Capture ages, rather than today's ages on frozen counts, establish
    which observations really had at least as much time to collect views.
    """
    latest = {}
    for row in sorted(rows, key=lambda r: str(r.get("captured_at") or "")):
        if row.get("video_id"):
            latest[row["video_id"]] = row
    observations = []
    for row in latest.values():
        published, captured = _stamp(row.get("published_at")), _stamp(row.get("captured_at"))
        try:
            views, duration = float(row.get("views") or 0), float(row.get("duration_seconds") or 0)
        except (ValueError, TypeError):
            continue
        if (not published or not captured or published > captured or captured > now
                or not math.isfinite(views) or not math.isfinite(duration)
                or views < 0 or duration < 300 or row.get("channel") in (None, "", "CS2 Archive")
                or now - published > timedelta(days=BASELINE_DAYS)):
            continue
        observations.append((row, published, captured, views))
    trends = {}
    for row, published, captured, views in observations:
        age = now - published
        player = aliases.get(str(row.get("primary_player") or "").strip().casefold())
        if (not player or not timedelta(hours=MIN_HOURS) <= age <= timedelta(days=MAX_DAYS)
                or now - captured > timedelta(hours=MAX_CAPTURE_HOURS) or views < MIN_VIEWS):
            continue
        observed_age = captured - published
        peers = [peer_views for peer, peer_pub, peer_cap, peer_views in observations
                 if peer["video_id"] != row["video_id"] and peer["channel"] == row["channel"]
                 and peer_cap - peer_pub >= max(observed_age, timedelta(days=2))]
        if len(peers) < MIN_PEERS:
            continue
        baseline = median(peers)
        if baseline <= 0:
            continue
        ratio = views / baseline
        if ratio < MIN_RATIO:
            continue
        key = player.casefold()
        if key in trends and trends[key]["ratio"] >= ratio:
            continue
        trends[key] = dict(video_id=row["video_id"], channel=row["channel"],
                           title=row.get("title", ""), views=views, ratio=round(ratio, 4),
                           baseline_views=baseline, peer_videos=len(peers),
                           age_hours=round(age.total_seconds() / 3600, 2),
                           observed_at=captured.isoformat(),
                           expires_at=min(captured + timedelta(hours=MAX_CAPTURE_HOURS),
                                          published + timedelta(days=MAX_DAYS)).isoformat())
    return trends


def read_early_trend(payload: dict, nick: str, now: datetime) -> dict | None:
    """Fail closed on expired or malformed evidence, independently of star age."""
    if not isinstance(payload, dict):
        return None
    method, trends = payload.get("method"), payload.get("early_trends")
    if (not isinstance(method, dict) or method.get("early_trend_version") != TREND_VERSION
            or not isinstance(trends, dict)):
        return None
    trend = trends.get(str(nick).strip().casefold())
    if not isinstance(trend, dict):
        return None
    observed, expires = _stamp(trend.get("observed_at")), _stamp(trend.get("expires_at"))
    try:
        valid = (observed and expires and observed <= now < expires
                 and now - observed <= timedelta(hours=MAX_CAPTURE_HOURS)
                 and float(trend["ratio"]) >= MIN_RATIO and float(trend["views"]) >= MIN_VIEWS
                 and int(trend["peer_videos"]) >= MIN_PEERS)
    except (ValueError, TypeError, KeyError):
        return None
    return trend if valid else None
