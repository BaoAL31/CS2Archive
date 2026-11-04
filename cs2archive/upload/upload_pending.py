"""
Scan youtube/*/upload_meta.json and upload any that are still pending.

The pipeline (cs2archive/pov/pipeline.py) only produces the finished video,
thumbnail, and upload_meta.json (with youtube_id=None, upload_status="pending").
This script does the actual uploading: for every upload_meta.json whose
upload_status != "completed", it invokes cs2archive/upload/upload_youtube.py --meta <path>,
which performs the upload, sets the thumbnail, and writes youtube_id +
upload_status="completed" back into the same meta file.

Because the pipeline writes one independent upload_meta.json per variant
(raw -> youtube/{run_id}/, overlay -> youtube/{run_id}_overlay/), this script
naturally handles dual-upload and overlay-only variants with no special flags.

Resume-safe: a completed meta (youtube_id set, upload_status="completed") is
skipped, so re-running only uploads what's left. upload_youtube.py also stores
resumable_* fields in the meta file for crash recovery within a single upload.

With --also-bilibili, after YouTube (or when YouTube is already done) this also
runs the bilibili.tv upload via upload_youtube.py --also-bilibili /
--bilibili-only. Bilibili status is stored separately in the same meta
(bilibili_aid / bilibili_upload_status).

Usage:
    python cs2archive/upload/upload_pending.py                 # upload every pending meta
    python cs2archive/upload/upload_pending.py --dry-run       # list what would upload
    python cs2archive/upload/upload_pending.py --limit 1       # upload at most one
    python cs2archive/upload/upload_pending.py --dir youtube/my-match   # restrict scope
    python cs2archive/upload/upload_pending.py --also-bilibili # YouTube + bilibili.tv
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

from cs2archive.upload.upload_bilibili import is_bilibili_pending  # noqa: E402
from cs2archive.upload.youtube_schedule import DEFAULT_PUBLISH_TZ, resolve_auto_publish_schedule  # noqa: E402

PY = sys.executable
UPLOAD_YOUTUBE = PROJECT_ROOT / "cs2archive" / "upload" / "upload_youtube.py"
DEFAULT_YOUTUBE_DIR = PROJECT_ROOT / "youtube"


class UploadLocked(RuntimeError):
    """Another upload_pending.py run already owns the upload lock."""


class UploadLock:
    """Cross-process lock so two uploaders cannot publish the same video.

    ``upload_pending.py`` had NO single-instance lock — the listener documented
    that gap and worked around it with a FACEIT-only spawn ledger, which left
    the HLTV path open. Combined with the step-6 meta rewrite (which cleared
    ``youtube_id``) that is how sh1ro Anubis was uploaded twice on 2026-10-25:
    a second invocation had nothing to detect the first one with.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="ascii")
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise UploadLocked(
                f"another upload_pending.py run is already uploading "
                f"(lock {self.path})") from exc
        return self

    def __exit__(self, *_):
        import msvcrt
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


class UploadRefused(RuntimeError):
    """``upload_youtube.py`` refused the upload on purpose (exit code 2).

    Today the only refusal is the channel-side duplicate guard: a video with
    this exact title already exists. Retrying cannot fix that — it would just
    create the duplicate the guard exists to stop — so ``_upload_all`` reports
    it and moves on instead of burning its 3 attempts.
    """


def _is_youtube_pending(meta: dict) -> bool:
    return not (meta.get("upload_status") == "completed" and meta.get("youtube_id"))


#: How long an unfinished resumable session counts as "live".
#:
#: ``upload_youtube.py`` stamps ``upload_progress_at`` on every chunk it
#: persists, so a session whose stamp is recent belongs to a running uploader.
#: Past the TTL the uploader is dead and the session is safe to pick up again
#: (resuming it re-sends to the SAME YouTube video, so a retry is not a
#: duplicate). The window is well above the ~32 MB chunk cadence on a slow
#: link and well below a GTA-sized video's total upload time.
IN_FLIGHT_TTL_SECONDS = 20 * 60


def _parse_stamp(value: object) -> float | None:
    """Epoch seconds from an ISO-8601 stamp, or None when unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def session_in_flight(meta: dict, *, now: float | None = None,
                      ttl_seconds: int = IN_FLIGHT_TTL_SECONDS) -> bool:
    """True when this meta carries a LIVE resumable upload session.

    The session state lives in ``upload_meta.json`` (``resumable_uri`` +
    ``upload_progress_at``). Older metas that only recorded ``resumable_uri``
    with no stamp are treated as NOT live: refusing forever on a session whose
    uploader was killed would park the video with no way to resume it.
    """
    if not meta.get("resumable_uri"):
        return False
    stamp = (_parse_stamp(meta.get("upload_progress_at"))
             or _parse_stamp(meta.get("upload_started_at")))
    if stamp is None:
        return False
    if now is None:
        now = time.time()
    return (now - stamp) < ttl_seconds


def upload_in_flight(meta_path: Path, meta: dict) -> bool:
    """True when an upload session for this meta's video is still open.

    Two kinds of evidence, because the session marker moved: live sessions are
    recorded inside ``upload_meta.json`` (see ``session_in_flight``), while very
    old runs wrote a sibling ``<video>.upload_session.json`` and removed it on
    completion. Nothing writes that sibling file any more, so on its own it was
    a guard that could never fire — which is how the 2026-11-01 YEKINDAR Dust2
    upload was started a second time after its first uploader was killed
    mid-insert (an interrupted ``videos.insert`` leaves a YouTube video object
    but no local receipt).
    """
    if session_in_flight(meta):
        return True
    video = meta.get("video_path")
    if not video:
        return False
    return (Path(str(video))).with_name(
        Path(str(video)).name + ".upload_session.json").is_file()


def _needs_upload(meta: dict, also_bilibili: bool) -> bool:
    if _is_youtube_pending(meta):
        return True
    if also_bilibili and is_bilibili_pending(meta):
        return True
    return False


def find_pending(youtube_dir: Path, also_bilibili: bool = False) -> list[Path]:
    """Return paths of upload_meta.json files that still need uploading."""
    pending: list[Path] = []
    for meta_path in sorted(youtube_dir.rglob("upload_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not parse {meta_path}: {e}")
            continue
        if not _needs_upload(meta, also_bilibili):
            continue
        if upload_in_flight(meta_path, meta):
            # Someone is uploading this video right now: starting a second
            # session would publish it twice (the 2026-10-25 sh1ro Anubis
            # duplicate). Leave it pending — the live uploader owns it.
            print(f"  [skip] upload already in flight for {meta_path.parent.name}"
                  f" (live resumable session) — not starting a second one")
            continue
        video = meta.get("video_path")
        if not video or not Path(video).exists():
            print(f"  [skip] missing video for {meta_path.parent.name}: {video}")
            continue
        pending.append(meta_path)
    return pending


def _run_id_for_meta_dir(meta_dir: Path) -> str:
    """youtube/{run_id}[_overlay]/ -> {run_id} (matches .pipeline/{run_id}.json)."""
    name = meta_dir.name
    if name.endswith("_overlay"):
        name = name[: -len("_overlay")]
    return name


def _sibling_metas_pending(run_id: str, youtube_root: Path,
                           also_bilibili: bool) -> list[str]:
    """Pending meta dirs (raw + overlay variants) sharing this run_id."""
    pending: list[str] = []
    for cand in (youtube_root / run_id, youtube_root / f"{run_id}_overlay"):
        meta_path = cand / "upload_meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _needs_upload(meta, also_bilibili):
            pending.append(cand.name)
    return pending


def maybe_purge_renders(meta_path: Path, also_bilibili: bool,
                        dry_run: bool = False) -> None:
    """Delete renders/pov-* once EVERY variant of the run is uploaded.

    Render intermediates are the only repair path (re-overlay, re-scale),
    so purge only when neither raw nor overlay variant still needs uploading.
    The render dir comes from .pipeline/{run_id}.json (written by pipeline.py).
    Shorts survive the purge (separate upload flow) — see paths.purge_pov_dir.
    """
    meta_dir = meta_path.parent
    # youtube_root: parent of the variant dir, unless --dir pointed deeper.
    youtube_root = meta_dir.parent
    run_id = _run_id_for_meta_dir(meta_dir)
    still = _sibling_metas_pending(run_id, youtube_root, also_bilibili)
    if still:
        print(f"  [keep-renders] {run_id}: still pending: {', '.join(still)}")
        return
    state_path = PROJECT_ROOT / ".pipeline" / f"{run_id}.json"
    render_dir = None
    if state_path.is_file():
        try:
            render_dir = (json.loads(state_path.read_text(encoding="utf-8"))
                            .get("data", {}).get("render_dir"))
        except Exception:
            render_dir = None
    if not render_dir:
        return
    rd = Path(render_dir)
    if not rd.is_absolute():
        rd = PROJECT_ROOT / rd
    if not rd.exists():
        return
    if dry_run:
        print(f"  [dry-run] would purge renders: {rd.name}")
        return
    try:
        from cs2archive.paths import purge_pov_dir
        freed_gb = purge_pov_dir(rd)
        print(f"  [purge-renders] removed {rd.name}/ "
              f"({freed_gb:.1f} GB) — all variants uploaded (shorts spared)")
    except OSError as e:
        print(f"  [WARN] render purge failed (non-fatal): {e}")


def upload_one(meta_path: Path, meta: dict, dry_run: bool, also_bilibili: bool) -> bool:
    """Upload a single pending meta. Returns True on success.

    Success means upload_youtube.py exited 0 AND the expected platform IDs
    are present in the meta file afterward.
    """
    video = Path(meta["video_path"])
    thumb = meta.get("thumbnail_path")
    privacy = meta.get("privacy", "private")
    yt_pending = _is_youtube_pending(meta)
    bili_pending = also_bilibili and is_bilibili_pending(meta)

    if dry_run:
        parts = []
        if yt_pending:
            parts.append("youtube")
        if bili_pending:
            parts.append("bilibili")
        print(
            f"  [dry-run] would upload ({'+'.join(parts) or 'nothing'}): {video} "
            f"(privacy={privacy}, thumbnail={'yes' if thumb else 'no'})"
        )
        return False

    if yt_pending:
        cmd = [
            PY, str(UPLOAD_YOUTUBE),
            str(video),
            "--meta", str(meta_path),
            "--privacy", privacy,
        ]
        if thumb and Path(thumb).exists():
            cmd += ["--thumbnail", str(thumb)]
        if also_bilibili:
            cmd.append("--also-bilibili")

        print(f"  Uploading: {video}")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
        if r.returncode == 2:
            raise UploadRefused(
                f"uploader refused {meta_path.parent.name}: a video with this "
                f"title already exists on the channel (delete it, or upload "
                f"with --allow-duplicate)")
        if r.returncode != 0:
            print(f"  [FAIL] upload exited {r.returncode}: {video}")
            return False
    elif bili_pending:
        cmd = [
            PY, str(UPLOAD_YOUTUBE),
            str(video),
            "--meta", str(meta_path),
            "--bilibili-only",
        ]
        if thumb and Path(thumb).exists():
            cmd += ["--thumbnail", str(thumb)]
        print(f"  Uploading bilibili only: {video}")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
        if r.returncode != 0:
            print(f"  [FAIL] bilibili upload exited {r.returncode}: {video}")
            return False
    else:
        return True

    try:
        updated = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        updated = {}

    ok = True
    vid = updated.get("youtube_id")
    if yt_pending or not also_bilibili:
        if vid:
            print(f"  [OK] Uploaded: https://youtu.be/{vid}")
        else:
            print(f"  [WARN] upload_youtube.py exited 0 but no youtube_id in {meta_path.name}")
            ok = False
    if also_bilibili:
        aid = updated.get("bilibili_aid")
        if aid and updated.get("bilibili_upload_status") == "completed":
            print(f"  [OK] Bilibili: aid={aid}")
        else:
            print(f"  [WARN] bilibili not completed in {meta_path.name}")
            ok = False
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload every pending youtube/*/upload_meta.json")
    parser.add_argument(
        "--dir", default=str(DEFAULT_YOUTUBE_DIR),
        help=f"Root dir to scan for upload_meta.json (default: {DEFAULT_YOUTUBE_DIR})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List pending uploads without actually uploading")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Upload at most N pending metas (default: 0 = all)")
    parser.add_argument(
        "--also-bilibili", action="store_true",
        help="Also upload to bilibili.tv (same title/schedule/tags; needs "
             ".bilibili_storage.json). Resume-safe via bilibili_aid in meta.")
    parser.add_argument(
        "--retries", type=int, default=3,
        help="Retry a failed upload (crash/non-zero exit) up to N times "
             "(default: 3). upload_youtube.py is resumable, so a relaunch picks "
             "up where the failed run left off.")
    parser.add_argument(
        "--retry-delay", type=float, default=10.0,
        help="Base delay in seconds before the first retry; delay grows linearly "
             "per attempt (default: 10).")
    parser.add_argument(
        "--purge-renders", dest="purge_renders", action="store_true",
        default=True,
        help="Delete renders/pov-* once every variant of the run is uploaded "
             "(default: on).")
    parser.add_argument(
        "--keep-renders", dest="purge_renders", action="store_false",
        help="Keep render intermediates after upload (repair path).")
    parser.add_argument(
        "--check-schedule", action="store_true",
        help="Show next available YouTube publish slot and exit")
    args = parser.parse_args()

    if args.check_schedule:
        from cs2archive.upload.upload_youtube import get_authenticated_service, get_youtube_publish_dates
        print("Querying YouTube schedule...")
        youtube = get_authenticated_service()
        occupied = get_youtube_publish_dates(youtube) or set()
        privacy, utc, tz, local = resolve_auto_publish_schedule(
            timezone=DEFAULT_PUBLISH_TZ, occupied_dates=occupied,
        )
        print(f"Next free slot:")
        print(f"  Local:  {local} {tz}")
        print(f"  UTC:    {utc}")
        print(f"  Status: {privacy}")
        if occupied:
            print(f"  Occupied dates: {len(occupied)}")
        return

    youtube_dir = Path(args.dir)
    if not youtube_dir.exists():
        print(f"[ERROR] directory not found: {youtube_dir}")
        sys.exit(1)

    # One uploader at a time for the whole process. The listener respawns this
    # script when a previous spawn looks dead; without the lock a respawn that
    # overlapped a live uploader published the same video twice (sh1ro Anubis,
    # 2026-10-25). A held lock is a hard stop, not a retry: a second uploader
    # has nothing useful to do while the first one is running.
    if args.dry_run:
        _upload_all(args, youtube_dir)
        return
    try:
        with UploadLock(PROJECT_ROOT / ".pipeline" / "upload.lock"):
            _upload_all(args, youtube_dir)
    except UploadLocked as exc:
        # Loud and terminal: the live uploader owns these videos. Exiting 0
        # here would hide the collision; exiting 1 makes it visible exactly
        # where the repo's retry policy expects it.
        print(f"[ERROR] {exc}")
        sys.exit(1)


def _upload_all(args, youtube_dir: Path) -> None:
    pending = find_pending(youtube_dir, also_bilibili=args.also_bilibili)
    print(f"Found {len(pending)} pending upload(s) under {youtube_dir}")
    if args.limit > 0:
        pending = pending[:args.limit]

    if not pending:
        print("Nothing to upload.")
        return

    ok = 0
    failed = 0
    retried = 0
    for meta_path in pending:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not re-read {meta_path}: {e}")
            failed += 1
            continue

        attempts = 1 + args.retries  # first try + N retries
        uploaded_ok = False
        refused = False
        for attempt in range(1, attempts + 1):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  [skip] could not re-read {meta_path}: {e}")
                break
            try:
                if upload_one(meta_path, meta, args.dry_run, args.also_bilibili):
                    uploaded_ok = True
                    break
            except UploadRefused as exc:
                print(f"  [REFUSED] {exc}", flush=True)
                refused = True
                break
            if args.dry_run:
                break
            if attempt < attempts:
                delay = args.retry_delay * attempt
                print(f"  [retry] {meta_path.parent.name}: try {attempt}/{args.retries} "
                      f"failed; retrying in {delay:.0f}s...", flush=True)
                time.sleep(delay)
                retried += 1

        if uploaded_ok:
            ok += 1
            if args.purge_renders and not args.dry_run:
                try:
                    maybe_purge_renders(meta_path, args.also_bilibili)
                except Exception as e:
                    print(f"  [WARN] post-upload purge failed (non-fatal): {e}")
        elif not args.dry_run:
            failed += 1
            if refused:
                # Terminal per repo policy: a refusal must be visible, never
                # silently skipped. Non-zero exit so the caller sees it.
                print(f"  [HARD FAIL] {meta_path.parent.name} needs a human "
                      f"decision (duplicate on channel)", flush=True)

    print(f"\nDone. uploaded={ok} failed={failed} retried={retried} "
          f"(dry_run={args.dry_run}, also_bilibili={args.also_bilibili})")
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
