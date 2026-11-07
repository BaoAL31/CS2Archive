"""Populate moment dossiers for every reliably linked moment on disk.

Population, in order of evidence strength:

1. **staged AllStar alignments** — ``clips/<clip_id>/alignment.json`` with an
   exact map+player+round+kill-count match (1,030 today). The dossier is built
   from the clip's own `alignment.json` / `events.parquet` / `state.parquet`
   (when present) plus the cached action timeline for its demo.
2. **durable linkage records** — `short_linkage` rows that carry a demo path and
   a round (`linked` or the ledger's `pending`). These are render-side evidence
   and have no clip folder, so they are built from the timeline alone.

Bounded state enrichment (`--enrich-state N`) parses **local** demos with
demoparser2 for moments whose window has no `state.parquet`, and writes the
snapshot to a **new** file (`clips/<id>/state_enriched.parquet`). It never
touches `state.parquet`, `events.parquet`, `alignment.json` or the timelines, and
it is resumable (a moment with an existing enriched snapshot is not re-parsed).

No downloads, no rendering, no upload, no demo deletion, no Steam interaction, no
external API or LLM calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import datetime as _dt

from cs2archive.shorts.social.dossier import (
    DossierStats, build_dossier, dossier_source_hash, load_state_rows, moment_id,
    write_atomic, write_dossier,
)
STATE_PROPS = ["steamid", "team_num", "X", "Y", "Z", "pitch", "yaw",
               "health", "spotted", "m_iItemDefinitionIndex"]
STATE_EVERY_TICKS = 8
DEFAULT_ROOT = Path("data/shorts-foundation/dossiers")


def timeline_dir_for(foundation_dir: Path) -> Path:
    return Path(foundation_dir) / "_timelines"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def timeline_path_for(demo_path: str | None, timeline_dir: Path) -> Path | None:
    """Cached timeline for a demo path (`<match-folder>__<demo-stem>.json`)."""
    if not demo_path:
        return None
    demo = Path(str(demo_path).replace("\\", "/"))
    candidate = Path(timeline_dir) / f"{demo.parent.name}__{demo.stem}.json"
    return candidate if candidate.is_file() else None


def iter_moments(foundation_dir: Path, *, limit: int = 0,
                 skip_quarantined: bool = True) -> list[dict]:
    """Enumerate moments from staged alignments (strongest evidence first)."""
    clips_dir = Path(foundation_dir) / "clips"
    if not clips_dir.is_dir():
        return []
    moments: list[dict] = []
    for clip_dir in sorted(clips_dir.iterdir()):
        if not clip_dir.is_dir():
            continue
        alignment = _load_json(clip_dir / "alignment.json")
        if not alignment:
            continue
        if skip_quarantined and alignment.get("quarantined"):
            continue
        alignment.setdefault("clip_id", clip_dir.name)
        moments.append({"kind": "clip", "clip_dir": clip_dir,
                        "alignment": alignment})
        if limit and len(moments) >= limit:
            break
    return moments


def iter_linkage_moments(store, *, exclude_clip_ids: set[str] | None = None,
                         limit: int = 0) -> list[dict]:
    """Linkage rows (render-side) that carry demo + round but no clip folder."""
    exclude_clip_ids = exclude_clip_ids or set()
    out: list[dict] = []
    for row in store.linkage_rows():
        record = dict(row)
        if not record.get("demo_path") or record.get("round") is None:
            continue
        # Stored as JSON text in SQLite; the dossier needs the list itself.
        for key in ("kill_ticks", "selection", "evidence"):
            value = record.get(key)
            if isinstance(value, str):
                try:
                    record[key] = json.loads(value)
                except json.JSONDecodeError:
                    record[key] = [] if key == "kill_ticks" else {}
        if record.get("short_id") in exclude_clip_ids:
            continue
        out.append({"kind": "linkage", "record": record})
        if limit and len(out) >= limit:
            break
    return out


def build_all(*, foundation_dir: Path = Path("data/shorts-foundation"),
              root: Path = DEFAULT_ROOT, store=None, limit: int = 0,
              force: bool = False, only: str | None = None,
              log=print) -> dict:
    """Build dossiers for every staged alignment + linkage record."""
    foundation_dir = Path(foundation_dir)
    root = Path(root)
    timeline_dir = timeline_dir_for(foundation_dir)
    stats = DossierStats()
    timelines: dict[str, dict] = {}

    def timeline_for(demo_path):
        path = timeline_path_for(demo_path, timeline_dir)
        if path is None:
            return None, None
        key = str(path)
        if key not in timelines:
            timelines[key] = _load_json(path)
        return timelines[key], path

    moments = iter_moments(foundation_dir, limit=limit)
    ledger = iter_linkage_moments(store, limit=limit) if store is not None else []
    # One dossier per *moment* (demo + round + player + window). Several AllStar
    # clips can describe the same moment, so they are collected into one document
    # instead of overwriting each other.
    grouped: dict[str, dict] = {}
    for moment in moments + ledger:
        stats.moments_seen += 1
        if only and moment.get("kind") != only:
            continue
        try:
            dossier = _dossier_for(moment, timeline_for, foundation_dir)
        except Exception as exc:  # noqa: BLE001 - one bad moment must not stop the run
            stats.failed += 1
            stats.failures.append(
                {"kind": moment.get("kind"),
                 "id": str(moment.get("clip_dir") or moment.get("record", {}).get("short_id")),
                 "error": f"{type(exc).__name__}: {exc}"})
            continue
        if dossier is None:
            stats.skipped_current += 1
            continue
        existing = grouped.get(dossier["moment_id"])
        if existing is None:
            grouped[dossier["moment_id"]] = dossier
        else:
            _merge_moment(existing, dossier)
    for dossier in grouped.values():
        status, _path = write_dossier(dossier, root=root, force=force)
        stats.by_source[dossier["source"]] = stats.by_source.get(dossier["source"], 0) + 1
        if status == "built":
            stats.built += 1
        else:
            stats.skipped_current += 1
        if stats.built and stats.built % 200 == 0:
            log(f"[dossier] built {stats.built} ...")
    index = stats.as_dict()
    from cs2archive.shorts.social.dossier import DOSSIER_VERSION

    index.update({
        "dossier_version": DOSSIER_VERSION,
        "root": str(root),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "availability": feature_availability(root),
    })
    write_atomic(root / "INDEX.json",
                 json.dumps(index, indent=1, sort_keys=True, ensure_ascii=False))
    return index


def _merge_moment(target: dict, extra: dict) -> None:
    """Fold a second evidence path for the same moment into the first dossier."""
    target.setdefault("staged_clips", [])
    for key in ("staged_clip", "linkage_record"):
        value = extra.get(key)
        if value and value not in target["staged_clips"]:
            target["staged_clips"].append(value)
    target.setdefault("sources", [target.get("source")])
    if extra.get("source") not in target["sources"]:
        target["sources"].append(extra["source"])
    target["clip_verification"]["allstar_clip_ids"] = sorted({
        *(target["clip_verification"].get("allstar_clip_ids") or []),
        *(_clip_ids(extra))})


def _clip_ids(dossier: dict) -> list[str]:
    ids = []
    entry = dossier.get("clip_verification", {})
    if entry.get("allstar_clip_id"):
        ids.append(str(entry["allstar_clip_id"]))
    for staged in dossier.get("staged_clips") or []:
        if staged.get("clip_dir"):
            ids.append(str(staged["clip_dir"]).replace("\\", "/").rsplit("/", 1)[-1])
    return ids


def _dossier_for(moment: dict, timeline_for, foundation_dir: Path) -> dict | None:
    if moment["kind"] == "clip":
        alignment = moment["alignment"]
        demo_path = alignment.get("demo")
        timeline, _path = timeline_for(demo_path)
        if not timeline:
            # No cached timeline: fall back to the clip's staged events only.
            events = _events_from_clip(moment["clip_dir"])
            dossier = build_dossier(timeline={"map": _load_json(
                moment["clip_dir"] / "match.json").get("map"),
                "demo_path": demo_path, **events},
                alignment=alignment,
                state_rows=load_state_rows(moment["clip_dir"]),
                source="allstar_alignment_no_timeline")
        else:
            dossier = build_dossier(
                timeline=timeline, alignment=alignment,
                state_rows=load_state_rows(moment["clip_dir"]),
                source="allstar_alignment")
        dossier["staged_clip"] = {
            "clip_dir": str(moment["clip_dir"]),
            "has_events_parquet": (moment["clip_dir"] / "events.parquet").is_file(),
            "has_state_parquet": (moment["clip_dir"] / "state.parquet").is_file(),
            "has_state_enriched": (moment["clip_dir"]
                                   / "state_enriched.parquet").is_file(),
            "alignment_confidence": alignment.get("confidence"),
            "alignment_method": alignment.get("method"),
        }
    else:
        record = moment["record"]
        timeline, _path = timeline_for(record.get("demo_path"))
        if not timeline:
            return None
        dossier = build_dossier(
            timeline=timeline, alignment=None,
            linkage=record, source=f"linkage_{record.get('status')}"
            if record.get("status") else "linkage")
        dossier["linkage_record"] = {
            "short_id": record.get("short_id"),
            "status": record.get("status"),
            "reason": record.get("reason"),
            "kill_ticks": record.get("kill_ticks"),
            "platform": record.get("platform"),
            "post_id": record.get("post_id"),
        }
    dossier["moment_id"] = moment_id(dossier)
    dossier["staged_clips"] = ([dossier["staged_clip"]]
                               if dossier.get("staged_clip") else [])
    dossier["sources"] = [dossier["source"]]
    dossier["clip_verification"]["allstar_clip_ids"] = _clip_ids(dossier)
    dossier["source_hash"] = dossier_source_hash(dossier)
    return dossier


def _events_from_clip(clip_dir: Path) -> dict:
    """Reconstruct the minimal timeline shape from a clip's packed events."""
    path = Path(clip_dir) / "events.parquet"
    if not path.is_file():
        return {}
    try:
        import pandas as pd

        frame = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - unreadable parquet = no evidence
        return {}
    out: dict = {}
    for row in frame.to_dict(orient="records"):
        key = str(row.get("key") or "")
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if key == "kills" and isinstance(payload, list):
            out["kills_all"] = payload
        elif key in {"player_hurt", "weapon_fire", "player_death"}:
            out.setdefault("raw_events", {})[key] = {"rows": payload or []}
    return out


def feature_availability(root: Path) -> dict:
    """Aggregate availability across built dossiers (no fabrication, just counts)."""
    root = Path(root)
    totals: dict[str, int] = {}
    count = 0
    for path in sorted(root.glob("*.json")):
        if path.name == "INDEX.json":
            continue
        try:
            dossier = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        count += 1
        for key, value in (dossier.get("availability") or {}).items():
            if value is True:
                totals[key] = totals.get(key, 0) + 1
    return {"dossiers": count,
            "present": dict(sorted(totals.items())),
            "present_share": {k: round(v / count, 4) for k, v in sorted(totals.items())}
            if count else {}}


def plan_state_enrichment(*, foundation_dir: Path = Path("data/shorts-foundation"),
                          limit: int = 0) -> list[dict]:
    """Moments whose window has no staged state snapshot (enrichment candidates)."""
    foundation_dir = Path(foundation_dir)
    todo: list[dict] = []
    for moment in iter_moments(foundation_dir):
        clip_dir = moment["clip_dir"]
        if (clip_dir / "state.parquet").is_file() or \
                (clip_dir / "state_enriched.parquet").is_file():
            continue
        alignment = moment["alignment"]
        if not alignment.get("demo"):
            continue
        todo.append({"clip_dir": clip_dir, "alignment": alignment,
                     "demo": alignment["demo"],
                     "tick_start": alignment.get("tick_start"),
                     "tick_end": alignment.get("tick_end")})
        if limit and len(todo) >= limit:
            break
    return todo


def enrich_state_batch(*, foundation_dir: Path = Path("data/shorts-foundation"),
                       limit: int = 30, log=print) -> dict:
    """Parse local demos for moment windows lacking state. Bounded, resumable.

    Writes only new ``state_enriched.parquet`` files; existing artifacts are never
    modified. Failures are recorded per demo and never abort the batch.
    """
    import pandas as pd

    stats = {"candidates": 0, "enriched": 0, "demos_parsed": 0,
             "moments_without_demo": 0, "demos_unavailable": 0,
             "skipped_existing": 0, "failures": []}
    todo = plan_state_enrichment(foundation_dir=foundation_dir)
    stats["candidates"] = len(todo)

    def _exists(item: dict) -> bool:
        return Path(str(item["demo"]).replace("\\", "/")).is_file()

    # Work on the moments we can actually parse first: a bounded run must not
    # spend its budget on demos that are no longer on disk.
    available = [item for item in todo if _exists(item)]
    unavailable = [item for item in todo if not _exists(item)]
    stats["moments_without_demo"] = len(unavailable)
    stats["demos_unavailable"] = len({str(i["demo"]) for i in unavailable})

    by_demo: dict[str, list[dict]] = {}
    for item in available:
        by_demo.setdefault(str(item["demo"]), []).append(item)

    for demo_path, items in by_demo.items():
        if limit and stats["enriched"] >= limit:
            break
        demo = Path(str(demo_path).replace("\\", "/"))
        if not demo.is_file():
            stats["failures"].append({"demo": str(demo), "error": "demo not found"})
            continue
        windows = [(int(it["tick_start"]), int(it["tick_end"])) for it in items
                   if it.get("tick_start") is not None and it.get("tick_end") is not None]
        if not windows:
            stats["failures"].append({"demo": str(demo), "error": "no tick window"})
            continue
        lo = min(w[0] for w in windows)
        hi = max(w[1] for w in windows)
        ticks = list(range(max(0, lo), hi + 1, STATE_EVERY_TICKS))
        try:
            import demoparser2 as dp

            parser = dp.DemoParser(str(demo))
            frame = parser.parse_ticks(STATE_PROPS, ticks=ticks)
            rows = frame.to_dict(orient="records") if frame is not None else []
        except Exception as exc:  # noqa: BLE001 - record and move on
            stats["failures"].append({"demo": str(demo),
                                      "error": f"{type(exc).__name__}: {exc}"})
            continue
        stats["demos_parsed"] += 1
        for item in items:
            if limit and stats["enriched"] >= limit:
                break
            clip_dir = Path(item["clip_dir"])
            if (clip_dir / "state_enriched.parquet").is_file():
                stats["skipped_existing"] += 1
                continue
            lo_i, hi_i = int(item["tick_start"]), int(item["tick_end"])
            window = [row for row in rows
                      if lo_i <= int(row.get("tick", -1)) <= hi_i]
            if not window:
                stats["failures"].append(
                    {"clip_dir": str(clip_dir), "error": "no samples in window"})
                continue
            pd.DataFrame(window).to_parquet(
                clip_dir / "state_enriched.parquet", index=False)
            stats["enriched"] += 1
        log(f"[dossier-state] {demo.name}: {len(items)} moments, "
            f"{len(rows)} samples")
    return stats
