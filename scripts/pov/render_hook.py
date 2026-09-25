"""Render a Hook Timeline's planned windows via CSDM with the no-spoiler HUD.

Reads ``hook_timeline.json`` (from ``build_hook_timeline.py``), plans the
kill-anchored sub-clips (``hook_plan.py``), then renders one CSDM sequence per
window at the player's capture resolution.

HUD policy (the whole point of this product): ``cl_draw_only_deathnotices 1``
plus CSDM's ``showOnlyDeathNotices`` — scoreboard, round timer, money and team
scores are hidden, the killfeed stays. Same as the Shorts format, so a hook
leaks no result information. The player's own crosshair is restored and their
in-HUD name is rewritten to the canonical nickname (``mirv_replace_name``).

Segments stay at the capture resolution; ``assemble_hook.py`` scales them to
2560x1440@60 to match the POV timeline (so the prepend is a stream copy).

Usage:
    python scripts/pov/render_hook.py renders/hook-<stem>_<player>/hook_timeline.json
    python scripts/pov/render_hook.py <timeline.json> --width 1280 --height 960
    python scripts/pov/render_hook.py <timeline.json> --force

Output:
    renders/hook-{stem}_{player}/segments/<n>-sequence/video.mp4
    renders/hook-{stem}_{player}/hook_render.json   (ordered window -> moment map)

Steam must be running (CSDM/HLAE requirement).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from pov.hook_plan import plan_hook  # noqa: E402
from faceit.intro_prepend import (  # noqa: E402
    AUTOEXEC_RENDER,
    _restore_autoexec,
    _swap_autoexec,
)
from csdm_segments import sequence, tick_range_config  # noqa: E402
from shorts.render_shorts import (  # noqa: E402
    _ffmpeg_settings,
    _find_sequence_files,
    _get_player_crosshair_cvars,
    _resolve_player_resolution,
    _run_csdm_hook_aware,
)

# Shorts-format HUD: killfeed only. Kept in the cfg as well as in the CSDM
# sequence envelope (showOnlyDeathNotices) — belt and braces, same as
# render_shorts._build_csdm_config.
HOOK_CFG_LINES = [
    "cl_draw_only_deathnotices 1",
    "crosshair 1",
    "cl_chatfilters 63",
    "snd_mvp_volume 0",
    "cl_showfps 0",
    "net_graph 0",
]

MIN_SEGMENT_BYTES = 1_048_576


def build_config(plan: list[dict], demo_path: Path, out_dir: Path,
                 width: int, height: int, framerate: int,
                 use_cpu: bool = False) -> dict:
    crosshair_cache: dict[str, list[str]] = {}
    sequences = []
    n = 1
    for moment in plan:
        sid = moment["pov_steam_id"]
        if sid not in crosshair_cache:
            nick = (moment.get("pov_nick") or "").strip()
            crosshair_cache[sid] = _get_player_crosshair_cvars(sid, demo_path, nick) or []
        cfg_lines = list(HOOK_CFG_LINES) + crosshair_cache[sid]
        nick = (moment.get("pov_nick") or "").strip()
        if nick and nick.lower() != "unknown":
            cfg_lines.append(f'mirv_replace_name byXuid add x{sid} "{nick}"')
        for w in moment["windows"]:
            sequences.append(sequence(
                n, int(w["start_tick"]), int(w["end_tick"]), sid,
                "\n".join(cfg_lines) + "\n",
                show_only_death_notices=True,
                player_voices=False,
            ))
            n += 1
    return tick_range_config(
        demo_path, out_dir, sequences,
        width=width, height=height, framerate=framerate,
        ffmpeg_settings=_ffmpeg_settings(use_cpu=use_cpu),
    )


def render_hook(timeline_path: Path, width: int | None = None,
                height: int | None = None, framerate: int = 60,
                max_seconds: float = 30.0, force: bool = False,
                hook_timeout: float = 150.0, hook_retries: int = 2,
                use_cpu: bool = False) -> Path:
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    moments = timeline.get("picked") or []
    if not moments:
        raise ValueError("hook_timeline.json has no picked moments")

    demo_path = Path(timeline["demo_path"])
    if not demo_path.is_file():
        raise FileNotFoundError(f"Demo not found: {demo_path}")

    tickrate = int(timeline.get("tickrate") or 64)
    out_dir = timeline_path.resolve().parent
    segments_dir = out_dir / "segments"

    sid = moments[0]["pov_steam_id"]
    if width is None or height is None:
        # Same source render_pov.py uses for the POV capture resolution, so the
        # hook framing matches the footage it is prepended to.
        width, height, _scaling = _resolve_player_resolution(sid)

    plan = plan_hook(moments, tickrate=tickrate, max_seconds=max_seconds)
    expected = sum(len(m["windows"]) for m in plan)
    total_s = sum((w["end_tick"] - w["start_tick"]) / tickrate
                  for m in plan for w in m["windows"])

    print(f"Hook: {len(plan)} moment(s), {expected} clip(s), "
          f"{total_s:.1f}s footage, map={timeline.get('map', '?')}, "
          f"player={timeline.get('player', {}).get('nick', '?')}")
    for m in plan:
        wins = ", ".join(f"{w['start_tick']}-{w['end_tick']}" for w in m["windows"])
        print(f"  {m['label']:22s} r{m.get('round')}  {len(m['windows'])} clip(s): {wins}")
    print(f"  demo: {demo_path}")
    print(f"  capture: {width}x{height} @ {framerate}fps (HUD: killfeed only)")
    print(f"  out: {out_dir}")

    segs = _find_sequence_files(segments_dir, expected) if segments_dir.is_dir() else []
    have = len(segs) == expected and all(
        p.is_file() and p.stat().st_size >= MIN_SEGMENT_BYTES for p in segs)
    if force or not have:
        segments_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = out_dir / "hook_csdm.json"
        cfg_path.write_text(
            json.dumps(build_config(plan, demo_path, segments_dir, width, height,
                                    framerate, use_cpu=use_cpu), indent=2),
            encoding="utf-8")
        # CS2 reads autoexec.cfg at launch. The POV footage was recorded under
        # autoexec_render.cfg (this run's pro crosshair/viewmodel + the HLAE
        # spec-lock that keeps the camera on the POV player after death), so the
        # hook must render under the same file or the framing/camera mismatch.
        # The spec-lock matters most on clutch_attempt moments, which usually
        # end with the POV player dead.
        _swap_autoexec(AUTOEXEC_RENDER)
        try:
            _run_csdm_hook_aware(cfg_path, segments_dir, "hook",
                                 hook_timeout=hook_timeout, hook_retries=hook_retries)
        finally:
            _restore_autoexec()
        segs = _find_sequence_files(segments_dir, expected)
        if len(segs) < expected:
            print(f"[ERROR] expected {expected} segment(s), found {len(segs)}",
                  file=sys.stderr)
    else:
        print("  [resume] all segments present, skipping CSDM")

    clips = []
    flat = [(m, w) for m in plan for w in m["windows"]]
    for (moment, window), seg in zip(flat, segs):
        clips.append({
            "segment": str(seg),
            "start_tick": int(window["start_tick"]),
            "end_tick": int(window["end_tick"]),
            "moment_label": moment["label"],
            "moment_tier": moment["tier"],
            "round": moment.get("round"),
            "pov_steam_id": moment["pov_steam_id"],
            "pov_nick": moment.get("pov_nick"),
        })
    payload = {
        "hook_type": "hook_render",
        "demo_path": str(demo_path),
        "map": timeline.get("map"),
        "player": timeline.get("player"),
        "tickrate": tickrate,
        "capture": {"width": width, "height": height, "fps": framerate},
        "clips": clips,
    }
    (out_dir / "hook_render.json").write_text(json.dumps(payload, indent=2),
                                              encoding="utf-8")
    print(f"  [OK] {len(clips)} clip(s) -> {out_dir / 'hook_render.json'}")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Render hook_timeline.json windows via CSDM")
    ap.add_argument("timeline", type=Path, help="hook_timeline.json")
    ap.add_argument("--width", type=int, default=None,
                    help="Capture width (default: player capture_width)")
    ap.add_argument("--height", type=int, default=None,
                    help="Capture height (default: player capture_height)")
    ap.add_argument("--framerate", type=int, default=60)
    ap.add_argument("--max-seconds", type=float, default=30.0,
                    help="Total footage budget (default: 30) — drops the weakest "
                         "moment(s) to fit")
    ap.add_argument("--hook-timeout", type=float, default=150.0,
                    help="Seconds to wait for the HLAE hook before retrying (default: 150)")
    ap.add_argument("--hook-retries", type=int, default=2)
    ap.add_argument("--force", action="store_true", help="Re-render existing segments")
    ap.add_argument("--cpu", action="store_true",
                    help="Encode segments with libx264 instead of h264_nvenc")
    args = ap.parse_args()

    if not args.timeline.is_file():
        print(f"[ERR] timeline not found: {args.timeline}", file=sys.stderr)
        return 1
    render_hook(args.timeline, width=args.width, height=args.height,
                framerate=args.framerate, max_seconds=args.max_seconds,
                force=args.force, hook_timeout=args.hook_timeout,
                hook_retries=args.hook_retries, use_cpu=args.cpu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
