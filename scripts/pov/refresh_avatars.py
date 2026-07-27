"""Refresh all HLTV avatars older than a given age."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scrapers.player_images import CloakAvatarFetcher, _fetch_avatar_cloak

AVATAR_DIR = PROJECT_ROOT / "demos" / "avatars"
DEFAULT_CUTOFF_DAYS = 30


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CUTOFF_DAYS
    cutoff = datetime.now() - timedelta(days=days)

    stale: list[str] = []
    for p in sorted(AVATAR_DIR.rglob("*.png")):
        if p.parent.name in ("hltv", "faceit"):
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            if mtime < cutoff:
                stale.append(p.parent.parent.name)

    stale = sorted(set(stale))
    if not stale:
        print(f"No avatars older than {days} days")
        return

    print(f"Refreshing {len(stale)} avatars older than {days} days...")
    ok = fail = 0

    with CloakAvatarFetcher(headless=True) as fetcher:
        for i, nick in enumerate(stale):
            try:
                _fetch_avatar_cloak(
                    nick, "", "", fetcher=fetcher, force=True,
                )
                ok += 1
                print(f"  [{i+1}/{len(stale)}] {nick} OK")
            except Exception as e:
                fail += 1
                print(f"  [{i+1}/{len(stale)}] {nick} FAIL: {e}")
            if i < len(stale) - 1:
                time.sleep(1)

    if fail > 0:
        print(f"\nDone: {ok} ok, {fail} failed")
        raise SystemExit(1)
    print(f"\nDone: {ok} ok, 0 failed")


if __name__ == "__main__":
    main()