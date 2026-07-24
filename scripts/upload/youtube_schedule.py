"""YouTube publish scheduling helpers (timezone-aware wall-clock -> UTC)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _detect_local_tz() -> str:
    """Detect local IANA timezone name on Windows."""
    _WINDOWS_TO_IANA = {
        "SE Asia Standard Time": "Asia/Bangkok",
        "Singapore Standard Time": "Asia/Singapore",
        "Taipei Standard Time": "Asia/Taipei",
        "China Standard Time": "Asia/Shanghai",
        "Tokyo Standard Time": "Asia/Tokyo",
        "Korea Standard Time": "Asia/Seoul",
        "India Standard Time": "Asia/Kolkata",
        "AUS Eastern Standard Time": "Australia/Sydney",
        "AUS Central Standard Time": "Australia/Darwin",
        "Cen. Australia Standard Time": "Australia/Adelaide",
        "W. Australia Standard Time": "Australia/Perth",
        "Pacific Standard Time": "America/Los_Angeles",
        "Eastern Standard Time": "America/New_York",
        "Central Standard Time": "America/Chicago",
        "Mountain Standard Time": "America/Denver",
        "GMT Standard Time": "Europe/London",
        "W. Europe Standard Time": "Europe/Berlin",
        "Central Europe Standard Time": "Europe/Prague",
        "E. Europe Standard Time": "Europe/Bucharest",
        "UTC": "UTC",
    }
    try:
        r = subprocess.run(["tzutil", "/g"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            name = r.stdout.strip()
            if name in _WINDOWS_TO_IANA:
                return _WINDOWS_TO_IANA[name]
    except Exception:
        pass
    # Fallback: UTC offset
    import time as _time
    offset = -_time.timezone
    if _time.daylight and _time.localtime().tm_isdst:
        offset = -_time.altzone
    hours = offset // 3600
    if hours == 0:
        return "UTC"
    return f"Etc/GMT{'+' if hours <= 0 else '-'}{abs(hours)}"


DEFAULT_PUBLISH_TZ = _detect_local_tz()
AUTO_PUBLISH_TIMES = ["10:00", "16:30"]
AUTO_PUBLISH_MODE = "auto"

_PUBLISH_AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc


def _parse_publish_time(publish_time: str) -> tuple[int, int]:
    parts = publish_time.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Unrecognized publish time {publish_time!r} (expected HH:MM)")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Unrecognized publish time {publish_time!r} (expected HH:MM)") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Unrecognized publish time {publish_time!r} (expected HH:MM)")
    return hour, minute


def _parse_publish_times(publish_times: list[str] | str) -> list[tuple[int, int]]:
    if isinstance(publish_times, str):
        publish_times = [publish_times]
    return [_parse_publish_time(t) for t in publish_times]


def _parse_publish_time(time_str: str) -> tuple[int, int]:
    """Parse a time string like 'HH:MM' or 'HH:MM:SS' and return (hour, minute)."""
    parts = time_str.split(':')
    if len(parts) < 2:
        raise ValueError(f"Invalid time format: {time_str!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    # ignore seconds if present
    return hour, minute


def next_available_publish_date(
    start_date: date,
    occupied_dates: Iterable[str],
    timezone: str = DEFAULT_PUBLISH_TZ,
) -> date:
    """Return first unoccupied date from ``start_date`` onward."""
    _zone(timezone)
    occupied = {d.split("T")[0][:10] for d in occupied_dates if d}
    candidate = start_date
    while candidate.isoformat() in occupied:
        candidate += timedelta(days=1)
    return candidate


def parse_publish_at(publish_at: str, timezone: str = DEFAULT_PUBLISH_TZ) -> str:
    """Parse wall-clock datetime in ``timezone`` and return YouTube RFC3339 UTC."""
    tz = _zone(timezone)

    text = publish_at.strip()
    parsed: datetime | None = None
    for fmt in _PUBLISH_AT_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(
            f"Unrecognized publish-at datetime {publish_at!r} "
            f"(expected e.g. '2026-06-12 17:00')"
        )

    utc = parsed.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def resolve_auto_publish_schedule(
    *,
    timezone: str | None,
    start_date: date | None = None,
    occupied_dates: Iterable[str] | None = None,
    publish_times: list[str] | str = "16:30",
    now: datetime | None = None,
) -> tuple[str, str, str, str]:
    """Resolve next future daily publish slot at ``publish_times`` in ``timezone``.

    Returns (privacy, publish_at_utc, timezone_used, publish_at_local).
    Scheduled uploads force privacy to private (YouTube requirement).
    """
    tz = timezone or _detect_local_tz()
    tzinfo = ZoneInfo(tz)
    times = _parse_publish_times(publish_times)
    if now is None:
        now = datetime.now(tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)
    else:
        now = now.astimezone(tzinfo)

    candidate_date = start_date or now.date()

    for hour, minute in times:
        slot_time = datetime.combine(candidate_date, time(hour, minute, tzinfo=tzinfo))
        if now < slot_time:
            break
    else:
        candidate_date += timedelta(days=1)
        hour, minute = times[0]

    first_free = next_available_publish_date(
        candidate_date,
        occupied_dates or [],
        tz,
    )
    local = f"{first_free.isoformat()} {hour:02d}:{minute:02d}"
    return "private", parse_publish_at(local, tz), tz, local


def resolve_publish_schedule(
    *,
    publish_at: str | None,
    timezone: str | None,
    meta: dict,
    privacy: str,
    start_date: date | None = None,
    occupied_dates: Iterable[str] | None = None,
    publish_times: list[str] | None = None,
    now: datetime | None = None,
) -> tuple[str, str | None, str, str | None]:
    """
    Resolve scheduling from CLI + meta.

    Returns (privacy, publish_at_utc, timezone_used, publish_at_local).
    Scheduled uploads force privacy to private (YouTube requirement).
    ``publish_at="auto"`` uses the next future date at 10:00 or 16:30 in ``timezone``.
    """
    local = publish_at or meta.get("publish_at")
    tz = timezone or meta.get("publish_timezone") or DEFAULT_PUBLISH_TZ
    if not local:
        return privacy, None, tz, None

    if local == AUTO_PUBLISH_MODE:
        # Auto mode: always use local timezone detection
        return resolve_auto_publish_schedule(
            start_date=start_date,
            occupied_dates=occupied_dates,
            publish_times=publish_times or AUTO_PUBLISH_TIMES,
            now=now,
            timezone=tz,
        )

    publish_at_utc = parse_publish_at(local, tz)
    if privacy != "private":
        privacy = "private"
    return privacy, publish_at_utc, tz, local
