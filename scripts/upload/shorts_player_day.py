"""One Short per player per calendar day.

When auto-assigning CS2UtilArchive slots (12:00 / 18:00), occupy *both* slots
on any date this POV already has a Short scheduled so a second donk/m0NESY
clip cannot land the same day.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHORTS_META_NAME = "upload_meta_shorts.json"


def pov_nick_from_meta_path(meta_path: Path) -> str | None:
    tl = meta_path.parent / "short_timeline.json"
    if not tl.exists():
        return None
    try:
        data = json.loads(tl.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    shorts = data.get("shorts") or []
    if not shorts:
        return None
    nick = (shorts[0].get("pov_nick") or "").strip()
    return nick or None


def meta_publish_date_local(meta: dict, tz: str) -> str | None:
    utc = meta.get("publish_at_utc")
    if utc:
        dt = datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    raw = (meta.get("publish_at") or "").strip()
    if raw and raw.lower() != "auto" and len(raw) >= 10:
        return raw[:10]
    return None


def player_blocked_slots(
    root: Path,
    nick: str,
    tz: str,
    slot_times: list[str],
    *,
    exclude_meta: Path | None = None,
) -> set[tuple[str, str]]:
    """Return (date, HH:MM) pairs that must stay empty for this player."""
    if not nick:
        return set()
    want = nick.casefold()
    blocked: set[tuple[str, str]] = set()
    exclude = exclude_meta.resolve() if exclude_meta else None
    for meta_path in root.rglob(SHORTS_META_NAME):
        if exclude and meta_path.resolve() == exclude:
            continue
        other = pov_nick_from_meta_path(meta_path)
        if not other or other.casefold() != want:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        date = meta_publish_date_local(meta, tz)
        if not date:
            continue
        for slot in slot_times:
            blocked.add((date, slot))
    return blocked
