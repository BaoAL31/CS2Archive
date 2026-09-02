"""Partial-star fit and daily Clip Observation refresh."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STARS_PATH = ROOT / ".data" / "partial_stars.json"
ALLSTAR_JSONL = ROOT / ".data" / "allstar_hltv_probe.jsonl"
TO_JSONL = ROOT / ".data" / "to_shorts_observations.jsonl"
DEMO_KIND_STAMPS = ROOT / ".data" / "demo_kind_stamps.json"
LISTENER_LOCK = ROOT / ".listener" / "hltv.json.lock"
YOUTUBE_VIDEO_URL = "https://www.googleapis.com/youtube/v3/videos"
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
WINDOW_DAYS = 180
DAILY_NEW_MATCHES = 15
KIND_LEVELS = (
    "1v5_won", "1v4_won", "1v3_won", "2vx_won",
    "ace", "4k", "3k",
    "flick", "perfect_shots", "wallbang", "knife", "defuse",
)
STAGE_LEVELS = ("group", "playoff", "grand_final")


def rows_in_window(rows: list[dict], *, window_days: int = WINDOW_DAYS) -> list[dict]:
    """Drop Clip Observations older than 180 days. Unknown age is kept."""
    out: list[dict] = []
    for row in rows:
        age = row.get("age_days")
        if age is None:
            pub = row.get("published_at")
            if pub:
                try:
                    stamp = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - stamp).days
                except ValueError:
                    age = None
        if age is not None and float(age) > window_days:
            continue
        out.append(row)
    return out


def listener_holds_cloak(lock_path: Path | None = None) -> bool:
    """True when the match listener holds the CloakBrowser profile lock."""
    dest = lock_path or LISTENER_LOCK
    if not dest.is_file():
        return False
    try:
        import msvcrt
        with dest.open("a+b") as handle:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
    except OSError:
        return True


def _levels(rows: list[dict], key: str) -> list[str]:
    seen: list[str] = []
    have: set[str] = set()
    for row in rows:
        val = row.get(key)
        if not val:
            continue
        name = str(val)
        if name not in have:
            have.add(name)
            seen.append(name)
    return seen


def _recognised_steamids() -> set[str]:
    path = ROOT / ".data" / "player_accounts.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(row.get("steam_id") or "") for row in data if row.get("steam_id")}


def fit_partial_stars(
    rows: list[dict],
    *,
    l2: float = 8.0,
    window_days: int = WINDOW_DAYS,
    recognised: set[str] | None = None,
) -> dict:
    """Ridge on log(views). Source and Clip age are controls, not Candidate score."""
    import numpy as np

    usable: list[dict] = []
    for row in rows_in_window(rows, window_days=window_days):
        try:
            views = float(row.get("views"))
        except (TypeError, ValueError):
            continue
        if views <= 0:
            continue
        usable.append(row)
    empty = {
        "intercept": 0.0,
        "player": {},
        "opponent": {},
        "stage": {},
        "kind": {},
    }
    if len(usable) < 2:
        if usable:
            empty["intercept"] = float(math.log(float(usable[0]["views"])))
        return empty

    recognised = _recognised_steamids() if recognised is None else recognised
    players = [p for p in _levels(usable, "steamid") if p in recognised]
    opponents = _levels(usable, "opponent")
    stages = [s for s in STAGE_LEVELS if any(r.get("stage") == s for r in usable)]
    sources = _levels(usable, "source")
    kinds = [k for k in KIND_LEVELS if any(k in (r.get("kinds") or []) for r in usable)]

    cols = ["intercept"]
    cols.extend(f"player:{p}" for p in players)
    cols.extend(f"opponent:{o}" for o in opponents)
    cols.extend(f"stage:{s}" for s in stages)
    cols.extend(f"kind:{k}" for k in kinds)
    cols.extend(f"source:{s}" for s in sources)
    cols.append("clip_age")

    x = np.zeros((len(usable), len(cols)), dtype=float)
    y = np.zeros(len(usable), dtype=float)
    for i, row in enumerate(usable):
        y[i] = math.log(float(row["views"]))
        x[i, 0] = 1.0
        sid = str(row.get("steamid") or "")
        if sid in players:
            x[i, cols.index(f"player:{sid}")] = 1.0
        opp = row.get("opponent")
        if opp:
            key = f"opponent:{opp}"
            if key in cols:
                x[i, cols.index(key)] = 1.0
        stage = row.get("stage")
        if stage:
            key = f"stage:{stage}"
            if key in cols:
                x[i, cols.index(key)] = 1.0
        for kind in row.get("kinds") or []:
            key = f"kind:{kind}"
            if key in cols:
                x[i, cols.index(key)] = 1.0
        src = row.get("source")
        if src:
            key = f"source:{src}"
            if key in cols:
                x[i, cols.index(key)] = 1.0
        age = row.get("age_days")
        try:
            x[i, cols.index("clip_age")] = float(age or 0)
        except (TypeError, ValueError):
            pass

    penalty = np.eye(len(cols)) * l2
    penalty[0, 0] = 0.0
    xtx = x.T @ x + penalty
    try:
        beta = np.linalg.solve(xtx, x.T @ y)
    except np.linalg.LinAlgError:
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)

    out = {
        "intercept": float(beta[0]),
        "player": {p: float(beta[cols.index(f"player:{p}")]) for p in players},
        "opponent": {o: float(beta[cols.index(f"opponent:{o}")]) for o in opponents},
        "stage": {s: float(beta[cols.index(f"stage:{s}")]) for s in stages},
        "kind": {k: float(beta[cols.index(f"kind:{k}")]) for k in kinds},
    }
    return out


def refresh_youtube_views(rows: list[dict], views_by_id: dict[str, int]) -> list[dict]:
    """Update YouTube Clip Observation views from stored video ids (not an HLTV recrawl)."""
    out: list[dict] = []
    for row in rows:
        vid = str(row.get("clip_id") or row.get("video_id") or "")
        if vid and vid in views_by_id:
            out.append({**row, "views": int(views_by_id[vid])})
        else:
            out.append(row)
    return out


def youtube_ids_from_rows(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if str(row.get("source") or "") == "allstar":
            continue
        vid = str(row.get("clip_id") or row.get("video_id") or "")
        if _YT_ID.fullmatch(vid) and vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def fetch_youtube_view_counts(
    video_ids: list[str],
    *,
    api_key: str | None = None,
) -> dict[str, int]:
    """Data API statistics on stored video ids. Empty if no key."""
    if not video_ids:
        return {}
    key = api_key
    if key is None:
        try:
            from config import settings
            key = settings.youtube_api_key
        except Exception:
            key = ""
    if not key:
        return {}
    import httpx

    out: dict[str, int] = {}
    uniq = list(dict.fromkeys(video_ids))
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(uniq), 50):
            batch = uniq[i:i + 50]
            resp = client.get(
                YOUTUBE_VIDEO_URL,
                params={"part": "statistics", "id": ",".join(batch), "key": key},
            )
            resp.raise_for_status()
            for item in resp.json().get("items") or []:
                vid = str(item.get("id") or "")
                try:
                    out[vid] = int((item.get("statistics") or {}).get("viewCount") or 0)
                except (TypeError, ValueError):
                    continue
    return out


def load_demo_kind_stamps(path: Path | None = None) -> dict[str, list[str]]:
    dest = path or DEMO_KIND_STAMPS
    if not dest.is_file():
        return {}
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    stamps = data.get("stamps") if isinstance(data, dict) else None
    if not isinstance(stamps, dict):
        return {}
    return {
        str(key): [str(k) for k in val]
        for key, val in stamps.items()
        if isinstance(val, list)
    }


def apply_demo_kind_stamps(
    rows: list[dict],
    stamps: dict[str, list[str]] | None,
) -> list[dict]:
    if not stamps:
        return rows
    from shorts.clip_observation import merge_label_and_demo_kinds

    out: list[dict] = []
    for row in rows:
        extra = stamps.get(str(row.get("clip_id") or ""))
        if extra:
            merged = merge_label_and_demo_kinds(row.get("kinds") or (), extra)
            out.append({**row, "kinds": merged})
        else:
            out.append(row)
    return out


def observations_from_to_jsonl(path: Path | None = None) -> list[dict]:
    dest = path or TO_JSONL
    if not dest.is_file():
        return []
    out: list[dict] = []
    for line in dest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("source") and row.get("source") != "allstar":
            out.append(row)
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def observations_from_allstar_jsonl(path: Path | None = None) -> list[dict]:
    from shorts.clip_observation import observations_from_match_row, parse_stage

    dest = path or ALLSTAR_JSONL
    if not dest.is_file():
        return []
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
        clips = row.get("clips") or []
        if clips and isinstance(clips[0], dict) and "kinds" in clips[0] and "source" in clips[0]:
            stage = parse_stage(row.get("match_stage") or row.get("stage"))
            for c in clips:
                if not isinstance(c, dict):
                    continue
                out.append({**c, "stage": stage} if stage else c)
            continue
        out.extend(observations_from_match_row(row))
    if dest.resolve() == ALLSTAR_JSONL.resolve():
        out = apply_demo_kind_stamps(out, load_demo_kind_stamps())
    return out


def refresh_partial_stars(
    *,
    jsonl: Path | None = None,
    out_path: Path | None = None,
    to_jsonl: Path | None = None,
    views_by_id: dict[str, int] | None = None,
    fetch_views: bool = False,
) -> dict:
    """Refit Partial stars from stored Clip Observations. Does not wipe the store."""
    allstar = observations_from_allstar_jsonl(jsonl)
    to_path = to_jsonl or TO_JSONL
    to_rows = observations_from_to_jsonl(to_path)
    rows = allstar + to_rows
    fetched = views_by_id
    if fetch_views and fetched is None:
        try:
            fetched = fetch_youtube_view_counts(youtube_ids_from_rows(rows))
        except Exception:
            fetched = None
    if fetched:
        rows = refresh_youtube_views(rows, fetched)
        if to_rows:
            _write_jsonl(to_path, refresh_youtube_views(to_rows, fetched))
    stars = fit_partial_stars(rows)
    dest = out_path or STARS_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(stars, indent=2) + "\n", encoding="utf-8")
    stars["_rows"] = len(rows)
    return stars


def harvest_allstar(*, max_matches: int = DAILY_NEW_MATCHES) -> int:
    """Open at most 10–20 unseen Popular-event match pages. Skip if listener holds Cloak."""
    if listener_holds_cloak():
        return 0
    import subprocess
    import sys
    return subprocess.call(
        [
            sys.executable,
            str(ROOT / "scripts" / "shorts" / "scrape_allstar_hltv.py"),
            "--max-matches",
            str(max_matches),
        ]
    )
