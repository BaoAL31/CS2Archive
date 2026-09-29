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
    python cs2archive/pov/render_hook.py renders/pov-<stem>_<nick>/hook/hook_timeline.json
    python cs2archive/pov/render_hook.py <timeline.json> --width 1280 --height 960
    python cs2archive/pov/render_hook.py <timeline.json> --force

Output:
    renders/pov-<stem>_<nick>/hook/segments/<n>-sequence/video.mp4
    renders/pov-<stem>_<nick>/hook/hook_render.json   (ordered window -> moment map)

Steam must be running (CSDM/HLAE requirement).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


from cs2archive.pov.hook_plan import planned_seconds, plan_hook  # noqa: E402
from cs2archive.crosshair_resolve import canonical_nick, resolve_crosshair_cvars  # noqa: E402
from cs2archive.crosshair_code import effective_crosshair_height  # noqa: E402
from cs2archive.faceit.intro_prepend import (  # noqa: E402
    AUTOEXEC_RENDER,
    _restore_autoexec,
    _swap_autoexec,
)
from cs2archive.csdm_segments import sequence, tick_range_config  # noqa: E402
from cs2archive.shorts.render_shorts import (  # noqa: E402
    _ffmpeg_settings,
    _find_sequence_files,
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


def _pov_kill_ticks(demo_path: Path, sid: str) -> list[int]:
    """Every POV kill tick in the demo (attacker == sid, suicides excluded,
    knife round excluded — same convention as the FACEIT backlog K/D)."""
    try:
        import demoparser2 as dp
        parser = dp.DemoParser(str(demo_path))
        deaths = parser.parse_event("player_death")
        if deaths is None or len(deaths) == 0:
            return []
        first_real_tick = 0
        try:
            round_starts = parser.parse_event("round_start")
            if round_starts is not None and len(round_starts):
                r1 = round_starts[round_starts["round"] == 1]
                if len(r1):
                    first_real_tick = int(r1["tick"].max())
        except Exception:
            pass
        att = deaths["attacker_steamid"].astype(str)
        vic = deaths["user_steamid"].astype(str)
        core = deaths[(deaths["tick"] >= first_real_tick) & (att != vic)]
        ticks = core[core["attacker_steamid"].astype(str) == str(sid)]["tick"]
        return sorted({int(t) for t in ticks})
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] POV kill-tick parse failed ({e}) — no single-kill cap")
        return []


def _player_cvars(sid: str, demo_path: Path, nick: str,
                  height: int) -> tuple[list[str], dict]:
    """The POV render's exact cfg inputs for this player: prosettings
    crosshair (canonical nick, same pixel conversion height) + viewmodel."""
    from types import SimpleNamespace
    from cs2archive.pov.render_pov import CSDM, _viewmodel_cvars_from_args
    xhair_h = effective_crosshair_height(height)
    cvars, info = resolve_crosshair_cvars(
        nick, sid, demo_path, csdm_cmd=CSDM, screen_height=xhair_h)
    vm_cvars = _viewmodel_cvars_from_args(SimpleNamespace(player=nick))
    return list(cvars) + list(vm_cvars), {**info, "screen_height": xhair_h}


def build_config(plan: list[dict], demo_path: Path, out_dir: Path,
                 width: int, height: int, framerate: int,
                 use_cpu: bool = False, player_nick: str = "",
                 player_cvars: list[str] | None = None,
                 rename_map: dict | None = None) -> dict:
    crosshair_cache: dict[str, list[str]] = {}
    sequences = []
    n = 1
    for moment in plan:
        sid = moment["pov_steam_id"]
        if sid not in crosshair_cache:
            # Same cfg system as render_pov: canonical nick first (the timeline
            # may carry the demo's raw name, which misses prosettings), then
            # the moment nick, then player_accounts by steam_id.
            nick = (player_nick or "").strip() or (moment.get("pov_nick") or "").strip()
            nick = canonical_nick(sid, nick)
            if player_cvars is not None:
                crosshair_cache[sid] = list(player_cvars)
            else:
                cvars, _info = _player_cvars(sid, demo_path, nick, height)
                crosshair_cache[sid] = cvars
        # HUD policy first, player cvars last (last wins — same as the POV).
        cfg_lines = list(HOOK_CFG_LINES) + crosshair_cache[sid]
        if rename_map:
            from cs2archive.pov.render_pov import rename_cfg_lines
            cfg_lines += rename_cfg_lines(rename_map)
        nick = canonical_nick(sid, (moment.get("pov_nick") or "").strip()
                              or (player_nick or "").strip())
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
                max_seconds: float = 60.0, force: bool = False,
                hook_timeout: float = 150.0, hook_retries: int = 2,
                use_cpu: bool = False, player_nick: str = "",
                rename_map: dict | None = None,
                min_seconds: float = 10.0,
                hide_avatars: bool = False) -> Path | None:
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

    # Canonical nick (player_accounts by steam_id): the timeline may carry the
    # demo's raw name, which misses the prosettings lookup and drops the hook
    # onto the demo share-code fallback — a different crosshair from the POV.
    nick = (player_nick or "").strip() or (moments[0].get("pov_nick") or "").strip()
    nick = canonical_nick(sid, nick)
    cvars, xhair_info = _player_cvars(sid, demo_path, nick, height)
    print(f"  [crosshair] {xhair_info.get('source')} ({nick or sid}) "
          f"@ {xhair_info.get('screen_height')}p ({len(cvars)} cvars)")

    # Single-kill moments must not swallow the next POV kill (the payoff would
    # stretch into it and the cold open reads as a 2k). Parse once, gated.
    all_kills: list[int] | None = None
    if any(len(m.get("kill_ticks") or []) == 1 for m in moments):
        all_kills = _pov_kill_ticks(demo_path, sid)

    plan = plan_hook(moments, tickrate=tickrate, max_seconds=max_seconds,
                     all_kill_ticks=all_kills)
    expected = sum(len(m["windows"]) for m in plan)
    total_s = planned_seconds(plan, tickrate)

    if min_seconds > max_seconds:
        print(f"  [warn] --min-seconds ({min_seconds:g}) > --max-seconds "
              f"({max_seconds:g}) — clamping to the budget")
        min_seconds = max_seconds
    if total_s < min_seconds:
        # A 3s single-kill flash into the intro is disorienting — a hook must
        # be able to stand as a cold open or it doesn't ship (normal skip).
        # Stale artifacts from a previously qualifying plan are removed so a
        # later stage can never assemble them.
        print(f"  [skip] planned hook {total_s:.1f}s < minimum {min_seconds:g}s "
              f"— no hook for this POV")
        for stale in (out_dir / "hook_render.json", out_dir / "hook.mp4"):
            stale.unlink(missing_ok=True)
        return None

    print(f"Hook: {len(plan)} moment(s), {expected} clip(s), "
          f"{total_s:.1f}s footage, map={timeline.get('map', '?')}, "
          f"player={nick or sid}")
    for m in plan:
        wins = ", ".join(f"{w['start_tick']}-{w['end_tick']}" for w in m["windows"])
        print(f"  {m['label']:22s} r{m.get('round')}  {len(m['windows'])} clip(s): {wins}")
    print(f"  demo: {demo_path}")
    print(f"  capture: {width}x{height} @ {framerate}fps (HUD: killfeed only)")
    print(f"  out: {out_dir}")

    cfg = build_config(plan, demo_path, segments_dir, width, height,
                       framerate, use_cpu=use_cpu, player_nick=nick,
                       player_cvars=cvars, rename_map=rename_map)
    cfg_path = out_dir / "hook_csdm.json"
    render_json = out_dir / "hook_render.json"
    segs = _find_sequence_files(segments_dir, expected) if segments_dir.is_dir() else []
    have = len(segs) == expected and all(
        p.is_file() and p.stat().st_size >= MIN_SEGMENT_BYTES for p in segs)
    # Stale-cache guard: same clip count is not enough — a changed plan
    # (tighter single-kill window), changed cfg (canonical crosshair), or a
    # changed avatar policy must re-render instead of silently reusing stale
    # pixels (avatar cvars live only in the game autoexec, not the cfg).
    fresh = False
    if have:
        try:
            old_cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else None
            old_render = json.loads(render_json.read_text(encoding="utf-8")) if render_json.is_file() else None
            old_wins = [(c.get("start_tick"), c.get("end_tick"))
                        for c in (old_render.get("clips") or [])] if old_render else None
            old_hide = (old_render.get("hide_avatars") if old_render else None)
            new_wins = [(w["start_tick"], w["end_tick"]) for m in plan for w in m["windows"]]
            fresh = (old_cfg == cfg) and (old_wins == new_wins) and (old_hide == bool(hide_avatars))
            if not fresh:
                print("  [stale] hook plan/cfg/avatar-policy changed — re-rendering segments")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] could not validate cached hook render ({e}) — re-rendering")
    if force or not (have and fresh):
        if segments_dir.is_dir():
            for stale in list(segments_dir.glob("*.mp4")):
                try:
                    stale.unlink()
                except OSError:
                    pass
        segments_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        # Write the game's render autoexec ourselves (crosshair + viewmodel +
        # rename + spec-lock, exactly like the POV render) instead of relying
        # on the leftover file from a previous render.
        from cs2archive.pov.render_pov import (_demo_player_name, _ensure_spec_lock_snippet,
                                _write_render_autoexec, _write_spec_lock_cfg)
        _ensure_spec_lock_snippet()
        demo_name = _demo_player_name(sid, [str(demo_path)])
        _write_render_autoexec(cvars, rename_map, demo_name,
                               hide_avatars=bool(hide_avatars))
        _write_spec_lock_cfg(demo_name)
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
        "hide_avatars": bool(hide_avatars),
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
    ap.add_argument("--max-seconds", type=float, default=60.0,
                    help="Total footage budget (default: 60) — drops the weakest "
                         "moment(s) to fit")
    ap.add_argument("--min-seconds", type=float, default=10.0,
                    help="Minimum assembled hook length (default: 10) — a shorter "
                         "plan ships no hook at all (backstop; the builder "
                         "already fills to this)")
    ap.add_argument("--hook-timeout", type=float, default=150.0,
                    help="Seconds to wait for the HLAE hook before retrying (default: 150)")
    ap.add_argument("--hook-retries", type=int, default=2)
    ap.add_argument("--player", default="",
                    help="Canonical POV nickname for the prosettings crosshair/"
                         "viewmodel lookup (default: timeline nick, resolved via "
                         "player_accounts.json by steam_id)")
    ap.add_argument("--rename", default="",
                    help="SteamID64->name JSON for mirv_replace_name (same map the "
                         "POV render uses)")
    ap.add_argument("--hide-avatars", action="store_true", default=False,
                    help="Hide scoreboard avatar images (auto-team lobbies with no "
                         "resolvable Steam avatars render them as missing-texture "
                         "checkers).")
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
                hook_retries=args.hook_retries, use_cpu=args.cpu,
                player_nick=args.player,
                rename_map=(json.loads(args.rename) if args.rename else None),
                min_seconds=args.min_seconds,
                hide_avatars=args.hide_avatars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
