"""Fill missing Recognised-Pro FACEIT GUIDs in player_accounts.json.

Steam64 lookup first (not spoofable). Leftovers are resolved from FACEIT
match room links in the POV-market YouTube scrape, matching the video's
primary_player to a roster nick.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from player_accounts import _load_accounts, _save_accounts  # noqa: E402
from scrapers.faceit import FACEITClient  # noqa: E402

ROOM_RE = re.compile(
    r"faceit\.com/(?:[\w-]+/)?(?:cs2/)?room/"
    r"(1-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
    re.I,
)
YOUTUBE_CSVS = (
    ROOT / "exports" / "pov_market" / "expanded" / "videos_20260827T233414Z.csv",
    ROOT / "exports" / "pov_market" / "own" / "videos_20260827T234736Z.csv",
    ROOT / "exports" / "pov_market" / "recent_2d" / "videos_20260829T041719Z.csv",
)
MATCHES_PER_PLAYER = 5


def _has_faceit_id(rec: dict) -> bool:
    fid = rec.get("faceit_id")
    return isinstance(fid, str) and fid.strip() and fid.strip() != "-1"


def extract_match_ids(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match_id in ROOM_RE.findall(text or ""):
            key = match_id.lower()
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def roster_from_match(payload: dict) -> list[dict]:
    out: list[dict] = []
    teams = (payload or {}).get("teams") or {}
    for faction in teams.values():
        if not isinstance(faction, dict):
            continue
        for player in faction.get("roster") or []:
            if not isinstance(player, dict) or not player.get("player_id"):
                continue
            out.append({
                "player_id": str(player["player_id"]),
                "nickname": str(player.get("nickname") or ""),
                "steam_id": str(player.get("game_player_id") or ""),
            })
    return out


def apply_faceit(rec: dict, player_id: str, nickname: str, source: str) -> None:
    rec["faceit_id"] = player_id
    if nickname and not rec.get("faceit_nickname"):
        rec["faceit_nickname"] = nickname
    if nickname and not rec.get("faceit_url"):
        rec["faceit_url"] = f"https://www.faceit.com/en/players/{nickname}"
    rec["faceit_id_source"] = source
    rec["updated_at"] = datetime.now()


def youtube_match_ids_for(nicks: set[str]) -> dict[str, list[str]]:
    """primary_player (casefold) -> FACEIT match ids from video descriptions."""
    wanted = {n.casefold(): n for n in nicks}
    found: dict[str, list[str]] = {n: [] for n in nicks}
    seen_per: dict[str, set[str]] = {n: set() for n in nicks}
    for path in YOUTUBE_CSVS:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                primary = (row.get("primary_player") or "").strip().casefold()
                if primary not in wanted:
                    continue
                nick = wanted[primary]
                if len(found[nick]) >= MATCHES_PER_PLAYER:
                    continue
                for match_id in extract_match_ids(row.get("description") or ""):
                    if match_id in seen_per[nick]:
                        continue
                    seen_per[nick].add(match_id)
                    found[nick].append(match_id)
                    if len(found[nick]) >= MATCHES_PER_PLAYER:
                        break
    return found


def _pick_roster_player(roster: list[dict], nick: str, steam_id: str) -> dict | None:
    nick_cf = nick.casefold()
    steam_hits = [p for p in roster if steam_id and p["steam_id"] == steam_id]
    if len(steam_hits) == 1:
        return steam_hits[0]
    nick_hits = [p for p in roster if p["nickname"].casefold() == nick_cf]
    if len(nick_hits) == 1:
        return nick_hits[0]
    return None


async def backfill(*, dry_run: bool) -> None:
    records = _load_accounts()
    missing = [
        rec for rec in records
        if rec.get("nickname") and rec.get("steam_id") and not _has_faceit_id(rec)
    ]
    print(f"missing faceit_id: {len(missing)} / {len(records)}")
    if not missing:
        return

    client = FACEITClient()
    filled: list[tuple[str, str]] = []
    leftover: list[dict] = []
    try:
        for rec in missing:
            nick = rec["nickname"]
            steam_id = str(rec["steam_id"])
            player = await client.get_player_by_steam_id(steam_id)
            if player and player.get("player_id"):
                apply_faceit(
                    rec,
                    str(player["player_id"]),
                    str(player.get("nickname") or ""),
                    "steam",
                )
                filled.append((nick, "steam"))
                print(f"  [STEAM] {nick} -> {player['player_id']} ({player.get('nickname')})")
            else:
                leftover.append(rec)
                print(f"  [MISS]  {nick} steam {steam_id} not linked")

        if leftover:
            print(f"\nYouTube match fallback for {len(leftover)} leftover(s)")
            ids_by_nick = youtube_match_ids_for({r["nickname"] for r in leftover})
            match_cache: dict[str, dict | None] = {}
            still: list[dict] = []
            for rec in leftover:
                nick = rec["nickname"]
                steam_id = str(rec["steam_id"])
                hit = None
                for match_id in ids_by_nick.get(nick, []):
                    if match_id not in match_cache:
                        match_cache[match_id] = await client.get_match(match_id)
                    payload = match_cache[match_id]
                    if not payload:
                        continue
                    hit = _pick_roster_player(roster_from_match(payload), nick, steam_id)
                    if hit:
                        apply_faceit(rec, hit["player_id"], hit["nickname"], "youtube_match")
                        filled.append((nick, "youtube_match"))
                        print(
                            f"  [YT]    {nick} -> {hit['player_id']} "
                            f"({hit['nickname']}) via {match_id}"
                        )
                        break
                if not hit:
                    still.append(rec)
                    print(f"  [MISS]  {nick} no YouTube match roster hit")
            leftover = still
    finally:
        await client.close()

    if not dry_run:
        _save_accounts(records)
        print(f"\nwrote {len(filled)} faceit_id(s) to .data/player_accounts.json")
    else:
        print(f"\n[dry-run] would write {len(filled)} faceit_id(s)")
    if leftover:
        print("still missing: " + ", ".join(r["nickname"] for r in leftover))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
