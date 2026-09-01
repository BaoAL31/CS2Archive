"""Shared CSDM --config-file envelope for tick-range renders.

Shorts, highlight segments, and match intro all render tick windows via
``csdm video --config-file``. Product-specific HUD cfg lines stay with the
caller; this module owns the JSON envelope and sequence tick sanitizing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def sanitize_tick_window(start_tick: int, end_tick: int, *, min_ticks: int = 64) -> tuple[int, int]:
    if start_tick >= end_tick:
        start_tick, end_tick = end_tick, start_tick
    if end_tick - start_tick < min_ticks:
        end_tick = start_tick + min_ticks
    return start_tick, end_tick


def sequence(
    number: int,
    start_tick: int,
    end_tick: int,
    pov_steam_id: str,
    cfg_text: str,
    *,
    extra_player_cameras: list[dict] | None = None,
    show_only_death_notices: bool = False,
    player_voices: bool = False,
    death_notices_duration: int = 5,
) -> dict[str, Any]:
    start_tick, end_tick = sanitize_tick_window(start_tick, end_tick)
    cameras = [
        {"tick": start_tick, "playerSteamId": pov_steam_id, "playerName": "pov"},
    ]
    if extra_player_cameras:
        cameras.extend(extra_player_cameras)
    return {
        "number": number,
        "startTick": start_tick,
        "endTick": end_tick,
        "cfg": cfg_text,
        "showXRay": False,
        "showAssists": True,
        "showOnlyDeathNotices": show_only_death_notices,
        "playersOptions": [],
        "cameras": [],
        "playerCameras": cameras,
        "playerVoicesEnabled": player_voices,
        "recordAudio": True,
        "deathNoticesDuration": death_notices_duration,
    }


def tick_range_config(
    demo_path: Path,
    output_dir: Path,
    sequences: list[dict],
    *,
    width: int,
    height: int,
    framerate: int,
    ffmpeg_settings: dict,
    close_game: bool = True,
) -> dict[str, Any]:
    return {
        "demoPath": str(Path(demo_path).resolve()),
        "outputFolderPath": str(Path(output_dir).resolve()),
        "recordingSystem": "HLAE",
        "recordingOutput": "video",
        "encoderSoftware": "FFmpeg",
        "framerate": framerate,
        "width": width,
        "height": height,
        "closeGameAfterRecording": close_game,
        "concatenateSequences": False,
        "trueView": False,
        "ffmpegSettings": ffmpeg_settings,
        "sequences": sequences,
    }
