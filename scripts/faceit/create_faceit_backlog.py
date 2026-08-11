"""Create a backlog entry for an already-downloaded FACEIT demo.

The demo must live under demos/faceit/ (or any path). The resulting backlog
entry carries is_faceit=true so the pipeline uses the FACEIT title/thumbnail
path automatically.

Per-match ELO is fetched from the FACEIT Data API at creation time and stored
on the card (``elo`` = POV player's ELO, ``opp_avg_elo`` = average ELO of the
five players on the opposing team). These feed the individual FACEIT
title/thumbnail — "NiKo 3521 ELO vs 3105 ELO" — and the pipeline reads them
straight from the card, so no API calls happen during rendering. Pass
``--no-elo`` to skip (title/thumbnail then omit the ELO line).

Usage:
    python scripts/faceit/create_faceit_backlog.py <demo_path> --player <nick> --map <map>
                                  [--steam-id <id>] [--tournament <name>]
                                  [--match-id <id>] [--priority high] [--no-elo]

``--match-id`` stores the FACEIT match id on the card (drives the room link in
the YouTube description); when omitted it is auto-resolved from
``.data/download_history.json`` for demos downloaded via ``faceit match``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

BACKLOG_DIR = PROJECT_ROOT / "backlog"
MAP_DISPLAY = {
    "de_ancient": "Ancient", "de_mirage": "Mirage", "de_inferno": "Inferno",
    "de_nuke": "Nuke", "de_anubis": "Anubis", "de_overpass": "Overpass",
    "de_vertigo": "Vertigo", "de_dust2": "Dust2", "de_train": "Train",
}
MAP_KEYWORDS = {
    "cache": "Cache", "dust2": "Dust2", "mirage": "Mirage",
    "inferno": "Inferno", "nuke": "Nuke", "ancient": "Ancient",
    "anubis": "Anubis", "overpass": "Overpass", "vertigo": "Vertigo",
    "train": "Train",
}


def _map_from_demo(demo_path: Path) -> str:
    name = demo_path.name.lower()
    m = re.search(r"(de_[a-z0-9]+)", name)
    if m:
        return MAP_DISPLAY.get(m.group(1), m.group(1).replace("de_", "").capitalize())
    for kw, display in MAP_KEYWORDS.items():
        if kw in name:
            return display
    return "Unknown"


def _demo_players(demo_path: Path) -> list[dict]:
    """Return list of {name, steamid, team_number} from the demo header."""
    import demoparser2 as dp
    parser = dp.DemoParser(str(demo_path))
    info = parser.parse_player_info()
    out = []
    for _, row in info.iterrows():
        sid = str(row.get("steamid", "")).strip()
        if not sid or sid.lower() == "nan":
            continue  # bots / empty slots
        out.append({
            "name": str(row.get("name", "")).strip(),
            "steamid": sid,
            "team_number": int(row.get("team_number", 0)),
        })
    return out


def _kd_from_demo(demo_path: Path, steam_id: str) -> tuple[int, int] | None:
    """(kills, deaths) for one player from the demo's player_death events.

    Matches csdm's convention: suicides (attacker == victim) and the knife
    round (demo round 1 before the real first round starts) are excluded.
    None when unavailable or the player had zero deaths.
    """
    try:
        import demoparser2 as dp
        parser = dp.DemoParser(str(demo_path))
        deaths = parser.parse_event("player_death")
        round_starts = parser.parse_event("round_start")
    except Exception as e:
        print(f"  [WARN] kd computation failed: {e}")
        return None
    if deaths is None or len(deaths) == 0:
        return None
    # Last round_start with round == 1 is the real first round; earlier
    # round-1 events are the knife round (not counted in match stats).
    first_real_tick = 0
    if round_starts is not None and len(round_starts):
        r1 = round_starts[round_starts["round"] == 1]
        if len(r1):
            first_real_tick = int(r1["tick"].max())
    att = deaths["attacker_steamid"].astype(str)
    vic = deaths["user_steamid"].astype(str)
    core = deaths[(deaths["tick"] >= first_real_tick) & (att != vic)]
    kills = int((core["attacker_steamid"].astype(str) == steam_id).sum())
    deaths_n = int((core["user_steamid"].astype(str) == steam_id).sum())
    if deaths_n == 0:
        return None
    return kills, deaths_n


def _resolve_steam_id(demo_path: Path, nick: str) -> str:
    """Resolve a player's steam64 from the demo, by in-game name first, then
    by Recognised-Pro canonical nickname (.data/player_accounts.json)."""
    try:
        import demoparser2 as dp
        info = dp.DemoParser(str(demo_path)).parse_player_info()
        demo_steam_ids = [str(r.get("steamid", "")).strip() for _, r in info.iterrows()]
        for _, row in info.iterrows():
            if str(row.get("name", "")).strip().lower() == nick.lower():
                return str(row.get("steamid", "")).strip()
    except Exception as e:
        print(f"  [WARN] steam id resolve failed: {e}")
        return ""
    # canonical nick fallback: account nickname -> steam id present in demo
    try:
        data = json.loads((PROJECT_ROOT / ".data" / "player_accounts.json").read_text(encoding="utf-8"))
        players = data if isinstance(data, list) else data.get("players", [])
        for acct in players:
            if str(acct.get("nickname") or "").strip().lower() == nick.lower():
                sid = str(acct.get("steam_id") or "").strip()
                if sid in demo_steam_ids:
                    return sid
    except Exception:
        pass
    return ""


async def _match_elo(demo_path: Path, pov_steam_id: str) -> dict:
    """Fetch current ELO for every demo player; return
    {elo: <pov elo>, opp_avg_elo: <opponent-team average>} (empty dict when
    the POV player's ELO can't be resolved)."""
    from scrapers.faceit import FACEITClient
    players = _demo_players(demo_path)
    client = FACEITClient()
    elos: dict[str, int] = {}
    try:
        for p in players:
            if not p["steamid"]:
                continue
            elo = await client.get_elo_by_steam_id(p["steamid"])
            if elo is not None:
                elos[p["steamid"]] = elo
            await asyncio.sleep(0.3)  # be polite to the free API tier
    finally:
        await client.close()

    if pov_steam_id not in elos:
        return {}
    pov_elo = elos[pov_steam_id]
    pov_team = next((p["team_number"] for p in players
                     if p["steamid"] == pov_steam_id), 0)
    if pov_team not in (2, 3):
        # team numbers unreliable — treat everyone else as opponents
        opp = [elos[p["steamid"]] for p in players
               if p["steamid"] != pov_steam_id and p["steamid"] in elos]
    else:
        opp = [elos[p["steamid"]] for p in players
               if p["team_number"] != pov_team and p["steamid"] in elos]
    if not opp:
        return {"elo": pov_elo}
    return {"elo": pov_elo, "opp_avg_elo": round(sum(opp) / len(opp))}


def _match_id_from_history(demo: Path) -> str:
    """Resolve the FACEIT match id from .data/download_history.json by demo
    path (populated by `main.py faceit match <id>` downloads)."""
    hist = PROJECT_ROOT / ".data" / "download_history.json"
    try:
        records = json.loads(hist.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(records, list):
        return ""
    try:
        demo_rel = demo.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        demo_rel = str(demo).replace("\\", "/")
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        if str(rec.get("source") or "").lower() != "faceit":
            continue
        if str(rec.get("demo_path") or "").replace("\\", "/") == demo_rel:
            return str(rec.get("match_id") or "")
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path")
    ap.add_argument("--player", required=True)
    ap.add_argument("--map", default="")
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--tournament", default="")
    ap.add_argument("--match-id", default="",
                    help="FACEIT match id (auto-resolved from download history if omitted)")
    ap.add_argument("--priority", choices=["high", "mid", "low"], default="high")
    ap.add_argument("--no-elo", action="store_true",
                    help="Skip FACEIT ELO fetch (title/thumbnail omit ELO line)")
    args = ap.parse_args()

    demo = Path(args.demo_path).resolve()
    if not demo.exists():
        print(f"[ERR] demo not found: {demo}")
        sys.exit(1)

    steam_id = args.steam_id or _resolve_steam_id(demo, args.player)
    if not steam_id:
        print(f"[ERR] player '{args.player}' not found in demo")
        sys.exit(1)

    map_name = args.map or _map_from_demo(demo)

    match_id = args.match_id.strip() or _match_id_from_history(demo)

    meta: dict = {
        "player": args.player,
        "map": map_name,
        "steam_id": steam_id,
        "demo_path": str(demo.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "tournament": args.tournament,
        "priority": args.priority,
        "is_faceit": True,
    }
    if match_id:
        meta["faceit_match_id"] = match_id

    kd_stats = _kd_from_demo(demo, steam_id)
    if kd_stats is not None:
        kills, deaths = kd_stats
        meta["kills"] = kills
        meta["deaths"] = deaths

    if not args.no_elo:
        print("[FACEIT] Fetching match ELO ...")
        elo = asyncio.run(_match_elo(demo, steam_id))
        if elo:
            meta.update(elo)
            opp_txt = f"vs {elo['opp_avg_elo']} ELO avg" if "opp_avg_elo" in elo else "(opponents n/a)"
            print(f"  [OK] POV ELO: {elo['elo']} {opp_txt}")
        else:
            print("  [WARN] ELO unavailable — title/thumbnail will omit the ELO line")

    slug = re.sub(r"[^a-z0-9]+", "-", f"{args.player}-{map_name}".lower()).strip("-")
    demo_key = re.sub(r"[^a-z0-9]+", "-", demo.stem.lower()).strip("-")
    if demo_key:
        slug = f"{slug}-{demo_key}"
    backlog_dir = BACKLOG_DIR / "faceit" / args.priority
    backlog_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = backlog_dir / f"{slug}.json"

    meta["pipeline_cmd"] = (
        f'$env:PYTHONPATH=.; & C:/Users/jembo/anaconda3/envs/cs2archive/python.exe '
        f'scripts/pov/pipeline.py --backlog {backlog_file.relative_to(PROJECT_ROOT).as_posix()} --overlay-only'
    )
    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[OK] Created: {backlog_file}")


if __name__ == "__main__":
    main()
