"""
CS2Archive — Backlog Creator

Downloads match demos and creates backlog entries organized by priority.
Usage: python scripts/create_backlog.py <hltv_url>
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

BACKLOG_DIR = PROJECT_ROOT / "backlog"


def get_player_status(nickname: str, demo_path: Path | None) -> tuple[str, str]:
    """Check Steam ID and avatar availability for a player."""
    from player_accounts import _load_accounts, PlayerAccount

    lower = nickname.lower()
    found = None
    for rec in _load_accounts():
        if rec["nickname"].lower() == lower:
            found = PlayerAccount(**rec)
            break

    steam_id = found.steam_id if found else None
    if not steam_id and found and found.steam_url:
        steam_id = found.steam_url
    steam_status = "✅ available" if steam_id else "❌ missing"

    avatar_status = "❌ missing"
    for ext in (".png", ".jpg", ".jpeg"):
        avatar_path = PROJECT_ROOT / "demos" / "avatars" / f"{nickname.lower()}{ext}"
        if avatar_path.exists():
            avatar_status = "✅ available"
            break

    return steam_status, avatar_status


def get_priority(rating: float) -> str:
    if rating >= 1.5:
        return "high"
    elif rating >= 1.0:
        return "medium"
    return "low"


def _resolve_demo_for_map(match_slug: str, map_name: str) -> Path | None:
    """
    Best-effort resolve the actual .dem filename for a map.

    HLTV acquisitions typically produce files like:
      demos/hltv/<match_slug>/<match_slug>-m2-ancient.dem
    Older code assumed <Map>.dem which is often wrong.
    """
    demo_dir = PROJECT_ROOT / "demos" / "hltv" / match_slug
    if not demo_dir.exists():
        return None

    map_slug = map_name.strip().lower()
    cands = list(demo_dir.glob(f"*{map_slug}*.dem"))
    if not cands:
        return None

    def score(p: Path) -> tuple[int, int, str]:
        name = p.name.lower()
        # Prefer explicit "-mN-<map>" patterns if present
        has_m = 0 if f"-{map_slug}.dem" in name or f"-{map_slug}-" in name or f"-m" in name else 1
        return (has_m, len(name), name)

    return sorted(cands, key=score)[0]


def create_backlog_entry(
    match_url: str,
    match_slug: str,
    player: str,
    map_name: str,
    rating: float,
    kd: str,
    team: str,
    demo_path: Path | None,
) -> None:
    player_clean = player.strip()
    steam_status, avatar_status = get_player_status(player_clean, demo_path)
    priority = get_priority(rating)

    # Extract steam_id for the pipeline command
    from player_accounts import _load_accounts, PlayerAccount
    found = None
    for rec in _load_accounts():
        if rec["nickname"].lower() == player_clean.lower():
            found = PlayerAccount(**rec)
            break
    steam_id = (found.steam_id or found.steam_url or "") if found else ""

    slug = f"{player_clean.lower()}-{map_name.lower()}-{match_slug}"
    backlog_file = BACKLOG_DIR / priority / f"{slug}.md"
    backlog_file.parent.mkdir(parents=True, exist_ok=True)

    steam_flag = f"--steam-id {steam_id}" if steam_id else "--steam-id <STEAM_ID>"

    demo_for_map = _resolve_demo_for_map(match_slug, map_name)
    demo_rel = (
        str(demo_for_map.relative_to(PROJECT_ROOT)).replace("\\", "/")
        if demo_for_map
        else f"demos/hltv/{match_slug}/<DEMO_FOR_MAP>.dem"
    )

    content = f"""# Handoff: {player_clean} — {map_name} ({rating})

Run the pipeline for this POV.

## Player & Map

| Field | Value |
|---|---|
| Player | {player_clean} |
| Map | {map_name} |
| Rating | {rating} |
| Steam ID | {steam_status} |
| Avatar | {avatar_status} |
| Team | {team} |
| Priority | {priority} |

## Match

| Field | Value |
|---|---|
| HLTV URL | {match_url} |
| Demo path | `{demo_rel}` |

## Pipeline Command

```powershell
python scripts/pipeline.py {player_clean} {map_name} "{match_url}" `
  {steam_flag} --demo "{demo_rel}"
```

"""
    backlog_file.write_text(content, encoding="utf-8")
    print(f"  Created: backlog/{priority}/{slug}.md")


async def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <hltv_url>")
        sys.exit(1)

    match_url = sys.argv[1]

    from scrapers.hltv_acquire import acquire_match, match_slug_from_url, match_demo_dir
    from models import DownloadStatus, DownloadResult, MatchInfo, DemoSource
    from scrapers.ratings import get_match_ratings

    # Check if demos already exist
    slug = match_slug_from_url(match_url)
    demo_folder = match_demo_dir(slug)
    existing_demos = list(demo_folder.glob("*.dem"))

    if existing_demos:
        print(f"[SKIP] Demos already exist in {demo_folder}")
        result = DownloadResult(
            match=MatchInfo(
                match_id="",
                source=DemoSource.HLTV,
                url=match_url,
                team1="",
                team2="",
            ),
            status=DownloadStatus.SKIPPED,
            demo_path=existing_demos[0],
        )
    else:
        print("[DL] Acquiring match...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, acquire_match, match_url)

        if result.status.name == "FAILED":
            print(f"[ERR] Acquisition failed: {result.error}")
            sys.exit(1)

        print(f"[OK] Acquired to {result.demo_path.parent}")

    print("[SCRAPE] Scraping ratings...")
    ratings = await get_match_ratings(match_url)

    if not ratings or not ratings.get("tables"):
        print("[ERR] No ratings found")
        sys.exit(1)

    for table in ratings["tables"]:
        if table["map"] == "Series Overall":
            continue
        map_name = table["map"]
        for player in table["players"]:
            rating = float(player["rating"])
            create_backlog_entry(
                match_url=match_url,
                match_slug=slug,
                player=player["nickname"],
                map_name=map_name,
                rating=rating,
                kd=player["kd"],
                team=table["team"],
                demo_path=result.demo_path,
            )

    print("[OK] Backlog created")


if __name__ == "__main__":
    asyncio.run(main())
