"""V1 engine-event evidence for action timelines and the Shorts foundation.

No quality labels or tactical inference. Missing values remain null, and
64-bit identity fields remain strings for JSON/Parquet/JavaScript consumers.
"""
from __future__ import annotations

import math

EVENT_DATA_VERSION = 1
KILL_FLAGS = ("headshot", "assistedflash", "attackerblind", "noscope", "thrusmoke", "attackerinair")
KILL_DETAILS = (
    "penetrated", "assister_steamid", "assister_name", "distance",
    "dmg_health", "dmg_armor", "hitgroup", "weapon", "weapon_itemid",
    "weapon_fauxitemid", "weapon_originalowner_xuid", "dominated", "revenge", "wipe",
)
IDENTITY_FIELDS = {"weapon_itemid", "weapon_fauxitemid", "weapon_originalowner_xuid"}


def event_value(value, *, field: str = ""):
    import pandas as pd

    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): event_value(v, field=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [event_value(v) for v in value]
    if hasattr(value, "tolist") and not hasattr(value, "item"):
        return event_value(value.tolist(), field=field)
    if hasattr(value, "item"):
        try:
            value = value.item()
        except ValueError:
            return event_value(value.tolist(), field=field)
    if pd.isna(value):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if field.endswith(("steamid", "steam_id", "xuid")) or field in IDENTITY_FIELDS:
        if isinstance(value, float):
            # A float SteamID may already have lost bits; do not invent identity.
            return None
        return str(value)
    if isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported event value for {field}: {type(value).__name__}")


def _flag(value) -> bool | None:
    value = event_value(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false", "0", "1"}:
        return value.lower() in {"true", "1"}
    return None


def kill_event_details(row) -> dict:
    result = {key: _flag(row.get(key)) for key in KILL_FLAGS}
    result.update({key: event_value(row.get(key), field=key) for key in KILL_DETAILS})
    return result


def raw_event_table(frame) -> dict:
    """Retain every parsed column and row, including non-pro/world/team deaths."""
    return {
        "columns": [str(c) for c in frame.columns],
        "rows": [{str(k): event_value(v, field=str(k)) for k, v in row.items()}
                 for row in frame.to_dict(orient="records")],
    }
