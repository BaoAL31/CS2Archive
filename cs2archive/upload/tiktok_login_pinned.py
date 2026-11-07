"""One-time TikTok login into the pinned-fingerprint Cloak profile.

CloakBrowser randomizes the fingerprint seed per launch; TikTok binds the
session to the device fingerprint, so the login only survives when the
upload flow launches with the SAME seed. This script pins it (must match
``_pin_tiktok_fingerprint`` in ``upload_youtube_shorts.py``).

    python cs2archive/upload/tiktok_login_pinned.py [--seed 73491] [--timeout 600]

Log in inside the window that opens; the session persists for the Studio
upload flow. Close the window when done.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "CS2UtilArchive" / "scripts"))

TIKTOK_LOGIN_URL = "https://www.tiktok.com/login"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=73491,
                    help="Pinned CloakBrowser fingerprint seed (must match "
                         "upload_youtube_shorts._pin_tiktok_fingerprint)")
    ap.add_argument("--profile-dir", default=r"D:\Projects\CS2Archive\.cloak-tiktok-profile")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    from cloakbrowser import launch_persistent_context

    profile = Path(args.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    print(f"[login] profile {profile}")
    print(f"[login] pinned fingerprint seed {args.seed}")
    ctx = launch_persistent_context(
        str(profile),
        headless=False,
        humanize=False,
        args=[f"--fingerprint={args.seed}"],
    )
    page = ctx.new_page()
    page.goto(TIKTOK_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    print(f"[login] waiting up to {args.timeout}s — log in inside the window, "
          "then close it (or wait for the timeout)")
    try:
        page.wait_for_url("**/foryou**", timeout=args.timeout * 1000)
        print("[login] landed on the feed — session should be saved")
    except Exception:
        print("[login] timeout or navigation — closing")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())