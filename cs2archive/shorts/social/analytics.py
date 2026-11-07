"""Authorized YouTube Analytics for our own channel, kept strictly separate.

Public Data API views are *cumulative counts at observation time*. Analytics
reports are a different thing entirely — windowed, owner-only, and not
comparable:

* they are stored in ``analytics_snapshots`` with the window recorded and a
  ``metric_family`` of ``youtube_analytics``;
* they are **never** read by the public-views target loader (see
  ``train.TARGET_METRIC`` / ``load_platform_rows``), so impressions/CTR/retention
  can never leak into that label or be silently mixed with it;
* the window is the complete-day window used by the existing
  ``export_pov_analytics`` (first full Pacific day after publication for 7 days),
  so a partial publication day is never graded.

Credentials: ``token_youtube_analytics.json`` at the repo root (scope
``yt-analytics.readonly`` + ``youtube.readonly``), created by
``python -m cs2archive.upload.auth_youtube_analytics``. If the token or the
needed scope is missing this refuses cleanly — it never falls back to another
metric family or to estimated numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cs2archive.shorts.social.safety import iso, parse_int, sha256_text, utcnow

ROOT = Path(__file__).resolve().parents[3]
TOKEN_PATH = ROOT / "token_youtube_analytics.json"
METRIC_FAMILY = "youtube_analytics"
REQUIRED_SCOPES = ("https://www.googleapis.com/auth/yt-analytics.readonly",)
METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
ALL_METRICS = METRICS + ",impressions,impressionClickThroughRate"
WINDOW_DAYS = 7
REPORTING_BUFFER_DAYS = 3


class AnalyticsUnavailable(RuntimeError):
    """Raised with a human reason; the caller records it and moves on."""


AUTH_FAILURE_MARKERS = ("invalid_grant", "expired or revoked", "invalid_credentials",
                        "invalid_token", "unauthorized_client", "401")


def is_auth_failure(exc: Exception) -> bool:
    """True when the credential itself is dead (retrying every video is pointless)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in AUTH_FAILURE_MARKERS)


def analytics_status(*, token_path: Path | None = None) -> dict:
    path = Path(token_path or TOKEN_PATH)
    if not path.is_file():
        return {"available": False,
                "reason": f"no analytics token at {path} (run "
                          "python -m cs2archive.upload.auth_youtube_analytics)",
                "token_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"unreadable token: {exc}",
                "token_path": str(path)}
    scopes = payload.get("scopes") or []
    if payload.get("scope"):
        scopes = list(scopes) + str(payload["scope"]).split()
    missing = [s for s in REQUIRED_SCOPES if s not in scopes]
    if missing:
        return {"available": False,
                "reason": f"token is missing scope(s): {missing}", "token_path": str(path)}
    if not (payload.get("refresh_token") or payload.get("token")):
        return {"available": False, "reason": "token has no credential material",
                "token_path": str(path)}
    return {"available": True, "reason": None, "token_path": str(path)}


def _service(token_path: Path | None = None, *, token_factory=None):
    path = Path(token_path or TOKEN_PATH)
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except Exception as exc:  # noqa: BLE001 - optional dependency
        raise AnalyticsUnavailable(f"google api client unavailable: {exc}") from exc
    if token_factory is not None:
        credentials = token_factory(path)
    else:
        credentials = Credentials.from_authorized_user_file(str(path))
    if not credentials.valid and not getattr(credentials, "refresh_token", None):
        raise AnalyticsUnavailable("analytics token has no refresh token")
    return build("youtubeAnalytics", "v2", credentials=credentials,
                 cache_discovery=False)


def analysis_window(published_at: str, now: datetime | None = None,
                    *, tz_name: str = "America/Los_Angeles") -> tuple[str, str] | None:
    """First full local day after publication, for WINDOW_DAYS days."""
    from zoneinfo import ZoneInfo

    if not published_at:
        return None
    published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    now = now or utcnow()
    zone = ZoneInfo(tz_name)
    start = published.astimezone(zone).date() + timedelta(days=1)
    end = start + timedelta(days=WINDOW_DAYS - 1)
    if end > now.astimezone(zone).date() - timedelta(days=REPORTING_BUFFER_DAYS):
        return None  # too new to grade: the window is not a complete set of days
    return start.isoformat(), end.isoformat()


def report_values(payload: dict) -> dict:
    """An empty report means *unavailable*, not zero activity."""
    rows = payload.get("rows") or []
    if not rows:
        return {}
    headers = [column["name"] for column in payload.get("columnHeaders") or []]
    if len(rows) != 1:
        return {name: None for name in headers}
    return dict(zip(headers, rows[0]))


def _query(service, *, video_id: str, start: str, end: str) -> dict:
    request = service.reports().query(
        ids="channel==MINE", startDate=start, endDate=end, metrics=ALL_METRICS,
        dimensions="video", filters=f"video=={video_id}")
    return request.execute(num_retries=0)


def ingest_analytics(store, *, videos: list[dict], run_id: str, token_path: Path | None = None,
                     now: datetime | None = None, service=None, log=print,
                     raw_root: Path | None = None) -> dict:
    """Fetch and store analytics for our own Shorts. Never mixes families."""
    store.ensure_run(run_id, "youtube", mode="analytics")
    status = analytics_status(token_path=token_path)
    stats = {"run_id": run_id, "family": METRIC_FAMILY,
             "available": status["available"], "reason": status["reason"],
             "candidates": 0, "written": 0, "skipped_window": 0,
             "empty_reports": 0, "errors": []}
    if not status["available"]:
        store.log_error(run_id=run_id, platform="youtube",
                        stage="analytics", code="analytics_unavailable",
                        message=str(status["reason"]), terminal=False,
                        request_note="youtube_analytics")
        return stats
    if service is None:
        try:
            service = _service(token_path)
        except AnalyticsUnavailable as exc:
            stats["available"] = False
            stats["reason"] = str(exc)
            store.log_error(run_id=run_id, platform="youtube", stage="analytics",
                            code="analytics_unavailable", message=str(exc),
                            terminal=False, request_note="youtube_analytics")
            return stats
    now = now or utcnow()
    for video in videos:
        video_id = str(video.get("post_id") or video.get("video_id") or "")
        published = video.get("published_at")
        if not video_id:
            continue
        window = analysis_window(published, now)
        if window is None:
            stats["skipped_window"] += 1
            continue
        stats["candidates"] += 1
        start, end = window
        try:
            payload = _query(service, video_id=video_id, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - one video must not kill the run
            message = f"{video_id}: {type(exc).__name__}: {exc}"
            stats["errors"].append(message)
            if is_auth_failure(exc):
                # The credential is dead: record it once and stop hammering.
                reason = (f"analytics credential rejected ({type(exc).__name__}); "
                          "re-run python -m cs2archive.upload.auth_youtube_analytics")
                stats["available"] = False
                stats["reason"] = reason
                store.log_error(run_id=run_id, platform="youtube", stage="analytics",
                                code="analytics_auth_failed", message=reason,
                                terminal=True, request_note="youtube_analytics")
                break
            store.log_error(run_id=run_id, platform="youtube", stage="analytics",
                            code="analytics_query_failed", message=message,
                            request_note=f"video={video_id}")
            continue
        values = report_values(payload)
        if not values:
            stats["empty_reports"] += 1
        fetched_at = iso(now)
        raw_id = None
        if raw_root is not None:
            ref = store.write_raw(platform="youtube_analytics", run_id=run_id,
                                  name=f"analytics_{video_id}", payload=payload,
                                  endpoint="youtubeAnalytics/v2/reports",
                                  http_status=200, fetched_at=fetched_at,
                                  params_note=f"video={video_id}&window={start}..{end}",
                                  raw_root=raw_root)
            raw_id = ref.raw_id
        metrics = {
            "impressions": parse_int(values.get("impressions")),
            "views": parse_int(values.get("views")),
            "ctr": _as_float(values.get("impressionClickThroughRate")),
            "average_view_duration": _as_float(values.get("averageViewDuration")),
            "average_view_percentage": _as_float(values.get("averageViewPercentage")),
            "estimated_minutes_watched": _as_float(values.get("estimatedMinutesWatched")),
            "subscribers_gained": parse_int(values.get("subscribersGained")),
        }
        unavailable = [name for name, value in metrics.items() if value is None]
        written = store.add_analytics_snapshot(
            platform="youtube", post_id=video_id, window_start=start, window_end=end,
            metrics=metrics, metric_family=METRIC_FAMILY, fetched_at=fetched_at,
            run_id=run_id, raw_id=raw_id, unavailable=unavailable)
        if written:
            stats["written"] += 1
    return stats


def _as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def analytics_identity(platform: str, post_id: str, window_start: str,
                       window_end: str) -> str:
    return sha256_text(f"{platform}|{post_id}|{METRIC_FAMILY}|{window_start}|{window_end}")
