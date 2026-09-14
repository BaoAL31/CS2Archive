"""Publish queued Instagram Reels when their YouTube slot arrives.

Instagram Graph has no native ``scheduled_publish_time``. The shorts uploader
registers a one-shot Windows Scheduled Task that runs this script at the same
wall-clock as YouTube/TikTok/Facebook.

Usage:
    python scripts/upload/publish_due_social.py --meta renders/.../upload_meta_shorts.json
    python scripts/upload/publish_due_social.py --dir renders   # any queued items already due
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import _pathsetup  # noqa: E402

_pathsetup.ensure()

SHORTS_META_NAME = "upload_meta_shorts.json"
QUEUED = "queued"
PUBLISHED = "published"


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def task_name_for(meta_path: Path) -> str:
    raw = f"CS2Archive-ig-{meta_path.parent.name}"
    return re.sub(r'[\\/:*?"<>|]', "-", raw)[:238]


def schedule_due_meta(meta_path: Path, run_at: datetime) -> str:
    """Register a one-shot task. Returns the task name. Raises on failure."""
    meta_path = meta_path.resolve()
    name = task_name_for(meta_path)
    run_local = run_at.astimezone()
    start = run_local.strftime("%Y-%m-%dT%H:%M:%S")
    end = (run_local + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    py = sys.executable
    # Paths in this repo have no spaces; extra quotes break PowerShell -Argument.
    argument = f"{Path(__file__).resolve()} --meta {meta_path}"
    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {json.dumps(py)} -Argument {json.dumps(argument)} -WorkingDirectory {json.dumps(str(_PROJECT_ROOT))}
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]{json.dumps(start)})
$trigger.EndBoundary = {json.dumps(end)}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)
Register-ScheduledTask -TaskName {json.dumps(name)} -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
"""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(ps)
        script = fh.name
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
        )
    finally:
        Path(script).unlink(missing_ok=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(err)
    return name


def _read_meta(meta_path: Path) -> dict:
    return json.loads(meta_path.read_text(encoding="utf-8-sig"))


def _write_meta(meta_path: Path, **fields) -> None:
    meta = _read_meta(meta_path)
    meta.update(fields)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def publish_queued_instagram(meta_path: Path, *, now: datetime | None = None) -> str:
    """Publish a queued Reel if the slot has arrived. Returns a status token."""
    meta = _read_meta(meta_path)
    status = meta.get("instagram_status")
    if status == PUBLISHED:
        return "already_published"
    if status != QUEUED:
        return f"skip_status_{status or 'missing'}"

    slot = parse_utc(meta.get("publish_at_utc"))
    now = now or datetime.now(timezone.utc)
    if slot is not None and now + timedelta(seconds=15) < slot:
        return "not_due"

    video = Path(meta.get("video_path") or "")
    if not video.is_file():
        fallback = meta_path.parent / "short.mp4"
        video = fallback if fallback.is_file() else video
    if not video.is_file():
        raise FileNotFoundError(f"queued Instagram video missing: {video}")

    from meta_graph import MetaGraph

    caption = meta.get("title") or ""
    result = MetaGraph().publish_ig_reel_file(video, caption=caption)
    ig_id = result.get("id")
    _write_meta(meta_path, instagram_status=PUBLISHED, instagram_id=ig_id)
    return f"published:{ig_id}"


def _iter_metas(root: Path) -> list[Path]:
    return sorted(root.rglob(SHORTS_META_NAME))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Publish queued Instagram Reels that are due")
    p.add_argument("--meta", type=Path, help="Single upload_meta_shorts.json")
    p.add_argument("--dir", type=Path, help="Scan a tree for queued metas")
    args = p.parse_args(argv)
    if bool(args.meta) == bool(args.dir):
        p.error("pass exactly one of --meta or --dir")

    paths = [args.meta] if args.meta else _iter_metas(args.dir)
    if args.meta and not args.meta.is_file():
        print(f"[ERROR] meta not found: {args.meta}", flush=True)
        return 1

    n_ok = 0
    n_skip = 0
    n_fail = 0
    for path in paths:
        try:
            result = publish_queued_instagram(path)
        except Exception as exc:
            print(f"  [FAIL] {path}: {type(exc).__name__}: {exc}", flush=True)
            n_fail += 1
            continue
        if result.startswith("published:"):
            print(f"  [OK] {path.parent.name} {result}", flush=True)
            n_ok += 1
        else:
            print(f"  [skip] {path.parent.name} {result}", flush=True)
            n_skip += 1
    print(f"due_social published={n_ok} skipped={n_skip} failed={n_fail}", flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
