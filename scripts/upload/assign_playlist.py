"""Playlist name normalization."""

import re


def normalize_playlist_name(name: str | None) -> str | None:
    """Convert tournament name to a playlist-safe name."""
    if not name:
        return None
    # Strip whitespace, collapse spaces, remove special chars
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name
