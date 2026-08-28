"""Find a FACEIT match by a recognised player's scoreline and co-player."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from faceit.faceit_names import known_pro_faceit_ids  # noqa: E402
from scrapers.faceit import FACEITClient  # noqa: E402


async def find_match(
    player: str,
    kills: int,
    deaths: int,
    with_player: str | None,
    count: int,
    tolerance: int,
) -> list[dict]:
    ids = known_pro_faceit_ids()
    player_id = next(
        (faceit_id for faceit_id, nick in ids.items() if nick.casefold() == player.casefold()),
        None,
    )
    if not player_id:
        raise ValueError(f"No verified FACEIT ID for {player}")
    with_player_id = next(
        (
            faceit_id
            for faceit_id, nick in ids.items()
            if with_player and nick.casefold() == with_player.casefold()
        ),
        None,
    )

    client = FACEITClient()
    found = []
    try:
        matches = await client.get_player_matches(player_id, limit=count)
        for match in matches:
            stats = await client.get_match_stats(match.match_id)
            if not stats:
                continue
            line = next(
                (
                    value
                    for value in stats["players"].values()
                    if value.get("player_id") == player_id
                ),
                None,
            )
            if not line:
                continue
            if (
                abs(int(line["kills"]) - kills) > tolerance
                or abs(int(line["deaths"]) - deaths) > tolerance
            ):
                continue
            player_names = list(stats["players"])
            if with_player and not any(
                line.get("player_id") == with_player_id
                for line in stats["players"].values()
            ):
                continue
            found.append(
                {
                    "match_id": match.match_id,
                    "date": match.date.isoformat() if match.date else None,
                    "map": stats["map"],
                    "score": stats["score"],
                    "player_line": line,
                    "players": player_names,
                    "url": match.url,
                }
            )
    finally:
        await client.close()
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("player")
    parser.add_argument("kills", type=int)
    parser.add_argument("deaths", type=int)
    parser.add_argument("--with-player")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--tolerance", type=int, default=0)
    args = parser.parse_args()
    matches = asyncio.run(
        find_match(
            args.player,
            args.kills,
            args.deaths,
            args.with_player,
            args.count,
            args.tolerance,
        )
    )
    print(json.dumps(matches, indent=2))
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
