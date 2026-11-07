"""Schedule an already-uploaded video (resumed uploads never get a publishAt).

`upload_pending.py` launches `upload_youtube.py` without `--publish-at`, so the
slot is only applied on a fresh insert. A resumed upload (the console died and
`--meta` resumed the resumable session) completes the video but leaves it
private with no `publishAt` — it would never go live. The decider POV
(fP_nH1Crv68, 2026-10-13) hit exactly that.

Usage: schedule_uploaded.py <video_id> [<meta_path>]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cs2archive.upload.upload_youtube import (  # noqa: E402
    get_authenticated_service,
    get_youtube_publish_dates,
)
from cs2archive.upload.youtube_schedule import (  # noqa: E402
    AUTO_PUBLISH_TIMES,
    resolve_auto_publish_schedule,
)


def main() -> int:
    video_id = sys.argv[1]
    meta_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    youtube = get_authenticated_service()
    occupied = get_youtube_publish_dates(youtube)
    privacy, publish_at_utc, tz, local = resolve_auto_publish_schedule(
        timezone=None, occupied_dates=occupied, publish_times=AUTO_PUBLISH_TIMES,
    )
    print(f"occupied dates: {sorted(occupied)}")
    print(f"chosen slot: {local} ({tz}) -> {publish_at_utc} UTC, privacy={privacy}")

    resp = youtube.videos().update(
        part="status",
        body={"id": video_id,
              "status": {"privacyStatus": privacy, "publishAt": publish_at_utc}},
    ).execute()
    status = resp.get("status", {})
    print(f"applied: id={resp.get('id')} privacy={status.get('privacyStatus')} "
          f"publishAt={status.get('publishAt')}")

    if meta_path and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["publish_at"] = local
        meta["publish_at_utc"] = publish_at_utc
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"meta updated: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
