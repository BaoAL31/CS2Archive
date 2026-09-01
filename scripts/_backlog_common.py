"""Shared helpers for backlog creators (HLTV + FACEIT).

Single source of truth for:
  - rating -> priority bucket thresholds
  - avatar cache lookup
  - Recognised-Pro account lookup by steam64
  - backlog card writing

Import via `from _backlog_common import ...` (scripts/ is on sys.path after
`_pathsetup.ensure()`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

ACCOUNTS_PATH = PROJECT_ROOT / ".data" / "player_accounts.json"

# Rating >= HIGH_RATING -> high, >= MID_RATING -> mid, else low.
HIGH_RATING = 1.5
MID_RATING = 1.0


def rating_bucket(rating: float | None, *, mid_name: str = "mid",
                  unknown: str | None = "mid") -> str:
    """Priority bucket from an in-match rating.

    mid_name: HLTV layout uses "medium", FACEIT uses "mid" (keep each
    source's historical folder names). unknown=None means a missing rating
    is an error condition for the caller; otherwise it maps to `unknown`.
    """
    if rating is None or rating != rating:  # None or NaN
        if unknown is None:
            raise ValueError("rating required")
        return unknown
    if rating >= HIGH_RATING:
        return "high"
    if rating >= MID_RATING:
        return mid_name
    return "low"


def load_accounts_by_steam() -> dict[str, dict]:
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


def find_avatar(nickname: str) -> str:
    """Project-relative path to the player's cached avatar, or "".

    Layout: demos/avatars/{nick}/{source}/{nick}.{png,jpg,jpeg} where source
    is hltv|faceit. Tries the raw (lowercased) nick and then the canonical
    nickname from player_accounts.json.
    """
    base = PROJECT_ROOT / "demos" / "avatars"
    candidates = [nickname.strip().lower()]
    try:
        from faceit_names import canonical_nick
        canon = canonical_nick(nickname)
        if canon and canon.lower() not in candidates:
            candidates.append(canon.lower())
    except Exception:
        pass
    for nick in candidates:
        folder = base / nick
        if not folder.is_dir():
            continue
        for source in ("hltv", "faceit"):
            for ext in (".png", ".jpg", ".jpeg"):
                p = folder / source / f"{nick}{ext}"
                if p.exists():
                    return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return ""


def write_card(meta: dict, backlog_file: Path) -> Path:
    """Persist one backlog card as pretty JSON."""
    meta = dict(meta)
    meta.setdefault("pipeline_cmd", pipeline_cmd(backlog_file))
    backlog_file.parent.mkdir(parents=True, exist_ok=True)
    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return backlog_file


def pipeline_cmd(backlog_file: Path) -> str:
    """Printable pipeline invocation using this interpreter, not a baked conda path."""
    rel = rel_to_project(Path(backlog_file))
    return (
        f"$env:PYTHONPATH=.; & {sys.executable} "
        f"scripts/pov/pipeline.py --backlog {rel}"
    )


def rel_to_project(path: Path) -> str:
    """Project-relative POSIX path string (absolute fallback outside root)."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def detect_demo_source(arg: str) -> str:
    """Classify a CLI argument: 'faceit' | 'hltv' | 'url'.

    A path to an existing .dem under demos/faceit (or any .dem when the
    caller opts in) routes to the FACEIT flow; anything URL-shaped routes
    to the HLTV match flow.
    """
    a = arg.strip()
    if a.lower().startswith(("http://", "https://")):
        return "url"
    p = Path(a)
    if p.suffix.lower() == ".dem" and p.exists():
        norm = str(p.resolve()).replace("\\", "/").lower()
        return "faceit" if "/demos/faceit/" in norm else "hltv"
    return "unknown"
