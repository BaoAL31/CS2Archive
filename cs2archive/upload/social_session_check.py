"""Preflight checks for TikTok / Instagram before upload.

Instagram Reels publish through Graph (`meta_graph.py`). TikTok uses the
Content Posting API (`tiktok_api.py` in the CS2UtilArchive sibling checkout)
— no browser session is needed; login state is the cached API token.

Usage:
    from cs2archive.upload.social_session_check import check_tiktok
    ok, msg = check_tiktok()
"""

from __future__ import annotations

from pathlib import Path


def check_tiktok(profile_dir: Path | None = None, *, timeout_ms: int = 15000) -> tuple[bool, str]:
    """Return (ok, message). OK means the TikTok API token is live.

    ``profile_dir`` is accepted for backward compatibility and ignored —
    the old CloakBrowser Studio session is gone."""
    try:
        from cs2archive.overlay._common import prefer_cs2util_scripts

        prefer_cs2util_scripts()
        from tiktok_api import user_info
    except Exception as e:
        return False, f"tiktok import failed: {e}"

    try:
        payload = user_info()
    except Exception as e:
        return (
            False,
            f"tiktok API check failed: {type(e).__name__}: {e}. "
            "Run: python scripts/tiktok_api.py login",
        )
    name = ((payload.get("data") or {}).get("user") or {}).get("display_name") or "unknown"
    return True, f"tiktok API OK ({name})"
