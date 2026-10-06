"""Fix the stage line/tag on an already-uploaded POV (one-off repair).

Removes the scraped stage suffix from the description's tournament line and the
matching junk tag, then pushes title/description/tags to the live video and
rewrites the local upload_meta.json so it matches YouTube.

Usage:
    python tools/fix_stage_meta.py <overlay_dir> [--apply]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cs2archive.pov.generate_title import show_stage  # noqa: E402


def clean_description(description: str) -> str:
    """Drop a scraped ``- <stage>`` suffix from the standalone tournament line."""
    lines = description.split("\n")
    for i, line in enumerate(lines):
        if " - " not in line:
            continue
        tournament, stage = line.split(" - ", 1)
        # Keep the line only as ``tournament`` when the stage is not one of the
        # deep rounds that are worth publishing.
        if not show_stage(stage.strip()):
            lines[i] = tournament.strip()
    return "\n".join(lines)


def clean_tags(tags: list[str], video_title: str) -> list[str]:
    """Drop tags that are not worth publishing (early-round stage labels)."""
    kept = []
    for tag in tags:
        cleaned = re.sub(r"\s*\d+\.\s.*$", "", tag).strip()
        if show_stage(cleaned) or not re.search(
            r"round|group|swiss|playoff|quarter|stage \d|decider", cleaned, re.I
        ):
            kept.append(tag)
    return kept


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("overlay_dir")
    parser.add_argument("--apply", action="store_true",
                        help="push the cleaned snippet to YouTube (default: dry run)")
    args = parser.parse_args()

    meta_path = Path(args.overlay_dir) / "upload_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    video_id = meta["youtube_id"]

    new_desc = clean_description(meta["description"])
    new_tags = clean_tags(meta["tags"], meta["title"])

    print(f"video: {video_id}  {meta['title']}")
    print("--- description ---")
    for before, after in zip(meta["description"].split("\n"), new_desc.split("\n")):
        if before != after:
            print(f"  - {before!r}\n  + {after!r}")
    removed = [t for t in meta["tags"] if t not in new_tags]
    print(f"--- tags: {len(meta['tags'])} -> {len(new_tags)} (removed {removed}) ---")

    if not args.apply:
        print("dry run — pass --apply to push")
        return

    from cs2archive.upload.upload_youtube import get_authenticated_service

    youtube = get_authenticated_service()
    current = youtube.videos().list(part="snippet", id=video_id).execute()["items"][0]
    snippet = current["snippet"]
    snippet.update({
        "title": meta["title"],
        "description": new_desc,
        "tags": new_tags,
        "categoryId": snippet.get("categoryId", "20"),
    })
    youtube.videos().update(part="snippet", body={
        "id": video_id, "snippet": snippet,
    }).execute()

    meta["description"] = new_desc
    meta["tags"] = new_tags
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[OK] updated {video_id} + {meta_path.name}")


if __name__ == "__main__":
    main()
