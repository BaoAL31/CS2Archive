"""Platform adapter registry."""

from __future__ import annotations

from cs2archive.shorts.social.platforms import (
    allstar, exports, instagram, reddit, tiktok, youtube,
)

ADAPTERS = {
    "youtube": youtube,
    "instagram": instagram,
    "tiktok": tiktok,
    "reddit": reddit,
    "allstar": allstar,
}

KNOWN_PLATFORMS = tuple(ADAPTERS)


def adapter_for(platform: str, config):
    if (config.mode or "") == "authorized_export" or (config.extra or {}).get("export_paths"):
        return exports
    if platform not in ADAPTERS:
        raise KeyError(f"no adapter for platform {platform!r}")
    return ADAPTERS[platform]


__all__ = ["ADAPTERS", "KNOWN_PLATFORMS", "adapter_for"]
