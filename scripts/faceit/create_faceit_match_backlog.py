"""Full-match FACEIT backlog creator.

Given an already-downloaded FACEIT demo, runs `csdm json` to compute
per-player stats, keeps **Recognised Pros** only (`.data/player_accounts.json`
by steam64 — see `docs/adr/0002-single-player-accounts-store.md`), and writes
one backlog card per pro.

No ELO notes: cards now also carry the POV player's FACEIT ELO and the
opposing team's average ELO (``elo`` / ``opp_avg_elo``, fetched via the
FACEIT API) so titles/thumbnails can show "3521 ELO vs 3105 ELO".
Pass ``--no-elo`` to skip the fetch.

This is the "full match POVs" half of the FACEIT flow. The other half is the
individual single-player flow: `scripts/faceit/create_faceit_backlog.py`
(one card per invocation) + the standard `pipeline.py`.

Usage:
    python scripts/faceit/create_faceit_match_backlog.py <demo_path>
                                  [--map <map>] [--tournament <name>]
                                  [--match-id <faceit_match_id>]

Output: backlog/faceit/{date}/{priority}/{player}-{map}-{match_slug}.json
Each card carries the pipeline fields plus: rating, kills, deaths, team,
faceit_match_id, faceit_id, faceit_nickname.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from faceit_names import avatar_path  # noqa: E402
from create_faceit_backlog import _match_elo  # noqa: E402
from _backlog_common import (  # noqa: E402
    load_accounts_by_steam,
    rating_bucket,
    write_card,
    rel_to_project,
)

BACKLOG_DIR = PROJECT_ROOT / "backlog" / "faceit"
CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
TMP_DIR = PROJECT_ROOT / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

MAP_DISPLAY = {
    "de_ancient": "Ancient", "de_mirage": "Mirage", "de_inferno": "Inferno",
    "de_nuke": "Nuke", "de_anubis": "Anubis", "de_overpass": "Overpass",
    "de_vertigo": "Vertigo", "de_dust2": "Dust2", "de_train": "Train",
}
# Workshop / non de_* maps sometimes lack the de_ prefix (e.g. "cache").
MAP_KEYWORDS = {
    "cache": "Cache", "dust2": "Dust2", "mirage": "Mirage",
    "inferno": "Inferno", "nuke": "Nuke", "ancient": "Ancient",
    "anubis": "Anubis", "overpass": "Overpass", "vertigo": "Vertigo",
    "train": "Train",
}


def _map_display(map_name: str) -> str:
    key = (map_name or "").strip().lower()
    if key in MAP_DISPLAY:
        return MAP_DISPLAY[key]
    m = re.search(r"(de_[a-z0-9]+)", key)
    if m:
        return MAP_DISPLAY.get(m.group(1), m.group(1).replace("de_", "").capitalize())
    for kw, display in MAP_KEYWORDS.items():
        if kw in key:
            return display
    return "Unknown"


def _match_slug(demo: Path, match_id: str = "") -> str:
    """Sanitize a FACEIT match id / demo stem into a filesystem-safe slug."""
    raw = match_id.strip() if match_id else demo.stem
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "faceit"


def _load_accounts() -> dict[str, dict]:
    """steam_id_64 -> account record for every Recognised Pro (shared)."""
    return load_accounts_by_steam()


def _elo_sync(demo: Path, steam_id: str) -> dict | None:
    """Run the async ELO fetch whether or not a loop is already running
    (unified dispatcher enters via async main; standalone entry does not)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_match_elo(demo, steam_id))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, _match_elo(demo, steam_id)).result()


def _csdm_json(demo: Path) -> dict:
    """Run `csdm json` on the demo and return the parsed match export.

    Same call as `extract_steamids.py` (incl. the PBDEMS2/challengermode
    fallback). The export carries per-player `hltvRating2`, `killDeathRatio`,
    `teamName` and the authoritative `mapName`.
    """
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmpdir:
        cmd = [CSDM, "json", str(demo), "--output-folder", tmpdir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0 and "unknown demo source" in (result.stderr or "").lower():
            cmd += ["--source", "challengermode"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"csdm json failed: {str(result.stderr)[-500:]}")
        files = list(Path(tmpdir).glob("*.json"))
        if not files:
            raise RuntimeError("csdm json produced no output")
        return json.loads(files[0].read_text(encoding="utf-8"))


def priority_from_rating(rating: float | None) -> str:
    """Priority bucket from the in-match rating (shared thresholds in
    _backlog_common.rating_bucket; FACEIT buckets are high/mid/low and an
    unknown rating queues mid)."""
    return rating_bucket(rating, mid_name="mid", unknown="mid")


def _round2(value) -> float | None:
    if isinstance(value, (int, float)) and value == value:  # skip NaN
        return round(float(value), 2)
    return None


def _write_card(pro: dict, *, demo: Path, map_name: str, match_slug: str,
                tournament: str, elo_fields: dict | None = None,
                match_date: str | None = None) -> Path:
    from scripts.faceit.backlog_paths import faceit_backlog_dir, match_date_for_demo
    priority = priority_from_rating(pro["rating"])
    match_date = match_date_for_demo(demo, match_date)
    player_key = re.sub(r"[^a-z0-9]+", "-", pro["canonical_nick"].lower()).strip("-")
    slug = f"{player_key}-{map_name.lower()}-{match_slug}"
    backlog_dir = faceit_backlog_dir(BACKLOG_DIR.parent, match_date, priority)
    backlog_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = backlog_dir / f"{slug}.json"

    av_path = avatar_path(pro["canonical_nick"])
    demo_rel = rel_to_project(demo)

    meta = {
        "player": pro["canonical_nick"],
        "map": map_name,
        "steam_id": pro["steam_id"],
        "demo_path": demo_rel,
        "tournament": tournament,
        "priority": priority,
        "match_date": match_date,
        "rating": pro["rating"],
        "kills": pro["kills"],
        "deaths": pro["deaths"],
        "team": pro["team"],
        "is_faceit": True,
        "faceit_match_id": pro["faceit_match_id"],
        "faceit_id": pro["faceit_id"],
        "faceit_nickname": pro["faceit_nickname"],
        "avatar_path": str(av_path.relative_to(PROJECT_ROOT)).replace("\\", "/") if av_path else "",
        **({} if not elo_fields else elo_fields),
        "pipeline_cmd": (
            f'$env:PYTHONPATH=.; & C:/Users/jembo/anaconda3/envs/cs2archive/python.exe '
            f'scripts/pov/pipeline.py --backlog backlog/faceit/{match_date}/{priority}/{slug}.json --overlay-only'
        ),
    }
    write_card(meta, backlog_file)
    return backlog_file


def run(demo: Path, *, map_override: str = "", tournament: str = "",
        match_id_arg: str = "", no_elo: bool = False,
        no_shorts: bool = False) -> list[Path]:
    """Full FACEIT flow: analyze demo -> one card per Recognised Pro
    (+ optional shorts extraction). Callable entry used by the unified
    create_backlog.py dispatcher."""
    print(f"[FACEIT] Analyzing {demo.name} (csdm json) ...")
    try:
        data = _csdm_json(demo)
    except Exception as e:
        print(f"[ERR] {e}")
        sys.exit(1)

    accounts = _load_accounts()
    if not accounts:
        print("[ERR] No Recognised Pros in .data/player_accounts.json")
        sys.exit(1)

    map_name = map_override or _map_display(data.get("mapName", ""))
    match_id = match_id_arg.strip() or demo.stem
    match_slug = _match_slug(demo, match_id_arg)
    tournament = tournament.strip() or "FACEIT"

    # Recognised Pros only: match demo players to accounts by steam_id_64
    # (canonical nick + faceit id come from the account record, not the demo).
    pros: list[dict] = []
    skipped: list[str] = []
    for p in data.get("players", []):
        sid = str(p.get("steamId") or "").strip()
        if not sid:
            continue  # bots / empty slots have no steam id
        acct = accounts.get(sid)
        if not acct:
            skipped.append(p.get("name") or "?")
            continue
        faceit_id = str(acct.get("faceit_id") or "").strip()
        if faceit_id == "-1":
            faceit_id = ""  # sentinel for accounts without a FACEIT id
        rating = p.get("hltvRating2")
        pros.append({
            "canonical_nick": str(acct.get("nickname") or p.get("name") or "?").strip(),
            "steam_id": sid,
            "faceit_id": faceit_id,
            "faceit_nickname": str(acct.get("faceit_nickname") or "").strip(),
            "faceit_match_id": match_id,
            "team": str(p.get("teamName") or "").strip(),
            "rating": _round2(rating),
            "kills": p.get("killCount"),
            "deaths": p.get("deathCount"),
        })
    if skipped:
        print(f"  [SKIP] {len(skipped)} non-pro player(s): {', '.join(skipped[:5])}"
              + (" ..." if len(skipped) > 5 else ""))
    if not pros:
        print("[WARN] No Recognised Pros in this demo — nothing to backlog.")
        sys.exit(0)

    # Rank by in-match rating — best performer first is the processing order.
    pros.sort(key=lambda x: (x["rating"] if x["rating"] is not None else -1), reverse=True)

    # Per-pro ELO for the POV player + opposing-team average (title/thumbnail).
    if no_elo:
        elo_by_pro: dict[str, dict] = {}
    else:
        print("[FACEIT] Fetching ELOs (POV player + opposing-team average)...")
        elo_by_pro = {}
        for pro in pros:
            ef = _elo_sync(demo, pro["steam_id"])
            if ef:
                elo_by_pro[pro["steam_id"]] = ef
                print(f"  [ELO] {pro['canonical_nick']:12s} "
                      + " ".join(f"{k}={v}" for k, v in ef.items()))

    written: list[Path] = []
    for pro in pros:
        priority = priority_from_rating(pro["rating"])
        card = _write_card(pro, demo=demo, map_name=map_name,
                           match_slug=match_slug, tournament=tournament,
                           elo_fields=elo_by_pro.get(pro["steam_id"]))
        written.append(card)
        rating_txt = f"{pro['rating']:.2f}" if pro["rating"] is not None else "n/a"
        print(f"  [{priority.upper():4s} r{rating_txt:5s}] {pro['canonical_nick']:12s} "
              f"{pro['team']:24s} -> {card.relative_to(PROJECT_ROOT).as_posix()}")

    from collections import Counter
    by_prio = Counter(priority_from_rating(p["rating"]) for p in pros)
    print(f"[OK] Created {len(written)} backlog card(s) under backlog/faceit/"
          + " ".join(f"{k}={v}" for k, v in sorted(by_prio.items())))
    missing_avatars = [pro["canonical_nick"] for pro in pros if not avatar_path(pro["canonical_nick"])]
    if missing_avatars:
        print(f"  [HINT] No cached avatar for: {', '.join(missing_avatars)}")
        print(f"         Fetch with: python scripts/faceit/faceit_avatar.py <nick>")

    # Shorts extraction (same pass over the demo): Recognised-Pros-gated,
    # one short_timeline.json per detected short under renders/shorts/shorts-{stem}/.
    if no_shorts:
        return written
    try:
        from shorts.build_short_timeline import (
            build_short_timeline, _build_short_slug,
            persist_action_timeline, short_json_payload,
        )
        from shorts import resolve_output_dir

        print("[SHORTS] Extracting short timelines (Recognised Pros only)...")
        timeline = build_short_timeline(demo, pros_only=True)
        dropped = timeline.get("_dropped_randos", 0)
        shorts_list = timeline.get("shorts", [])
        base_dir = resolve_output_dir(demo)
        persist_action_timeline(demo, timeline, output_dir=base_dir)
        if not shorts_list:
            suffix = f" ({dropped} non-pro short(s) filtered)" if dropped else ""
            print(f"[SHORTS] 0 shorts detected{suffix}")
            return written
        written_shorts = 0
        for short in shorts_list:
            slug = _build_short_slug(short)
            short_dir = base_dir / f"shorts-{slug}"
            short_dir.mkdir(parents=True, exist_ok=True)
            (short_dir / "short_timeline.json").write_text(
                json.dumps(short_json_payload(timeline, short), indent=2), encoding="utf-8")
            written_shorts += 1
        print(f"[SHORTS] {len(shorts_list)} shorts -> {written_shorts} files under "
              f"{base_dir.relative_to(PROJECT_ROOT).as_posix()}"
              + (f" ({dropped} non-pro short(s) filtered)" if dropped else ""))
    except Exception as e:
        print(f"[WARN] Shorts extraction failed (backlog cards unaffected): "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("demo_path", help="Path to the FACEIT .dem (e.g. demos/faceit/...)")
    ap.add_argument("--map", default="", help="Override map name (defaults to csdm mapName)")
    ap.add_argument("--tournament", default="", help="Event name (defaults to 'FACEIT')")
    ap.add_argument("--match-id", default="", help="FACEIT match id for run_id (defaults to demo stem)")
    ap.add_argument("--no-elo", action="store_true",
                    help="Skip FACEIT ELO fetch (title/thumbnail then omit the ELO line)")
    ap.add_argument("--no-shorts", action="store_true",
                    help="Skip short-timeline extraction (default: extracts shorts "
                         "for Recognised Pros right after the backlog cards)")
    args = ap.parse_args()

    demo = Path(args.demo_path).resolve()
    if not demo.exists():
        print(f"[ERR] demo not found: {demo}")
        sys.exit(1)

    run(demo, map_override=args.map, tournament=args.tournament,
        match_id_arg=args.match_id, no_elo=args.no_elo, no_shorts=args.no_shorts)


if __name__ == "__main__":
    main()
