"""Per-player talk intervals — the reusable part of the removed voice-shade module.

CR: the legacy "shade" voice indicator (dimming the scoreboard to highlight the POV team) was
removed; the voice HUD is now the Swift indicators, which are baked into the captured frames. This
module keeps the one piece that was not shade-specific: mapping raw voice-packet activity to
[start_sec, end_sec] intervals per POV-team player, which `speaker_rows_preview` (the name/avatar
row preview for the Swift HUD) still needs.
"""
from __future__ import annotations

from pathlib import Path

from cs2archive.faceit.mix_team_voice import (
    SAMPLE_RATE,
    decode_player_packets,
    detect_channels,
    group_voice_rows,
    load_team_map,
    load_voice,
    tick_to_time,
)


def _player_talk_segments(demo: Path, offsets: dict, pov_team: int,
                          steam_id: str, tickrate: int) -> dict[str, list[tuple[float, float]]]:
    """Return {steamid: [(start_sec, end_sec), ...]} for POV-team players.

    Talk segments come from RAW packet activity, not decoded-PCM RMS: a player
    is "talking" from the first packet of a burst to the last packet plus its
    decoded duration (the mic is live for exactly that span). Decoded-PCM RMS
    is a poor proxy here — a soft/short word mid-sentence dips below threshold
    and the indicator turns off early or misses speech entirely. Packet presence
    tracks the actual mic state (the in-game speaker indicator uses the same).
    """
    rows = load_voice(demo)
    team_map = load_team_map(demo, steam_id)
    out: dict[str, list[tuple[float, float]]] = {}
    for sid in {r["steamid"] for r in rows}:
        if team_map.get(sid) != pov_team:
            continue
        player_rows = sorted(
            (r for r in rows if r["steamid"] == sid), key=lambda r: r["tick"])
        segs: list[tuple[float, float]] = []
        for group in group_voice_rows(player_rows):
            channels = detect_channels(group[0]["bytes"])
            # decode only the first packet to get the per-packet frame length;
            # CS2 FACEIT voice is 10 ms frames, but decode to be exact.
            decoded = decode_player_packets([(g["tick"], g["bytes"]) for g in group], channels)
            last = decoded[-1]
            t0 = tick_to_time(group[0]["tick"], offsets, tickrate)
            t1 = tick_to_time(group[-1]["tick"], offsets, tickrate)
            if t0 is None or t1 is None:
                continue
            dur_s = len(last[1]) / SAMPLE_RATE
            segs.append((t0, t1 + dur_s))
        out[sid] = segs
    return out
