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
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
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
    """HLTV priority bucket (shared thresholds; HLTV folders use 'medium')."""
    from _backlog_common import rating_bucket
    return rating_bucket(rating, mid_name="medium", unknown="medium")


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



def _resolve_steam_id(nickname: str, demo_steamids: dict[str, str] | None = None) -> str:
    from player_accounts import _load_accounts, PlayerAccount, extract_steam_id

    lower = nickname.lower()
    for rec in _load_accounts():
        if rec["nickname"].lower() == lower:
            acct = PlayerAccount(**rec)
            # Prefer re-resolving from steam_url (canonical when it's a
            # 17-digit numeric ID). acct.steam_id can be stale if the Steam
            # profile URL redirect changed over — using it over the
            # CSDM to the wrong player and silently render enemy POV.
            resolved = extract_steam_id(acct.steam_url)
            if resolved:
                return resolved
            return acct.steam_id or acct.steam_url or ""

    # Fallback: extract from the demo itself
    if demo_steamids:
        for player_name, sid in demo_steamids.items():
            if player_name.lower() == lower:
                print(f"  [STEAM] resolved {nickname} from demo: {sid}")
                return sid

    return ""


def _existing_avatar_path(nickname: str) -> str:
    """Cached avatar lookup — shared implementation in _backlog_common."""
    from _backlog_common import find_avatar
    return find_avatar(nickname)


def _ensure_player_account(nickname: str, steam_id: str) -> None:
    """Add player to player_accounts.json if not already present."""
    from player_accounts import _load_accounts, _save_accounts
    import datetime as _dt

    records = _load_accounts()
    for r in records:
        if r["nickname"].lower() == nickname.lower():
            return

    now = _dt.datetime.now().isoformat()
    records.append({
        "nickname": nickname,
        "steam_id": steam_id,
        "steam_url": f"https://steamcommunity.com/profiles/{steam_id}",
        "created_at": now,
        "updated_at": now,
    })
    _save_accounts(records)
    print(f"  [ACCT] Added {nickname} ({steam_id}) to player_accounts.json")


def _ensure_video_settings(nickname: str) -> None:
    """Sync video settings from prosettings.net onto the player account."""
    from scrapers.prosettings import resolve_video_settings
    from player_accounts import _load_accounts, _save_accounts
    import datetime as _loc_dt

    settings = resolve_video_settings(nickname)
    records = _load_accounts()
    now = _loc_dt.datetime.now()
    for r in records:
        if r["nickname"].lower() == nickname.lower():
            r["resolution"] = settings["resolution"]
            r["aspect_ratio"] = settings["aspect_ratio"]
            r["scaling_mode"] = settings["scaling_mode"]
            r["capture_width"] = settings["width"]
            r["capture_height"] = settings["height"]
            r["video_settings_source"] = settings.get("source", "default")
            for k in ("viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
                      "viewmodel_offset_z", "viewmodel_presetpos", "hud_scaling"):
                if settings.get(k) is not None:
                    r[k] = settings[k]
                elif k not in r:
                    r[k] = None
            r["updated_at"] = str(now)
            _save_accounts(records)
            print(f"  [CFG] Synced video settings for {nickname} (source: {settings.get('source', '?')})")
            return


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
    demo_steamids: dict[str, str] | None = None,
) -> None:
    player_clean = player.strip()
    priority = get_priority(rating)
    steam_id = _resolve_steam_id(player_clean, demo_steamids)

    demo_for_map = _resolve_demo_for_map(match_slug, map_name)
    demo_rel = str(demo_for_map.relative_to(PROJECT_ROOT)).replace("\\", "/")

    if not avatar_rel:
        avatar_rel = _existing_avatar_path(player_clean)

    missing: list[str] = []
    if not steam_id:
        missing.append("steam_id")
    if not avatar_rel:
        missing.append("avatar_path")
    if missing:
        print(f"[ERR] Missing required fields for {player_clean} on {map_name}: {missing}")
        sys.exit(1)

    _ensure_player_account(player_clean, steam_id)
    _ensure_video_settings(player_clean)

    # Team + opponent from the DEMO itself (authoritative — see
    # scripts/shorts/detect_team.py). Falls back to the HLTV ratings table
    # only when demo detection fails, and warns on disagreement.
    demo_team: str | None = None
    demo_opponent: str | None = None
    if steam_id and Path(demo_for_map).exists():
        try:
            from scripts.shorts.detect_team import detect_pov_opponent
            demo_team, demo_opponent = detect_pov_opponent(str(demo_for_map), steam_id)
        except Exception as e:
            print(f"  [WARN] demo team detection failed for {player_clean}: {e}")
    team_final = demo_team or team
    if demo_team and team and demo_team.strip().lower() != team.strip().lower():
        print(f"  [WARN] team mismatch for {player_clean}: demo says "
              f"{demo_team!r}, HLTV ratings say {team!r} (using demo)")

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
        "team": team_final,
        "opponent": demo_opponent or "",
        "team_source": "demo" if demo_team else "hltv_ratings",
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


def _write_avatar_worker(players: list[str], match_url: str, ratings_path: str) -> Path:
    """Write a self-contained Python script that fetches avatars via CloakBrowser
    in its own process (avoids Plapyright-async-inside-asyncio deadlock)."""
    script = PROJECT_ROOT / "tmp" / "_avatar_worker.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_ROOT_ABS = str(PROJECT_ROOT).replace("\\", "/")
    script.write_text(f'''
import json, os, sys, time
from pathlib import Path

PROJECT_ROOT = Path(r"{PROJECT_ROOT_ABS}")
os.chdir(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

players = {json.dumps(players)}
match_url = {json.dumps(match_url)}
ratings_path = {json.dumps(ratings_path)}

from scrapers.player_images import CloakAvatarFetcher, _fetch_avatar_cloak

def _existing(nick):
    name = nick.strip().lower()
    for source in ("hltv", "faceit"):
        folder = PROJECT_ROOT / "demos" / "avatars" / name / source
        if folder.is_dir():
            for ext in (".png", ".jpg", ".jpeg"):
                p = folder / f"{{name}}{{ext}}"
                if p.exists():
                    return str(p.relative_to(PROJECT_ROOT)).replace("\\\\", "/")
    return ""

with CloakAvatarFetcher(headless=False) as fetcher:
    for i, nick in enumerate(players):
        cached = _existing(nick)
        if cached:
            print(f"AVATAR_OK:{{nick}}:{{cached}}")
            continue
        try:
            path = _fetch_avatar_cloak(nick, match_url, str(ratings_path), fetcher=fetcher)
            abs_path = PROJECT_ROOT.joinpath(path) if not path.is_absolute() else path
            rel = str(abs_path.relative_to(PROJECT_ROOT)).replace("\\\\", "/")
            print(f"AVATAR_OK:{{nick}}:{{rel}}")
        except Exception as e:
            print(f"AVATAR_FAIL:{{nick}}:{{e}}")
        if i < len(players) - 1:
            time.sleep(2)
'''.strip(), encoding="utf-8")
    return script


async def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <hltv_url | demos/faceit/<demo>.dem>")
        sys.exit(1)

    arg = sys.argv[1]

    # Unified dispatcher: a .dem file routes to the FACEIT flow, an URL to
    # the HLTV match flow.
    from _backlog_common import detect_demo_source
    source = detect_demo_source(arg)
    if source == "faceit":
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from scripts.faceit.create_faceit_match_backlog import run as run_faceit
        demo = Path(arg).resolve()
        print(f"[DETECT] FACEIT demo -> create_faceit_match_backlog.run({demo.name})")
        extra = sys.argv[2:]
        import argparse as _ap
        ns = _ap.ArgumentParser()
        ns.add_argument("--map", default=""); ns.add_argument("--tournament", default="")
        ns.add_argument("--match-id", default=""); ns.add_argument("--no-elo", action="store_true")
        ns.add_argument("--no-shorts", action="store_true")
        opts, _unknown = ns.parse_known_args(extra)
        run_faceit(demo, map_override=opts.map, tournament=opts.tournament,
                   match_id_arg=opts.match_id, no_elo=opts.no_elo,
                   no_shorts=opts.no_shorts)
        return
    if source == "hltv":
        print("[ERR] HLTV .dem path given but the HLTV flow needs the match URL "
              "(ratings scrape). Pass the hltv.org match URL instead.")
        sys.exit(1)

    match_url = arg

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
        if hf_root:
            print("[HF] Demos not found locally. Downloading from HuggingFace...")
            try:
                demos = _download_match_from_hf(hf_root, slug, demo_folder)
                print(f"[OK] Downloaded {len(demos)} demo(s) to {demo_folder}")
            except Exception as e:
                print(f"  [WARN] HuggingFace download failed: {e}")
                print("  [FALLBACK] Trying CloakBrowser download...")
                from scrapers.hltv_acquire import acquire_match
                result = await asyncio.to_thread(acquire_match, match_url, force=False, headless=True)
                if result.status.value == "failed":
                    print(f"[ERR] CloakBrowser download also failed: {result.error}")
                    sys.exit(1)
                print(f"[OK] Downloaded via CloakBrowser to {demo_folder}")
        else:
            print("[DL] No HF root — downloading via CloakBrowser...")
            from scrapers.hltv_acquire import acquire_match
            result = await asyncio.to_thread(acquire_match, match_url, force=False, headless=True)
            if result.status.value == "failed":
                print(f"[ERR] CloakBrowser download failed: {result.error}")
                sys.exit(1)
            print(f"[OK] Downloaded via CloakBrowser to {demo_folder}")

        existing_demos = list(demo_folder.glob("*.dem"))
        if not existing_demos:
            print("[ERR] No .dem files available after download")
            sys.exit(1)

    unique_players = sorted({
        p["nickname"].strip()
        for t in ratings["tables"]
        for p in t["players"]
    })
    print(f"[STEAM] Extracting Steam IDs from demos...")
    demo_steamids = {}
    for dem in existing_demos:
        try:
            from scripts.pov.extract_steamids import extract_steamids
            demo_steamids.update(extract_steamids(str(dem)))
        except Exception as e:
            print(f"  [WARN] Failed to extract steam IDs from {dem.name}: {e}")
        # Keep the persistent team roster in sync with every extracted demo.
        try:
            from scripts.shorts.team_roster import update_team_roster
            update_team_roster(str(dem), demo_tag=dem.name)
        except Exception as e:
            print(f"  [WARN] Failed to update team roster from {dem.name}: {e}")
    print(f"  [OK] Resolved {len(demo_steamids)} players from demos")

    print(f"[AVATAR] Fetching {len(unique_players)} unique players (CloakBrowser)...")
    avatar_cache: dict[str, str] = {}
    avatar_script = _write_avatar_worker(unique_players, match_url, str(ratings_path))
    try:
        import subprocess as _subprocess
        print(f"  [AVATAR] Launching isolated subprocess for {len(unique_players)} players...")
        _result = _subprocess.run(
            [sys.executable, str(avatar_script)],
            capture_output=True, text=True, timeout=600, cwd=str(PROJECT_ROOT),
        )
        print(_result.stdout)
        if _result.stderr:
            print(_result.stderr, file=sys.stderr)
        for line in _result.stdout.splitlines():
            if line.startswith("AVATAR_OK:"):
                _, nick, path = line.split(":", 2)
                avatar_cache[nick] = path
            elif line.startswith("AVATAR_FAIL:"):
                _, nick, msg = line.split(":", 2)
                print(f"  [WARN] Avatar failed for {nick}: {msg}")
        if _result.returncode != 0:
            print(f"  [WARN] Avatar worker exited {_result.returncode}")
    except Exception as e:
        print(f"  [WARN] Avatar worker failed: {e}")

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
                demo_steamids=demo_steamids,
            )

    print("[OK] Backlog created")


if __name__ == "__main__":
    asyncio.run(main())
