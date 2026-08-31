"""Add missing YouTube-relevant / 100T pros to player_accounts.json.

Identity comes from FACEIT match rooms linked in POV-market video
descriptions (same roster match as backfill_faceit_ids). Nickname search is
only a fallback, and only when CS2 ELO is high and the nick matches.
"""
from __future__ import annotations

import argparse
import asyncio
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

from backfill_faceit_ids import (  # noqa: E402
    MATCHES_PER_PLAYER,
    extract_match_ids,
    roster_from_match,
    youtube_match_ids_for,
)

# canonical nick -> FACEIT/YouTube aliases
PROS: dict[str, tuple[str, ...]] = {
    "device": ("device", "dev1ce", "devvezfg"),
    "rain": ("rain",),
    "poiii": ("poiii", "poiii-"),
    "Ag1l": ("ag1l", "Ag1l"),
    "sirah": ("sirah",),
    "jks": ("jks",),
    "ScreaM": ("scream", "ScreaM"),
    "Magnojez": ("magnojez", "MAGNOJEZ", "Magnojezzz"),
    "magisk": ("magisk", "Mag1sk-"),
    "SunPayus": ("sunpayus", "SUNPAYUS"),
    "JACKZ": ("jackz", "JACKZ"),
    "xKacpersky": ("xkacpersky", "xKacpersky"),
    "GeT_RiGhT": ("get_right", "GeT_RiGhT"),
    "MaiL09": ("mail09", "MAIL09", "MaiL09"),
    "r0se": ("r0se",),
    "JamYoung": ("jamyoung", "JamYoung"),
    "zweih": ("zweih", "ZWEIH", "zweih1221"),
    "xiELO": ("xielo", "xiELO"),
    "nocries": ("nocries", "NOCRIES"),
    "broky": ("broky",),
    "cadiaN": ("cadian", "cadiaN"),
    "blameF": ("blamef", "blameF"),
    "dupreeh": ("dupreeh",),
    "stavn": ("stavn",),
    "k0nfig": ("k0nfig",),
    "sjuush": ("sjuush",),
    "chopper": ("chopper",),
    "degster": ("degster",),
    "siuhy": ("siuhy",),
}

# Nickname-search fallback must match these countries (FACEIT ISO).
NICK_COUNTRY = {
    "device": {"DK"},
    "rain": {"NO"},
    "poiii": {"SE", "DK"},
    "Ag1l": {"PT"},
    "sirah": {"DK"},
    "jks": {"AU"},
    "ScreaM": {"BE", "MA"},
    "magisk": {"DK"},
    "broky": {"LV"},
    "cadiaN": {"DK"},
    "blameF": {"DK"},
    "dupreeh": {"DK"},
    "stavn": {"DK"},
    "k0nfig": {"DK"},
    "sjuush": {"DK"},
    "chopper": {"RU"},
    "degster": {"RU"},
    "siuhy": {"PL"},
    "JamYoung": {"CN"},
}

HLTV = {
    "device": ("7592", "https://www.hltv.org/player/7592/device"),
    "rain": ("8183", "https://www.hltv.org/player/8183/rain"),
    "magisk": ("9032", "https://www.hltv.org/player/9032/magisk"),
    "ScreaM": ("7398", "https://www.hltv.org/player/7398/scream"),
    "jks": ("8488", "https://www.hltv.org/player/8488/jks"),
    "dupreeh": ("7412", "https://www.hltv.org/player/7412/dupreeh"),
    "broky": ("18053", "https://www.hltv.org/player/18053/broky"),
    "cadiaN": ("10187", "https://www.hltv.org/player/10187/cadian"),
    "blameF": ("15165", "https://www.hltv.org/player/15165/blamef"),
    "stavn": ("13666", "https://www.hltv.org/player/13666/stavn"),
    "k0nfig": ("9031", "https://www.hltv.org/player/9031/k0nfig"),
    "GeT_RiGhT": ("7148", "https://www.hltv.org/player/7148/get-right"),
    "SunPayus": ("18892", "https://www.hltv.org/player/18892/sunpayus"),
    "chopper": ("11271", "https://www.hltv.org/player/11271/chopper"),
    "degster": ("18987", "https://www.hltv.org/player/18987/degster"),
    "siuhy": ("16847", "https://www.hltv.org/player/16847/siuhy"),
    "sjuush": ("18835", "https://www.hltv.org/player/18835/sjuush"),
    "JACKZ": ("11891", "https://www.hltv.org/player/11891/jackz"),
    "Magnojez": ("21667", "https://www.hltv.org/player/21667/magnojez"),
}

MIN_ELO = 2400


def _new_record(canon: str, steam_id: str, faceit_id: str, faceit_nick: str, source: str) -> dict:
    now = datetime.now()
    hid, hurl = HLTV.get(canon, ("", ""))
    return {
        "nickname": canon,
        "faceit_url": f"https://www.faceit.com/en/players/{faceit_nick}" if faceit_nick else "",
        "faceit_nickname": faceit_nick,
        "faceit_id": faceit_id,
        "faceit_id_source": source,
        "steam_url": f"https://steamcommunity.com/profiles/{steam_id}" if steam_id else "",
        "steam_id": steam_id,
        "hltv_player_id": hid,
        "hltv_player_url": hurl,
        "created_at": now,
        "updated_at": now,
    }


def _pick_roster(roster: list[dict], aliases: tuple[str, ...]) -> dict | None:
    wanted = {a.casefold().rstrip("-") for a in aliases}
    exact = [
        p for p in roster
        if p["nickname"].casefold().rstrip("-") in wanted
        or p["nickname"].casefold() in {a.casefold() for a in aliases}
    ]
    uniq = {p["player_id"]: p for p in exact}
    if len(uniq) == 1:
        return next(iter(uniq.values()))
    prefixed: dict[str, dict] = {}
    for player in roster:
        nick = player["nickname"].casefold().rstrip("-")
        for alias in wanted:
            if len(alias) >= 4 and (
                nick.startswith(alias) or (alias.startswith(nick) and len(nick) >= 4)
            ):
                prefixed[player["player_id"]] = player
                break
    if len(prefixed) == 1:
        return next(iter(prefixed.values()))
    return None


def _elo(player: dict) -> int:
    try:
        return int((player.get("games") or {}).get("cs2", {}).get("faceit_elo") or 0)
    except (TypeError, ValueError):
        return 0


def _country(player: dict) -> str:
    return str(player.get("country") or "").strip().upper()


def _nick_ok(player: dict, aliases: tuple[str, ...]) -> bool:
    nick = str(player.get("nickname") or "").casefold()
    return nick in {a.casefold() for a in aliases}


async def _player_by_nick(client: FACEITClient, nick: str) -> dict | None:
    try:
        data = await client._request("GET", "/players", params={"nickname": nick, "game": "cs2"})
    except Exception:
        return None
    return data if data and data.get("player_id") else None


async def add_pros(*, dry_run: bool) -> None:
    records = _load_accounts()
    have = {str(r.get("nickname") or "").casefold() for r in records}
    missing = {canon: aliases for canon, aliases in PROS.items() if canon.casefold() not in have}
    print(f"already saved: {len(PROS) - len(missing)} / {len(PROS)}")
    print(f"to add: {', '.join(sorted(missing))}")
    if not missing:
        return

    youtube_nicks = set()
    for aliases in missing.values():
        youtube_nicks.update(aliases)
    ids_by_alias = youtube_match_ids_for(youtube_nicks)

    client = FACEITClient()
    added: list[str] = []
    leftover = dict(missing)
    match_cache: dict[str, dict | None] = {}
    try:
        for canon, aliases in list(leftover.items()):
            hit = None
            source_match = ""
            for alias in aliases:
                for match_id in ids_by_alias.get(alias, [])[:MATCHES_PER_PLAYER]:
                    if match_id not in match_cache:
                        match_cache[match_id] = await client.get_match(match_id)
                    payload = match_cache[match_id]
                    if not payload:
                        continue
                    hit = _pick_roster(roster_from_match(payload), aliases)
                    if hit:
                        source_match = match_id
                        break
                if hit:
                    break
            if not hit:
                continue
            player = None
            try:
                player = await client._request("GET", f"/players/{hit['player_id']}")
            except Exception:
                player = None
            steam_id = str(hit.get("steam_id") or (player or {}).get("steam_id_64") or "")
            allowed = NICK_COUNTRY.get(canon)
            country = _country(player or {})
            if allowed and country and country not in allowed:
                print(f"  [SKIP] {canon} YT hit country={country} not in {sorted(allowed)}")
                continue
            faceit_nick = str(hit.get("nickname") or (player or {}).get("nickname") or aliases[0])
            rec = _new_record(canon, steam_id, hit["player_id"], faceit_nick, "youtube_match")
            records.append(rec)
            added.append(canon)
            leftover.pop(canon, None)
            print(
                f"  [YT] {canon} steam={steam_id} faceit={hit['player_id']} "
                f"nick={faceit_nick} elo={_elo(player or {})} via {source_match}"
            )

        for canon, aliases in list(leftover.items()):
            player = None
            for alias in aliases:
                player = await _player_by_nick(client, alias)
                if player and _nick_ok(player, aliases):
                    break
                player = None
            if not player:
                print(f"  [MISS] {canon} no FACEIT nick hit")
                continue
            elo = _elo(player)
            country = _country(player)
            allowed = NICK_COUNTRY.get(canon)
            if allowed and country not in allowed:
                print(f"  [SKIP] {canon} nick hit country={country} elo={elo}")
                continue
            if elo < MIN_ELO:
                print(f"  [SKIP] {canon} elo={elo} < {MIN_ELO} country={country}")
                continue
            steam_id = str(player.get("steam_id_64") or "")
            rec = _new_record(
                canon,
                steam_id,
                str(player["player_id"]),
                str(player.get("nickname") or aliases[0]),
                "nickname_elo",
            )
            records.append(rec)
            added.append(canon)
            leftover.pop(canon, None)
            print(
                f"  [NICK] {canon} steam={steam_id} faceit={player['player_id']} "
                f"elo={elo} country={country}"
            )
    finally:
        await client.close()

    if not dry_run:
        _save_accounts(records)
        print(f"\nwrote {len(added)} account(s)")
    else:
        print(f"\n[dry-run] would write {len(added)} account(s)")
    if leftover:
        print("still missing: " + ", ".join(sorted(leftover)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(add_pros(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
