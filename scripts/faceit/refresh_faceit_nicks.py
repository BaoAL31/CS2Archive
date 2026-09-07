"""Refresh stored faceit_nickname for all Recognised Pros from live API.

Compares each account's stored faceit_nickname against the current FACEIT
nickname (queried by stable faceit_id) and updates mismatches (renames).
Backs up player_accounts.json first. Nick is display-only after the
player_id patch, but stale nicks confuse manual CLI use + logs.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from scrapers.faceit import FACEITClient  # noqa: E402

ACCOUNTS = PROJECT_ROOT / ".data" / "player_accounts.json"


async def main() -> None:
    data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    players = data if isinstance(data, list) else data.get("players", [])
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ACCOUNTS.with_name(f"player_accounts.bak-{ts}.json")
    shutil.copy2(ACCOUNTS, backup)
    print(f"[BACKUP] {backup.name}")

    client = FACEITClient()
    changed, errors = 0, 0
    try:
        for p in players:
            fid = str(p.get("faceit_id") or "").strip()
            if not fid or fid == "-1":
                continue
            try:
                live = await client._request("GET", f"/players/{fid}")
            except Exception as e:
                print(f"  [WARN] {p.get('nickname')}: {e}")
                errors += 1
                continue
            live_nick = str(live.get("nickname") or "").strip()
            stored = str(p.get("faceit_nickname") or "").strip()
            if live_nick and live_nick != stored:
                print(f"  [RENAME] {p.get('nickname')}: "
                      f"'{stored}' -> '{live_nick}'")
                p["faceit_nickname"] = live_nick
                p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                changed += 1
            await asyncio.sleep(0.3)
    finally:
        await client.close()
    ACCOUNTS.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"[DONE] {changed} renamed, {errors} errors, "
          f"{len(players)} accounts checked")


if __name__ == "__main__":
    asyncio.run(main())
