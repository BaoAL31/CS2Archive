"""Full-match FACEIT backlog creator.

Given an already-downloaded FACEIT demo, runs `csdm json` to compute
per-player stats, keeps **Recognised Pros** only (`.data/player_accounts.json`
by steam64 — see `docs/adr/0002-single-player-accounts-store.md`), and writes
one backlog card per pro.

No ELO: matches are pre-filtered by pro performance, so rating only picks the
priority bucket, not the pipeline.

This is the "full match POVs" half of the FACEIT flow. The other half is the
individual single-player flow: `scripts/faceit/create_faceit_backlog.py`
(one card per invocation) + the standard `pipeline.py`.

Usage:
    python scripts/faceit/create_faceit_match_backlog.py <demo_path>
                                  [--map <map>] [--tournament <name>]
                                  [--match-id <faceit_match_id>]

Output: backlog/faceit/{priority}/{player}-{map}-{match_slug}.json
Each card carries the pipeline fields plus: rating, kd, team,
faceit_match_id, faceit_id, faceit_nickname.
"""

from __future__ import annotations

import argparse
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

BACKLOG_DIR = PROJECT_ROOT / "backlog" / "faceit"
ACCOUNTS_PATH = PROJECT_ROOT / ".data" / "player_accounts.json"
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
    """steam_id_64 -> account record for every Recognised Pro."""
    if not ACCOUNTS_PATH.exists():
        return {}
    data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    players = data if isinstance(data, list) else data.get("players", [])
    out: dict[str, dict] = {}
    for p in players:
        sid = str(p.get("steam_id") or "").strip()
        if sid:
            out[sid] = p
    return out


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


def _round2(value) -> float | None:
    if isinstance(value, (int, float)) and value == value:  # skip NaN
        return round(float(value), 2)
    return None


def priority_from_rating(rating: float | None) -> str:
    """Priority bucket from the in-match rating — mirrors HLTV get_priority
    thresholds but uses the faceit flow's bucket names (high/mid/low,
    matching create_faceit_backlog.py --priority choices)."""
    if rating is None:
        return "mid"  # unknown performance -> middle of the queue
    if rating >= 1.5:
        return "high"
    if rating >= 1.0:
        return "mid"
    return "low"


def _write_card(pro: dict, *, demo: Path, map_name: str, match_slug: str,
                tournament: str) -> Path:
    priority = priority_from_rating(pro["rating"])
    player_key = re.sub(r"[^a-z0-9]+", "-", pro["canonical_nick"].lower()).strip("-")
    slug = f"{player_key}-{map_name.lower()}-{match_slug}"
    backlog_dir = BACKLOG_DIR / priority
    backlog_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = backlog_dir / f"{slug}.json"

    av_path = avatar_path(pro["canonical_nick"])
    try:
        demo_rel = str(demo.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        demo_rel = str(demo).replace("\\", "/")

    meta = {
        "player": pro["canonical_nick"],
        "map": map_name,
        "steam_id": pro["steam_id"],
        "demo_path": demo_rel,
        "tournament": tournament,
        "priority": priority,
        "rating": pro["rating"],
        "kd": pro["kd"],
        "team": pro["team"],
        "is_faceit": True,
        "faceit_match_id": pro["faceit_match_id"],
        "faceit_id": pro["faceit_id"],
        "faceit_nickname": pro["faceit_nickname"],
        "avatar_path": str(av_path.relative_to(PROJECT_ROOT)).replace("\\", "/") if av_path else "",
        "pipeline_cmd": (
            f'$env:PYTHONPATH=.; & C:/Users/jembo/anaconda3/envs/cs2archive/python.exe '
            f'scripts/pov/pipeline.py --backlog backlog/faceit/{priority}/{slug}.json --overlay-only'
        ),
    }
    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return backlog_file


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("demo_path", help="Path to the FACEIT .dem (e.g. demos/faceit/...)")
    ap.add_argument("--map", default="", help="Override map name (defaults to csdm mapName)")
    ap.add_argument("--tournament", default="", help="Event name (defaults to 'FACEIT')")
    ap.add_argument("--match-id", default="", help="FACEIT match id for run_id (defaults to demo stem)")
    args = ap.parse_args()

    demo = Path(args.demo_path).resolve()
    if not demo.exists():
        print(f"[ERR] demo not found: {demo}")
        sys.exit(1)

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

    map_name = args.map or _map_display(data.get("mapName", ""))
    match_id = args.match_id.strip() or demo.stem
    match_slug = _match_slug(demo, args.match_id)
    tournament = args.tournament.strip() or "FACEIT"

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
            "kd": _round2(p.get("killDeathRatio")),
        })
    if skipped:
        print(f"  [SKIP] {len(skipped)} non-pro player(s): {', '.join(skipped[:5])}"
              + (" ..." if len(skipped) > 5 else ""))
    if not pros:
        print("[WARN] No Recognised Pros in this demo — nothing to backlog.")
        sys.exit(0)

    # Rank by in-match rating — best performer first is the processing order.
    pros.sort(key=lambda x: (x["rating"] if x["rating"] is not None else -1), reverse=True)

    written: list[Path] = []
    for pro in pros:
        priority = priority_from_rating(pro["rating"])
        card = _write_card(pro, demo=demo, map_name=map_name,
                           match_slug=match_slug, tournament=tournament)
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


if __name__ == "__main__":
    main()
