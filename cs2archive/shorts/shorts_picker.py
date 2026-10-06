"""Globally ranked cuts and crash-safe, locked two-successes-per-day state."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cs2archive.shorts.output_paths import short_output_path
from cs2archive.shorts.view_prediction import cut_features, predict_log_views

SYDNEY = ZoneInfo("Australia/Sydney")
STATE_VERSION = 2
ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "youtube" / ".shorts_render_selection.json"


def sydney_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(SYDNEY).date().isoformat()


def candidate_id(demo: Path, short: dict) -> str:
    resolved = demo.resolve()
    try:
        demo_key = str(resolved.relative_to(ROOT.resolve()))
    except ValueError:
        demo_key = str(resolved)
    identity = [os.path.normcase(demo_key), str(short["pov_steam_id"]),
                int(short["start_tick"]), int(short["end_tick"])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    key: str
    timeline: Path
    output_dir: Path
    payload: dict
    short: dict
    features: dict
    log_views: float

    @property
    def video(self) -> Path:
        return short_output_path(self.output_dir, self.short)

    @property
    def views(self) -> float:
        return math.exp(self.log_views)


def scan_candidates(renders: Path, model: dict, recognised: set[str], *,
                    complete, stages: dict | None = None) -> tuple[list[Candidate], dict]:
    """Rank individual cuts, including legacy multi-cut timelines.

    ``complete`` checks final size/resolution. No writes, ledger claims or
    renderer launches happen here. Duplicate identities collapse before rank.
    """
    report = dict(timelines=0, cuts=0, rendered=0, uploaded_or_skipped=0,
                  unrecognized=0, missing_demo=0, invalid=0, duplicates=0, unknown_stage=0)
    found = {}
    excluded_keys = set()
    fixture_orgs = {}
    stages = stages or {}
    for timeline in sorted(renders.rglob("short_timeline.json")):
        report["timelines"] += 1
        try:
            payload = json.loads(timeline.read_text(encoding="utf-8-sig"))
            demo = Path(payload["demo_path"])
            if not demo.is_absolute():
                demo = ROOT / demo
            shorts = payload["shorts"]
            if not isinstance(shorts, list):
                raise ValueError("shorts must be a list")
            meta_path = timeline.parent / "upload_meta_shorts.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
        except (OSError, ValueError, KeyError, TypeError):
            report["invalid"] += 1
            continue
        for raw in shorts:
            report["cuts"] += 1
            try:
                short = dict(raw)
                if str(short.get("pov_steam_id")) not in recognised:
                    report["unrecognized"] += 1
                    continue
                key = candidate_id(demo, short)
                if int(short["end_tick"]) <= int(short["start_tick"]):
                    raise ValueError("invalid cut bounds")
                # A multi-cut source gets one isolated folder per selected cut.
                out = timeline.parent if len(shorts) == 1 else timeline.parent / "selected" / key[:16]
                if meta.get("upload_status") in {"completed", "skipped"}:
                    report["uploaded_or_skipped"] += 1
                    excluded_keys.add(key)
                    continue
                if (complete(short_output_path(out, short))
                        or (len(shorts) > 1 and complete(short_output_path(timeline.parent, short)))):
                    report["rendered"] += 1
                    excluded_keys.add(key)
                    continue  # Existing deliverables never get re-rendered by this picker.
                if not demo.is_file():
                    report["missing_demo"] += 1
                    continue
                short.setdefault("stage", payload.get("stage") or payload.get("match_stage")
                                 or stages.get(demo.parent.name) or stages.get(demo.parent.name.split("-", 1)[0]))
                from cs2archive.shorts.demand_gate import folder_orgs
                if demo not in fixture_orgs:
                    fixture_orgs[demo] = folder_orgs(demo)
                features = cut_features(short, model, orgs=fixture_orgs[demo])
                report["unknown_stage"] += features["stage"] is None
                log_views = predict_log_views(features, model)
                if not math.isfinite(math.exp(log_views)):
                    raise ValueError("overflow prediction")
                candidate = Candidate(key, timeline, out, payload, short, features, log_views)
                if key in found:
                    report["duplicates"] += 1
                else:
                    found[key] = candidate
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                report["invalid"] += 1
    return sorted((c for key, c in found.items() if key not in excluded_keys),
                  key=lambda c: (-c.log_views, c.key)), report


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name, suffix=".tmp", delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


@contextmanager
def picker_lock(path: Path = LEDGER):
    """Nonblocking exclusive process lock, held through the render pass."""
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if lock.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PickerState:
    """Caller holds picker_lock for all reads and state transitions."""
    def __init__(self, path: Path = LEDGER):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(self.data, dict) or self.data.get("version") != STATE_VERSION
                    or not isinstance(self.data.get("entries"), dict)):
                raise ValueError("invalid shorts render ledger; refusing to reset quota")
            for entry in self.data["entries"].values():
                if not isinstance(entry, dict) or entry.get("state") not in {"pending", "reserved", "rendered", "expired"}:
                    raise ValueError("invalid shorts ledger entry")
                first_seen = datetime.fromisoformat(entry["first_seen"])
                if first_seen.tzinfo is None:
                    raise ValueError("ledger first_seen must be timezone-aware")
                if entry["state"] == "reserved" and not entry.get("video"):
                    raise ValueError("reserved entry lacks output path")
                if entry["state"] == "rendered":
                    datetime.strptime(entry["completed_day"], "%Y-%m-%d")
        else:
            self.data = {"version": STATE_VERSION, "entries": {}}

    @property
    def entries(self) -> dict:
        return self.data["entries"]

    def save(self) -> None:
        atomic_json(self.path, self.data)

    def completed_today(self, now: datetime) -> int:
        return sum(e.get("state") == "rendered" and e.get("completed_day") == sydney_day(now)
                   for e in self.entries.values())

    def reconcile(self, complete, *, now: datetime, persist: bool = True) -> None:
        # A render may have succeeded before the parent could commit its ledger.
        for key, entry in self.entries.items():
            if entry["state"] != "reserved" or not entry.get("video"):
                continue
            video = Path(entry["video"])
            if complete(video):
                stamp = datetime.fromtimestamp(video.stat().st_mtime, timezone.utc)
                self.finish(key, now=stamp, persist=persist)

    def select(self, candidates: list[Candidate], *, now: datetime, limit: int = 2,
               max_age_days: int = 7) -> tuple[list[Candidate], dict]:
        available = []
        expired = previously_rendered = 0
        candidate_keys = {c.key for c in candidates}
        # Expire disappeared candidates and abandoned crash reservations too;
        # reconciliation has already recovered any completed reserved outputs.
        for entry in self.entries.values():
            age = (now - datetime.fromisoformat(entry["first_seen"])).total_seconds() / 86400
            if entry["state"] in {"pending", "reserved"} and age > max_age_days:
                entry.update(state="expired", reason="expired_stale")
        for candidate in candidates:
            entry = self.entries.setdefault(candidate.key, {
                "state": "pending", "first_seen": now.isoformat(),
            })
            if entry["state"] == "rendered":
                previously_rendered += 1
                continue
            if entry["state"] == "expired":
                entry.update(state="expired", reason="expired_stale")
                expired += 1
                continue
            available.append(candidate)
        # Finish crashed reservations before admitting new candidates; new work
        # is still globally ranked by prediction, never filesystem/demo order.
        available.sort(key=lambda c: (self.entries[c.key]["state"] != "reserved", -c.log_views, c.key))
        remaining = max(0, min(limit, 2 - self.completed_today(now)))
        return available[:remaining], dict(expired_stale=expired, previously_rendered=previously_rendered,
                                           pending=len(available), remaining_today=remaining,
                                           absent_expired=sum(e["state"] == "expired" and key not in candidate_keys
                                                              for key, e in self.entries.items()))

    def reserve(self, candidate: Candidate, *, now: datetime, model: dict) -> None:
        self.entries[candidate.key].update(state="reserved", reserved_day=sydney_day(now),
                                          timeline=str(candidate.timeline), video=str(candidate.video),
                                          predicted_views=candidate.views, features=candidate.features,
                                          model_fitted_at=model.get("fitted_at"))
        self.save()

    def finish(self, key: str, *, now: datetime, persist: bool = True) -> None:
        self.entries[key].update(state="rendered", completed_day=sydney_day(now),
                                 completed_at=now.isoformat())
        if persist:
            self.save()

    def fail(self, key: str, error: str) -> None:
        self.entries[key].update(state="pending", last_error=error)
        self.save()


def render_timeline(candidate: Candidate) -> Path:
    """Single-cut render input; source multi-cut timelines remain untouched."""
    if len(candidate.payload["shorts"]) == 1:
        return candidate.timeline
    destination = candidate.output_dir / "short_timeline.json"
    atomic_json(destination, {**candidate.payload, "shorts": [candidate.short], "short_count": 1})
    return destination
