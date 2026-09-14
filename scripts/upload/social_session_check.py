"""Preflight checks for TikTok Studio sessions before upload.

Instagram Reels no longer use the CloakBrowser Business Suite flow; they
publish through Graph (`meta_graph.py`). TikTok still needs a live browser
session.

Usage:
    from social_session_check import check_tiktok
    ok, msg = check_tiktok(profile_dir)
"""

from __future__ import annotations

from pathlib import Path


def check_tiktok(profile_dir: Path, *, timeout_ms: int = 15000) -> tuple[bool, str]:
    """Return (logged_in, message). Logged_in True means TikTok Studio upload reachable."""
    try:
        from cloakbrowser import launch_persistent_context
        from tiktok_studio_navigator import UPLOAD_URL
    except Exception as e:
        return False, f"tiktok import failed: {e}"

    ctx = None
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        ctx = launch_persistent_context(str(profile_dir), headless=True, humanize=False)
        page = ctx.new_page()
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)
        cur = page.url
        body = page.evaluate("() => document.body.innerText || ''")[:2000].lower()
        # TikTok redirects to /login if not authenticated, or shows "log in" button with no upload form
        if "login" in cur.lower() or ("log in" in body and "select video" not in body and "upload" not in body):
            return False, f"TikTok Studio login required (url={cur}). Run: python scripts/upload_tiktok_browser.py login --isolated-profile (or tiktok login helper)"
        if "tiktokstudio" in cur and ("select video" in body or "upload" in body):
            return True, f"tiktok session OK (url={cur})"
        # fallback: if we stayed on tiktokstudio without login redirect, consider OK
        if "tiktok.com" in cur:
            return True, f"tiktok session OK (url={cur})"
        return False, f"tiktok check ambiguous (url={cur})"
    except Exception as e:
        return False, f"tiktok check failed: {type(e).__name__}: {e}"
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
