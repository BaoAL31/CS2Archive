"""
CS2Archive — Backlog Creator

Downloads demos, scrapes ratings + tournament, resolves steam IDs,
fetches avatars, and writes backlog entries as JSON.

Usage: python scripts/pov/create_backlog.py <hltv_url>
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

# Redirect HuggingFace cache to D: drive (must be set before importing huggingface_hub)
os.environ.setdefault("HF_HOME", "D:/.cache/huggingface")
os.environ.setdefault("HF_HUB_CACHE", "D:/.cache/huggingface/hub")

BACKLOG_DIR = PROJECT_ROOT / "backlog"

REQUIRED_META_FIELDS = [
    "player",
    "map",
    "hltv_url",
    "steam_id",
    "demo_path",
    "ratings_path",
    "tournament",
]


def get_priority(rating: float) -> str:
    if rating >= 1.5:
        return "high"
    elif rating >= 1.0:
        return "medium"
    return "low"


def _resolve_demo_for_map(match_slug: str, map_name: str) -> Path:
    demo_dir = PROJECT_ROOT / "demos" / "hltv" / match_slug
    if not demo_dir.exists():
        raise FileNotFoundError(
            f"Demo directory not found: {demo_dir}\n"
            f"  Download the match first before creating backlog entries."
        )

    map_slug = map_name.strip().lower()
    cands = list(demo_dir.glob(f"*{map_slug}*.dem"))
    if not cands:
        raise FileNotFoundError(
            f"No .dem file found for map '{map_name}' in {demo_dir}\n"
            f"  Expected a file matching '*{map_slug}*.dem'."
        )

    def score(p: Path) -> tuple[int, int, str]:
        name = p.name.lower()
        has_m = 0 if f"-{map_slug}.dem" in name or f"-{map_slug}-" in name or f"-m" in name else 1
        return (has_m, len(name), name)

    return sorted(cands, key=score)[0]



def _resolve_steam_id(nickname: str) -> str:
    from player_accounts import _load_accounts, PlayerAccount

    lower = nickname.lower()
    for rec in _load_accounts():
        if rec["nickname"].lower() == lower:
            acct = PlayerAccount(**rec)
            return acct.steam_id or acct.steam_url or ""
    return ""


def _existing_avatar_path(nickname: str) -> str:
    name = nickname.strip().lower()
    # Nested layout: {nick}/hltv or {nick}/faceit, then legacy flat
    for source in ("hltv", "faceit"):
        folder = PROJECT_ROOT / "demos" / "avatars" / name / source
        if folder.is_dir():
            for ext in (".png", ".jpg", ".jpeg"):
                p = folder / f"{name}{ext}"
                if p.exists():
                    return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
    for ext in (".png", ".jpg", ".jpeg"):
        p = PROJECT_ROOT / "demos" / "avatars" / f"{name}{ext}"
        if p.exists():
            return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return ""


async def create_backlog_entry(
    match_url: str,
    match_slug: str,
    player: str,
    map_name: str,
    rating: float,
    kd: str,
    team: str,
    demo_path: Path,
    ratings_path: Path,
    tournament: str,
    *,
    avatar_rel: str = "",
) -> None:
    player_clean = player.strip()
    priority = get_priority(rating)
    steam_id = _resolve_steam_id(player_clean)

    demo_for_map = _resolve_demo_for_map(match_slug, map_name)
    demo_rel = str(demo_for_map.relative_to(PROJECT_ROOT)).replace("\\", "/")

    if not avatar_rel:
        avatar_rel = _existing_avatar_path(player_clean)
        if not avatar_rel:
            try:
                avatar_rel = await _fetch_avatar(player_clean, match_url, ratings_path)
            except Exception as e:
                print(f"  [WARN] Avatar fetch failed for {player_clean}: {e}")

    slug = f"{player_clean.lower()}-{map_name.lower()}-{match_slug}"
    backlog_dir = BACKLOG_DIR / match_slug / priority
    backlog_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = backlog_dir / f"{slug}.json"

    hf_root = re.sub(r"[^\w]", "_", tournament.lower()).strip("_")

    from scrapers.prosettings import backlog_video_fields

    meta = {
        "player": player_clean,
        "map": map_name,
        "hltv_url": match_url,
        "steam_id": steam_id,
        "demo_path": demo_rel,
        "ratings_path": str(ratings_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "avatar_path": avatar_rel,
        "tournament": tournament,
        "hf_root": hf_root,
        "rating": rating,
        "kd": kd,
        "team": team,
        "priority": priority,
        **backlog_video_fields(player_clean),
        "pipeline_cmd": f'$env:PYTHONPATH=.; & C:/Users/jembo/anaconda3/envs/cs2archive/python.exe scripts/pov/pipeline.py --backlog backlog/{match_slug}/{priority}/{slug}.json',
    }

    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  Created: backlog/{match_slug}/{priority}/{slug}.json")


def _save_ratings_json(match_url: str, ratings: dict) -> Path:
    m = re.search(r"/matches/\d+/([^/?#]+)", match_url)
    if not m:
        raise ValueError(f"Cannot extract slug from URL: {match_url}")

    slug = m.group(1)
    analysis_dir = PROJECT_ROOT / "demos" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    path = analysis_dir / f"{slug}_ratings.json"
    path.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
    return path


HF_REPO = "cs2povarchive/cs2-demos"


def _download_match_from_hf(hf_root: str, slug: str, demo_folder: Path) -> list[Path]:
    """Download all .dem files for a match from the HuggingFace dataset.

    Remote layout: {hf_root}/{slug}/{filename}.dem  (slug already includes match id).
    """
    from huggingface_hub import HfApi, hf_hub_download

    if not hf_root:
        raise ValueError("hf_root (tournament) is empty; cannot locate demo on HuggingFace.")

    api = HfApi()
    prefix = f"{hf_root}/{slug}"
    print(f"[HF] Listing {HF_REPO}:{prefix} ...")
    items = list(api.list_repo_tree(HF_REPO, repo_type="dataset", path_in_repo=hf_root, recursive=True))
    dems = [i for i in items if i.path.startswith(prefix + "/") and i.path.endswith(".dem")]
    if not dems:
        raise FileNotFoundError(
            f"No .dem files found on HuggingFace under '{prefix}/'. "
            f"Cannot create backlog without demos."
        )

    demo_folder.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for it in dems:
        fname = Path(it.path).name
        local = demo_folder / fname
        if local.exists():
            out.append(local)
            continue
        print(f"  [HF] Downloading {it.path}")
        cached = hf_hub_download(repo_id=HF_REPO, filename=it.path, repo_type="dataset")
        shutil.copy2(cached, local)
        out.append(local)
    return out


async def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <hltv_url>")
        sys.exit(1)

    match_url = sys.argv[1]

    from scrapers.hltv_acquire import match_slug_from_url, match_demo_dir
    from scrapers.ratings import get_match_ratings

    slug = match_slug_from_url(match_url)
    demo_folder = match_demo_dir(slug)
    demo_folder.mkdir(parents=True, exist_ok=True)
    existing_demos = list(demo_folder.glob("*.dem"))

    print("[SCRAPE] Scraping ratings...")
    ratings = await get_match_ratings(match_url)
    if not ratings or not ratings.get("tables"):
        print("[ERR] No ratings found")
        sys.exit(1)
    ratings_path = _save_ratings_json(match_url, ratings)
    print(f"[OK] Ratings saved to {ratings_path}")

    tournament = ratings.get("tournament", "")
    print(f"[OK] Tournament: {tournament}")
    hf_root = re.sub(r"[^\w]", "_", tournament.lower()).strip("_") if tournament else ""
    print(f"[OK] HF root: {hf_root}")

    if existing_demos:
        print(f"[SKIP] Demos already exist in {demo_folder}")
    else:
        print("[HF] Demos not found locally. Downloading from HuggingFace...")
        try:
            demos = _download_match_from_hf(hf_root, slug, demo_folder)
        except Exception as e:
            print(f"[ERR] HuggingFace download failed: {e}")
            sys.exit(1)
        print(f"[OK] Downloaded {len(demos)} demo(s) to {demo_folder}")

    unique_players = sorted({
        p["nickname"].strip()
        for t in ratings["tables"]
        for p in t["players"]
    })
    print(f"[AVATAR] Fetching {len(unique_players)} unique players (CloakBrowser)...")
    avatar_cache: dict[str, str] = {}

    def _fetch_avatars_sync():
        from scrapers.player_images import CloakAvatarFetcher, _fetch_avatar_cloak
        import time as _time

        cache: dict[str, str] = {}
        with CloakAvatarFetcher(headless=False) as fetcher:
            for i, nick in enumerate(unique_players):
                cached = _existing_avatar_path(nick)
                if cached:
                    cache[nick] = cached
                    continue
                try:
                    path = _fetch_avatar_cloak(nick, match_url, str(ratings_path), fetcher=fetcher)
                    abs_path = PROJECT_ROOT.joinpath(path) if not path.is_absolute() else path
                    cache[nick] = str(abs_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
                except Exception as e:
                    print(f"  [WARN] Avatar failed for {nick}: {e}")
                if i < len(unique_players) - 1:
                    _time.sleep(2)
        return cache

    try:
        loop = asyncio.get_event_loop()
        avatar_cache = await loop.run_in_executor(None, _fetch_avatars_sync)
    except Exception as e:
        print(f"  [WARN] Avatar browser unavailable, skipping avatars: {e}")

    for table in ratings["tables"]:
        if table["map"] == "Series Overall":
            continue
        map_name = table["map"]
        for player in table["players"]:
            rating = float(player["rating"])
            nick = player["nickname"].strip()
            await create_backlog_entry(
                match_url=match_url,
                match_slug=slug,
                player=nick,
                map_name=map_name,
                rating=rating,
                kd=player.get("kd", ""),
                team=table["team"],
                demo_path=demo_folder,
                ratings_path=ratings_path,
                tournament=tournament,
                avatar_rel=avatar_cache.get(nick, ""),
            )

    print("[OK] Backlog created")


if __name__ == "__main__":
    asyncio.run(main())
