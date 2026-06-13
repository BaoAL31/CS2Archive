"""YouTube publish scheduling helpers (timezone-aware wall-clock -> UTC)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PUBLISH_TZ = "Australia/Sydney"

_PUBLISH_AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_publish_at(publish_at: str, timezone: str = DEFAULT_PUBLISH_TZ) -> str:
    """Parse wall-clock datetime in ``timezone`` and return YouTube RFC3339 UTC."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc

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


def resolve_publish_schedule(
    *,
    publish_at: str | None,
    timezone: str | None,
    meta: dict,
    privacy: str,
) -> tuple[str, str | None, str, str | None]:
    """
    Resolve scheduling from CLI + meta.

    Returns (privacy, publish_at_utc, timezone_used, publish_at_local).
    Scheduled uploads force privacy to private (YouTube requirement).
    """
    local = publish_at or meta.get("publish_at")
    tz = timezone or meta.get("publish_timezone") or DEFAULT_PUBLISH_TZ
    if not local:
        return privacy, None, tz, None

    publish_at_utc = parse_publish_at(local, tz)
    if privacy != "private":
        privacy = "private"
    return privacy, publish_at_utc, tz, local
