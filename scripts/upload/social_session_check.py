"""Preflight checks for TikTok / Instagram sessions before upload.

Fail fast with a clear `login required` message instead of hanging 10min
waiting for an upload that will never succeed.

Usage:
    from social_session_check import check_instagram, check_tiktok
    ok, msg = check_instagram(profile_dir)
    ok, msg = check_tiktok(profile_dir)
"""

from __future__ import annotations

from pathlib import Path


def check_instagram(profile_dir: Path, *, timeout_ms: int = 15000) -> tuple[bool, str]:
    """Return (logged_in, message). Logged_in True means Business Suite reachable."""
    try:
        from instagram_business_navigator import (
            DEFAULT_ASSET_ID,
            DEFAULT_BUSINESS_ID,
            build_entry_url,
            launch_browser_context,
            close_browser_context,
        )
    except Exception as e:
        return False, f"instagram import failed: {e}"

    ctx = None
    try:
        ctx = launch_browser_context(profile_dir, headed=False)
        page = ctx.new_page()
        url = build_entry_url(asset_id=DEFAULT_ASSET_ID, business_id=DEFAULT_BUSINESS_ID)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)
        cur = page.url
        if "/business/loginpage" in cur:
            return False, f"Meta Business Suite login required (url={cur}). Run: python scripts/upload_instagram_browser.py --profile-dir {profile_dir} login --isolated-profile --cloak-chrome --timeout 600"
        return True, f"instagram session OK (url={cur})"
    except Exception as e:
        return False, f"instagram check failed: {type(e).__name__}: {e}"
    finally:
        if ctx:
            try:
                close_browser_context(ctx)
            except Exception:
                pass


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
