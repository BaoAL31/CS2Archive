"""Sync Shorts foundation data to HuggingFace (resume-safe).

Local staging layout (under settings.shorts_foundation_dir):

    clips/<clip_id>/
        raw_allstar.json      # verbatim Allstar clip payload
        match.json            # match context (id, slug, stage, teams, retrieved_at)
        alignment.json        # demo identity, round, tick bounds, method, status
        events.parquet        # raw events (shots/hits/kills/utility/bomb)
        state.parquet         # tick-level player state snapshots
        los.parquet           # LOS/engagement measurements (optional, v1 may omit)

Each clip folder uploads independently; uploaded clip ids are recorded in
.sync_hf_state.json so reruns only push new/changed clips. Nothing is deleted
locally after upload (unlike the demo uploader).

Usage:
    python -m cs2archive.hf.sync_foundation_to_hf [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import HfApi

STATE_FILE = Path("cs2archive/hf/.sync_foundation_state.json")


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _clip_hash(clip_dir: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(clip_dir.rglob("*")):
        if path.is_file() and path.name != ".synced":
            h.update(path.name.encode())
            h.update(str(path.stat().st_size).encode())
            h.update(str(int(path.stat().st_mtime)).encode())
    return h.hexdigest()


def main() -> int:
    from cs2archive.config import settings

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", action="store_true",
                    help="Upload all pending clips in a single commit (avoids per-clip rate limits)")
    ap.add_argument("--repo", default=settings.shorts_foundation_repo)
    ap.add_argument("--staging", type=Path, default=settings.shorts_foundation_dir)
    args = ap.parse_args()

    staging = Path(args.staging)
    if not staging.is_dir():
        print(f"[ERR] staging dir missing: {staging}")
        return 1

    api = HfApi()
    state = _load_state()
    clips = sorted(p for p in (staging / "clips").glob("*") if p.is_dir()) if (staging / "clips").is_dir() else []
    if not clips:
        print("No clips staged. Nothing to do.")
        return 0

    pending = [(c, _clip_hash(c)) for c in clips]
    todo = [(c, h) for c, h in pending if state.get(c.name) != h]
    print(f"{len(clips)} staged, {len(todo)} changed/new — repo {args.repo}")
    if args.dry_run:
        for clip, _ in todo:
            print(f"  would upload: clips/{clip.name}/")
        return 0

    if args.batch and todo and not args.dry_run:
        try:
            api.upload_folder(
                folder_path=str((staging / "clips").resolve()),
                path_in_repo="clips",
                repo_id=args.repo,
                repo_type="dataset",
            )
            for _clip, digest in todo:
                state[_clip.name] = digest
            _save_state(state)
            print(f"  [OK] batched {len(todo)} clips in one commit")
        except Exception as e:
            print(f"  [FAIL] batch: {e}")
            return 1
        print(f"Done. Uploaded {len(todo)}/{len(todo)}.")
        return 0

    failed = 0
    for clip, digest in todo:
        try:
            api.upload_folder(
                folder_path=str(clip.resolve()),
                path_in_repo=f"clips/{clip.name}",
                repo_id=args.repo,
                repo_type="dataset",
            )
            state[clip.name] = digest
            _save_state(state)
            print(f"  [OK] clips/{clip.name}/")
        except Exception as e:
            print(f"  [FAIL] clips/{clip.name}/: {e}")
            failed += 1
    print(f"Done. Uploaded {len(todo) - failed}/{len(todo)}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
