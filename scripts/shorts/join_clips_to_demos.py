"""Assign scraped Clip Observations to local HLTV match demos.

Allstar rows already have HLTV match id, steam64, round, and a map in the
title. This join is path-only (no demo parse): match folder + map slug → .dem.

    python scripts/shorts/join_clips_to_demos.py
    python scripts/shorts/join_clips_to_demos.py --out .data/clip_demo_join.json
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMOS_HLTV = ROOT / "demos" / "hltv"
ALLSTAR_JSONL = ROOT / ".data" / "allstar_hltv_probe.jsonl"
TO_JSONL = ROOT / ".data" / "to_shorts_observations.jsonl"
HISTORY_FILE = ROOT / ".data" / "download_history.json"
OUT_DEFAULT = ROOT / ".data" / "clip_demo_join.json"
MIN_DEMO_BYTES = 1_000_000

# Longest first so "dust 2" wins over a stray "dust".
_MAP_SLUGS: tuple[tuple[str, str], ...] = (
    ("dust 2", "dust2"),
    ("dust2", "dust2"),
    ("cobblestone", "cbble"),
    ("overpass", "overpass"),
    ("ancient", "ancient"),
    ("inferno", "inferno"),
    ("vertigo", "vertigo"),
    ("anubis", "anubis"),
    ("mirage", "mirage"),
    ("office", "office"),
    ("italy", "italy"),
    ("mills", "mills"),
    ("thera", "thera"),
    ("basalt", "basalt"),
    ("cache", "cache"),
    ("train", "train"),
    ("nuke", "nuke"),
    ("edan", "edan"),
    ("cbble", "cbble"),
)


def map_slug_from_title(title: str) -> str | None:
    text = str(title or "")
    if not text:
        return None
    for phrase, slug in _MAP_SLUGS:
        if re.search(rf"\b{re.escape(phrase)}\b", text, re.I):
            return slug
    return None


def _map_aliases(slug: str) -> tuple[str, ...]:
    if slug == "dust2":
        return ("dust2", "dust_2")
    if slug == "cbble":
        return ("cbble", "cobblestone")
    if slug == "office":
        return ("office",)
    return (slug,)


def _name_has_map(name: str, slug: str) -> bool:
    low = name.lower()
    for alias in _map_aliases(slug):
        if (
            f"-{alias}.dem" in low
            or f"-{alias}-p" in low
            or f"_{alias}.dem" in low
            or f"_{alias}-p" in low
        ):
            return True
    return False


def demos_for_map(
    folder: Path,
    map_slug: str,
    *,
    min_bytes: int = MIN_DEMO_BYTES,
) -> list[Path]:
    if not folder.is_dir() or not map_slug:
        return []
    hits: list[Path] = []
    for path in folder.glob("*.dem"):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < min_bytes:
            continue
        if _name_has_map(path.name, map_slug):
            hits.append(path)
    return sorted(hits)


def index_hltv_demo_dirs(
    demos_root: Path | None = None,
    history: list[dict] | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Path]:
    """HLTV match id → match folder. Prefer ``{id}-slug`` dirs."""
    base = root or ROOT
    dest = demos_root if demos_root is not None else DEMOS_HLTV
    out: dict[str, Path] = {}
    if dest.is_dir():
        for path in dest.iterdir():
            if not path.is_dir() or path.name.lower() == "faceit":
                continue
            hit = re.match(r"^(\d{5,})-", path.name)
            if hit:
                out[hit.group(1)] = path
    for rec in history or []:
        mid = str(rec.get("match_id") or "")
        if not mid or mid in out:
            continue
        raw = rec.get("demo_path") or ""
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        folder = path.parent if path.suffix.lower() == ".dem" else path
        if folder.is_dir():
            out[mid] = folder
    return out


def load_download_history(path: Path | None = None) -> list[dict]:
    dest = path or HISTORY_FILE
    if not dest.is_file():
        return []
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _rel(path: Path, root: Path | None = None) -> str:
    base = root or ROOT
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _usable_demos(folder: Path, *, min_bytes: int) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    for path in folder.glob("*.dem"):
        try:
            if path.stat().st_size >= min_bytes:
                out.append(path)
        except OSError:
            continue
    return out


def _clip_join(
    *,
    source: str,
    clip: dict,
    match_id: str,
    folder: Path | None,
    min_bytes: int,
    root: Path | None = None,
    page_match_id: str | None = None,
    page_folder: Path | None = None,
) -> dict:
    title = str(clip.get("title") or clip.get("label") or "")
    map_slug = map_slug_from_title(title)
    row = {
        "source": source,
        "clip_id": str(clip.get("clip_id") or ""),
        "match_id": match_id or None,
        "page_match_id": page_match_id or None,
        "steamid": str(clip.get("steamid") or "") or None,
        "player": clip.get("player"),
        "round": clip.get("round"),
        "label": str(clip.get("label") or title),
        "title": title,
        "map": map_slug,
        "demo_path": None,
        "demo_paths": [],
        "status": "no_match_demo",
    }
    if not match_id:
        row["status"] = "no_match_id"
        return row
    if folder is None:
        if (
            page_match_id
            and page_match_id != match_id
            and page_folder is not None
            and _usable_demos(page_folder, min_bytes=min_bytes)
        ):
            row["status"] = "match_id_mismatch"
        return row
    if not _usable_demos(folder, min_bytes=min_bytes):
        return row
    if not map_slug:
        row["status"] = "no_map_in_title"
        return row
    hits = demos_for_map(folder, map_slug, min_bytes=min_bytes)
    if not hits:
        row["status"] = "no_map_demo"
        return row
    row["demo_paths"] = [_rel(p, root) for p in hits]
    row["demo_path"] = row["demo_paths"][0]
    row["status"] = "joined"
    return row


def join_allstar_jsonl(
    path: Path | None = None,
    dirs: dict[str, Path] | None = None,
    *,
    min_bytes: int = MIN_DEMO_BYTES,
    root: Path | None = None,
) -> list[dict]:
    dest = path or ALLSTAR_JSONL
    if not dest.is_file():
        return []
    folders = dirs if dirs is not None else index_hltv_demo_dirs(root=root)
    out: list[dict] = []
    seen: set[str] = set()
    for line in dest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        page_id = str(row.get("match_id") or "")
        page_folder = folders.get(page_id)
        for clip in row.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            match_id = str(clip.get("match_id") or page_id)
            rec = _clip_join(
                source="allstar",
                clip=clip,
                match_id=match_id,
                folder=folders.get(match_id),
                min_bytes=min_bytes,
                root=root,
                page_match_id=page_id or None,
                page_folder=page_folder,
            )
            key = rec["clip_id"] or f"{rec.get('steamid')}:{rec.get('title')}:{rec.get('round')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
    return out


def join_to_jsonl(
    path: Path | None = None,
    dirs: dict[str, Path] | None = None,
    *,
    min_bytes: int = MIN_DEMO_BYTES,
    root: Path | None = None,
) -> list[dict]:
    dest = path or TO_JSONL
    if not dest.is_file():
        return []
    folders = dirs if dirs is not None else index_hltv_demo_dirs(root=root)
    out: list[dict] = []
    for line in dest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        if not source or source == "allstar":
            continue
        match_id = str(row.get("match_id") or "")
        out.append(
            _clip_join(
                source=source,
                clip=row,
                match_id=match_id,
                folder=folders.get(match_id),
                min_bytes=min_bytes,
                root=root,
            )
        )
    return out


def _summarize(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    matches: set[str] = set()
    joined_matches: set[str] = set()
    for row in rows:
        st = str(row.get("status") or "")
        by_status[st] = by_status.get(st, 0) + 1
        mid = row.get("match_id")
        if mid:
            matches.add(str(mid))
            if st == "joined":
                joined_matches.add(str(mid))
    return {
        "clips": len(rows),
        "matches": len(matches),
        "joined_clips": by_status.get("joined", 0),
        "joined_matches": len(joined_matches),
        "by_status": by_status,
    }


def build_join(
    *,
    allstar_path: Path | None = None,
    to_path: Path | None = None,
    demos_root: Path | None = None,
    history: list[dict] | None = None,
    min_bytes: int = MIN_DEMO_BYTES,
    root: Path | None = None,
) -> dict:
    base = root or ROOT
    hist = history if history is not None else load_download_history()
    dirs = index_hltv_demo_dirs(demos_root, hist, root=base)
    allstar = join_allstar_jsonl(
        allstar_path, dirs, min_bytes=min_bytes, root=base,
    )
    to_rows = join_to_jsonl(to_path, dirs, min_bytes=min_bytes, root=base)
    clips = allstar + to_rows
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "demo_folders": len(dirs),
        "sources": {
            "allstar": {
                "store": (allstar_path or ALLSTAR_JSONL).is_file(),
                **_summarize(allstar),
            },
            "to": {
                "store": (to_path or TO_JSONL).is_file(),
                **_summarize(to_rows),
            },
        },
        "summary": _summarize(clips),
        "clips": clips,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args(argv)
    payload = build_join()
    dest = args.out
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    summary = payload["summary"]
    print(f"wrote {dest}")
    print(
        f"clips={summary['clips']} joined={summary['joined_clips']} "
        f"matches={summary['matches']} joined_matches={summary['joined_matches']}"
    )
    print(f"status {summary['by_status']}")
    print(f"sources {payload['sources']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
