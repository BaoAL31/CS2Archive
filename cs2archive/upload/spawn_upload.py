"""Spawn the YouTube upload for a finished POV.

Both entry points share this: the listener (after each pipeline it runs, with its
own daily-slot ledger) and ``pov/pipeline.py`` (so a hand-run pipeline uploads
too instead of leaving an ``upload_status: pending`` meta behind). The upload
runs in its own console, fire-and-forget — ``upload_youtube.py`` is resumable, so
a killed console is recoverable by re-running ``upload_pending.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_PENDING = ROOT / "cs2archive" / "upload" / "upload_pending.py"


def child_env() -> dict[str, str]:
    """Give direct script launches the same import path as the repo launcher."""
    env = os.environ.copy()
    paths = [str(ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def is_pending(meta: dict) -> bool:
    """True when this meta still needs an upload (completed ones are skipped)."""
    if not meta:
        return False
    if meta.get("upload_status") == "completed" or meta.get("youtube_id"):
        return False
    if meta.get("upload_status") == "skipped":
        return False
    return True


def upload_cmd(meta_path: Path) -> list[str] | None:
    """``upload_pending.py --dir <meta dir> --limit 1``, or None when nothing to do."""
    try:
        data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    video = data.get("video_path")
    if not is_pending(data) or not video or not Path(video).exists():
        return None
    return [
        sys.executable, "-u",
        str(UPLOAD_PENDING),
        "--dir", str(Path(meta_path).parent),
        "--limit", "1",
    ]


def spawn_upload_terminal(cmd: list[str], *, dry_run: bool = False,
                          echo: bool = True) -> bool:
    """Fire-and-forget upload in a new console. Returns True when handed off."""
    if echo:
        print(f"[upload] {' '.join(cmd)}", flush=True)
    if dry_run:
        return False
    env = child_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict = {"cwd": str(ROOT), "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        print(f"[upload] spawn failed: {exc}", flush=True)
        return False
    return True


def start_upload(meta_path: Path, *, dry_run: bool = False) -> bool:
    """Spawn the upload for one ``upload_meta.json`` (no-op when not pending)."""
    cmd = upload_cmd(meta_path)
    if not cmd:
        return False
    return spawn_upload_terminal(cmd, dry_run=dry_run)
