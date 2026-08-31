"""FACEIT POV thumbnail via style-01 (html_examples.html #proof).

Kill-frame background + HLTV cutout + big K-D. Overlay badge is
INPUTS + UTIL CAMS. Same-team Recognised Pros (not opponents) get a side portrait when
their avatar is cached. ELO is not drawn — K/D is the proof line.

Usage:
    python scripts/faceit/faceit_thumbnail.py <demo_path> --player <nick> --map <map>
                                  [--video <mp4>] [--steam-id <id>] [--elo <int>]
                                  [--opp-elo <int>] [--variant raw|overlay]
                                  --output <youtube_dir>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()
sys.path.insert(0, str(PROJECT_ROOT / "thumbnail"))

from thumbnail.proof_01 import BADGE_DEFAULT, render as render_style_01  # noqa: E402
from thumbnail.utils import extract_killfeed_frame  # noqa: E402


def style01_sub(costar_nicks: list[str]) -> str:
    if len(costar_nicks) == 1:
        return f"w/ {costar_nicks[0]}"
    return ""


def _demo_players(demo_path: Path) -> list[dict]:
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))
    info = parser.parse_player_info()
    out = []
    for _, row in info.iterrows():
        sid = str(row.get("steamid", "")).strip()
        if not sid or sid.lower() == "nan":
            continue
        out.append({
            "name": str(row.get("name", "")).strip(),
            "steamid": sid,
            "team_number": int(row.get("team_number", 0)),
        })
    return out


def lobby_costars(
    demo: Path,
    steam_id: str,
    pov_nick: str,
) -> list[tuple[str, Path]]:
    """Same-team Recognised Pros with cached avatars (max one, for the duo layout)."""
    from faceit_names import avatar_path, canonical_nick, known_pro_steam_ids

    if not steam_id:
        return []
    try:
        players = _demo_players(demo)
    except Exception as e:
        print(f"  [WARN] demo player parse failed ({e}); no costars")
        return []

    by_steam = known_pro_steam_ids()
    pov_team = None
    for p in players:
        if p["steamid"] == steam_id:
            pov_team = p["team_number"]
            break
    if pov_team is None:
        return []
    teammates: list[tuple[str, Path]] = []
    seen: set[str] = {canonical_nick(pov_nick).lower()}
    for p in players:
        if p["steamid"] == steam_id or p["team_number"] != pov_team:
            continue
        nick = by_steam.get(p["steamid"])
        if not nick:
            continue
        key = nick.lower()
        if key in seen:
            continue
        av = avatar_path(nick)
        if av is None:
            continue
        seen.add(key)
        teammates.append((nick, av))
    teammates.sort(key=lambda x: x[0].lower())
    return teammates[:1]


def _mid_video_frame(video: Path) -> Path | None:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=60,
        )
        if probe.returncode != 0:
            return None
        duration = float(probe.stdout.strip())
        seek_t = max(0.5, duration * 0.40)
        fd, name = tempfile.mkstemp(prefix="thumb_mid_", suffix=".jpg")
        os.close(fd)
        tmp = Path(name)
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{seek_t:.3f}", "-i", str(video),
             "-frames:v", "1",
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
             str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            return None
        print(f"  [bg] mid-video frame @ {seek_t:.2f}s (no killfeed sidecar)")
        return tmp
    except Exception as e:
        print(f"  [WARN] mid-video frame failed: {e}")
        return None


def _resolve_bg(args, demo: Path) -> Path:
    bg: Path | None = None
    if args.background:
        bg = Path(args.background)
    elif args.video and args.steam_id:
        bg = extract_killfeed_frame(
            args.video, args.steam_id,
            demo_path=demo, sidecar_path=args.sidecar,
        )
    if (bg is None or not Path(bg).exists()) and args.video:
        bg = _mid_video_frame(Path(args.video))
    if bg is None or not Path(bg).exists():
        from PIL import Image
        bg = PROJECT_ROOT / "tmp" / "faceit_bg_fallback.png"
        bg.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), (20, 20, 24)).save(bg)
        print("  [WARN] no video frame; using solid background")
    return Path(bg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path", help="FACEIT .dem path")
    ap.add_argument("--player", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--elo", type=int, default=None, help="POV player's FACEIT ELO")
    ap.add_argument("--opp-elo", type=int, default=None, help="Average FACEIT ELO of the opposing team")
    ap.add_argument("--kd", default=None, help="K/D line for the POV player, e.g. '38/9'")
    ap.add_argument("--background", default=None,
                    help="Reuse a cached background frame (jpg) instead of extracting from --video")
    ap.add_argument("--video", default=None,
                    help="Finished POV mp4 (overlay/raw) to grab the killfeed frame from")
    ap.add_argument("--sidecar", default=None,
                    help="round_offsets JSON (defaults to <video-stem>.round_offsets.json)")
    ap.add_argument("--variant", choices=["raw", "overlay"], default="raw")
    ap.add_argument("--output", required=True, help="youtube dir to write thumbnail.jpg")
    args = ap.parse_args()

    from faceit_names import avatar_path, canonical_nick

    player = canonical_nick(args.player)
    av_path = avatar_path(args.player)
    if av_path is None:
        sys.exit(f"[ERR] No avatar for {player} — style-01 needs a cutout PNG")

    demo = Path(args.demo_path)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = _resolve_bg(args, demo)

    pairs = lobby_costars(demo, args.steam_id, player)
    costar_nicks = [n for n, _ in pairs]
    right = pairs[0][1] if pairs else None
    score = (args.kd or "0-0").replace("/", "-")
    sub = style01_sub(costar_nicks)
    badge = BADGE_DEFAULT if args.variant == "overlay" else ""

    out = out_dir / "thumbnail.jpg"
    render_style_01(
        bg=bg,
        main_avatar=av_path,
        name=player,
        score=score,
        dest=out,
        sub=sub,
        costar_right=right,
        badge=badge,
    )
    print(f"[OK] FACEIT thumbnail (style-01): {out}")
    if costar_nicks:
        print(f"  costars: {', '.join(costar_nicks)}")


if __name__ == "__main__":
    main()
