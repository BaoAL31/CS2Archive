"""Stamp detector kinds onto joined Allstar Clip Observations, then refit.

Parses each joined demo once (resume-safe cache), matches clips by
steam64 + round, and writes `.data/demo_kind_stamps.json`. Factory labels
keep clutch/multikill; demo fills empty categories (flick, perfect_shots,
defuse, 1v5, 2vX, …).

    python scripts/shorts/stamp_demo_kinds.py
    python scripts/shorts/stamp_demo_kinds.py --no-fit
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOIN_PATH = ROOT / ".data" / "clip_demo_join.json"
STAMPS_PATH = ROOT / ".data" / "demo_kind_stamps.json"
CACHE_DIR = ROOT / ".data" / "demo_kind_cache"


def kinds_by_player_round(timeline: dict) -> dict[tuple[str, int], set[str]]:
    from shorts.clip_observation import kinds_from_cut

    out: dict[tuple[str, int], set[str]] = {}
    for short in timeline.get("shorts") or []:
        sid = str(short.get("pov_steam_id") or "")
        try:
            rn = int(short.get("round"))
        except (TypeError, ValueError):
            continue
        if not sid or rn <= 0:
            continue
        out.setdefault((sid, rn), set()).update(kinds_from_cut(short))
    return out


def _cache_path(demo: Path) -> Path:
    return CACHE_DIR / f"{demo.stem}.json"


def _rel(demo: Path) -> str:
    try:
        return demo.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return demo.as_posix()


def load_cached_kinds(demo: Path) -> dict[str, list[str]] | None:
    path = _cache_path(demo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    kinds = data.get("kinds")
    if not isinstance(kinds, dict):
        return None
    return {str(k): list(v) for k, v in kinds.items() if isinstance(v, list)}


def save_cached_kinds(demo: Path, kinds: dict[str, list[str]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(demo).write_text(
        json.dumps({"ok": True, "demo_path": _rel(demo), "kinds": kinds}) + "\n",
        encoding="utf-8",
    )


def parse_demo_kinds(demo: Path) -> dict[str, list[str]]:
    from shorts.build_short_timeline import build_short_timeline

    timeline = build_short_timeline(demo, pros_only=False)
    by = kinds_by_player_round(timeline)
    return {f"{sid}|{rn}": sorted(kinds) for (sid, rn), kinds in by.items()}


def unique_demo_paths(clips: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for clip in clips:
        paths = clip.get("demo_paths") or []
        if not paths and clip.get("demo_path"):
            paths = [clip["demo_path"]]
        for raw in paths:
            if raw and raw not in seen:
                seen.add(raw)
                out.append(str(raw))
    return out


def resolve_demo(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def kinds_for_clip(
    clip: dict,
    parsed: dict[str, dict[str, list[str]]],
) -> list[str]:
    sid = str(clip.get("steamid") or "")
    try:
        rn = int(clip.get("round"))
    except (TypeError, ValueError):
        return []
    if not sid or rn <= 0:
        return []
    key = f"{sid}|{rn}"
    kinds: set[str] = set()
    paths = clip.get("demo_paths") or []
    if not paths and clip.get("demo_path"):
        paths = [clip["demo_path"]]
    for raw in paths:
        kinds.update(parsed.get(str(raw), {}).get(key) or [])
    return sorted(kinds)


def joined_clips(join_path: Path | None = None) -> list[dict]:
    dest = join_path or JOIN_PATH
    if not dest.is_file():
        return []
    payload = json.loads(dest.read_text(encoding="utf-8"))
    return [
        c for c in (payload.get("clips") or [])
        if isinstance(c, dict) and c.get("status") == "joined"
    ]


def stamp_joined_clips(
    *,
    join_path: Path | None = None,
    parsed: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, list[str]]:
    clips = joined_clips(join_path)
    parsed = parsed or {}
    stamps: dict[str, list[str]] = {}
    for clip in clips:
        cid = str(clip.get("clip_id") or "")
        extra = kinds_for_clip(clip, parsed)
        if cid and extra:
            stamps[cid] = extra
    return stamps


def run(
    *,
    join_path: Path | None = None,
    out_path: Path | None = None,
    fit: bool = True,
) -> dict:
    clips = joined_clips(join_path)
    demos = unique_demo_paths(clips)
    parsed: dict[str, dict[str, list[str]]] = {}
    errors: list[dict] = []
    for i, raw in enumerate(demos, 1):
        demo = resolve_demo(raw)
        cached = load_cached_kinds(demo) if demo.is_file() else None
        if cached is not None:
            parsed[raw] = cached
            print(f"[{i}/{len(demos)}] cache {raw}", flush=True)
            continue
        if not demo.is_file():
            errors.append({"demo_path": raw, "error": "missing"})
            print(f"[{i}/{len(demos)}] missing {raw}", flush=True)
            continue
        print(f"[{i}/{len(demos)}] parse {raw}", flush=True)
        try:
            kinds = parse_demo_kinds(demo)
        except Exception as exc:
            errors.append({"demo_path": raw, "error": f"{type(exc).__name__}: {exc}"})
            traceback.print_exc()
            continue
        save_cached_kinds(demo, kinds)
        parsed[raw] = kinds

    stamps = stamp_joined_clips(join_path=join_path, parsed=parsed)
    kind_counts: Counter[str] = Counter()
    for extra in stamps.values():
        kind_counts.update(extra)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "demos": len(demos),
        "demos_parsed": len(parsed),
        "errors": errors,
        "joined_clips": len(clips),
        "stamped_clips": len(stamps),
        "kind_counts": dict(kind_counts),
        "stamps": stamps,
    }
    dest = out_path or STAMPS_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {dest} stamped={len(stamps)}/{len(clips)} "
        f"kinds={dict(kind_counts)} errors={len(errors)}",
        flush=True,
    )
    if fit:
        from shorts.fit_partial_stars import refresh_partial_stars

        stars = refresh_partial_stars()
        print(
            f"refit kinds={list((stars.get('kind') or {}).keys())} "
            f"rows={stars.get('_rows')}",
            flush=True,
        )
        payload["kind_stars"] = stars.get("kind") or {}
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fit", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "scripts"))
    from _pathsetup import ensure
    ensure()
    run(fit=not args.no_fit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
