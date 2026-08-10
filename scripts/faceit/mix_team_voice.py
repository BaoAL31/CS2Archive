"""Mix ONLY the POV player's team voice chat into a rendered POV video.

FACEIT (PBDEMS2) demos record per-player voice chat as raw Opus packets
(10ms frames, 48 kHz mono when decoded). The render cfg sets
``voice_enable 0`` so the game audio is voice-free; this script decodes the
demo's voice, keeps only the POV player's team, aligns it to the video
timeline (via the concat step's ``combined.round_offsets.json`` sidecar) and
mixes it into the video's audio track. Video is stream-copied (no re-encode).

Usage::

    python scripts/faceit/mix_team_voice.py \\
        --demo "demos/faceit/team_X vs team_Y - Mirage.dem" \\
        --video "youtube/<run_id>_overlay/video.mp4" \\
        --steam-id 76561198386265483 \\
        --offsets "renders/pov-.../combined.round_offsets.json" \\
        --out "youtube/<run_id>_overlay/video.mp4"

Run again on the same ``--out``: refuses unless ``--force`` (idempotency
marker ``<out>.teamvoice.json``).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))

import numpy as np  # noqa: E402
from demoparser2 import DemoParser  # noqa: E402

SAMPLE_RATE = 48000
FRAME_SAMPLES = 480  # 10 ms opus frame @ 48 kHz
_TICKRATE = 64  # CS2 PBDEMS2 demo tickrate
# Decoded output is never assumed to be exactly this; it's just an upper bound on
# the per-packet decode buffer (opus packets can hold up to 120 ms of audio).
_MAX_FRAME_SAMPLES = FRAME_SAMPLES * 6
# Voice packets that land closer than this (seconds) are treated as one utterance
# (burst); larger gaps are real pauses.
_MAX_GROUP_GAP_S = 0.100
# When placing a burst, a packet whose tick maps ahead of the current write
# position by more than this is treated as a real pause and we jump forward.
_MAX_SLIP_S = 0.020


# ── Opus packet-aligned decode (via libopus, not ffmpeg/Ogg) ───────────────

# opuslib uses ``ctypes.util.find_library("opus")`` which fails to locate the
# bundled dll on Windows. Prepend a dir that contains libopus to PATH so the
# import finds it. We only need the decoder symbols; any full libopus works.
def _ensure_opus_library() -> None:
    # opuslib uses ``ctypes.util.find_library("opus")`` which looks for a file
    # named ``opus.dll``/``opus`` on PATH. We vendor a full libopus as
    # ``opus.dll`` inside the opuslib package dir; ensure that dir is on PATH
    # before opuslib is imported. (``find_library`` wants exactly ``opus.dll``,
    # not ``libopus-0.dll``, so only the opuslib package dir matches reliably.)
    opuslib_pkg = Path(
        r"C:\Users\jembo\anaconda3\envs\cs2archive\Lib\site-packages\opuslib")
    if not (opuslib_pkg / "opus.dll").exists():
        for src in [Path(r"C:\Program Files\Kdenlive\bin\libopus.dll")]:
            if src.exists():
                import shutil
                shutil.copy2(src, opuslib_pkg / "opus.dll")
                break
    sp = str(opuslib_pkg)
    if sp not in os.environ.get("PATH", ""):
        os.environ["PATH"] = sp + os.pathsep + os.environ.get("PATH", "")


def detect_channels(sample_packet: bytes) -> int:
    """Read stereo/mono from the packet's own Opus TOC byte (bit 0x04).

    This is authoritative per-packet, unlike the OpusHead channel claim. CS2
    FACEIT voice decodes as mono (bit unset).
    """
    return 2 if (sample_packet[0] & 0x04) else 1


def decode_player_packets(
    packets_sorted: list[tuple[int, bytes]],
    channels: int,
) -> list[tuple[int, np.ndarray]]:
    """Decode one player's packets with a single persistent Opus decoder.

    Returns ``[(tick, pcm_float32_mono), ...]`` where each ``pcm`` length is the
    ACTUAL decoded sample count for that packet (never assumed to be 480). Using
    one persistent decoder preserves the inter-frame prediction state that makes
    the "individual player" path sound clean.
    """
    _ensure_opus_library()
    from opuslib import Decoder

    decoder = Decoder(SAMPLE_RATE, channels)
    out: list[tuple[int, np.ndarray]] = []
    for tick, raw in packets_sorted:
        pcm_bytes = decoder.decode(raw, _MAX_FRAME_SAMPLES, decode_fec=False)
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1)  # downmix to mono
        out.append((tick, pcm))
    return out


def group_voice_rows(
    rows_sorted: list[dict],
    tick_rate: int = _TICKRATE,
    max_gap_s: float = _MAX_GROUP_GAP_S,
):
    """Yield contiguous bursts of voice rows (packets within ``max_gap_s``)."""
    group: list[dict] = []
    last_tick: int | None = None
    for r in rows_sorted:
        if not group:
            group.append(r)
            last_tick = r["tick"]
            continue
        gap_s = (r["tick"] - last_tick) / tick_rate
        if gap_s <= max_gap_s:
            group.append(r)
        else:
            yield group
            group = [r]
        last_tick = r["tick"]
    if group:
        yield group


def place_into_buffer(buf: np.ndarray, rows_sorted: list[dict], offsets: dict,
                     tickrate: int) -> int:
    """Place a player's decoded bursts into ``buf`` at tick-derived times.

    Within a burst, packets are placed SEQUENTIALLY (each right after the
    previous) rather than at their own tick sample. This avoids the exact
    overlap that happens when many packets share the same tick (comb filtering
    -> robotic/static). A tick that maps ahead by more than ``_MAX_SLIP_S`` is
    treated as a real pause and we jump forward.
    """
    rows_sorted = sorted(rows_sorted, key=lambda r: r["tick"])
    channels = detect_channels(rows_sorted[0]["bytes"])
    placed = 0
    for group in group_voice_rows(rows_sorted):
        packets = [(r["tick"], r["bytes"]) for r in group]
        decoded = decode_player_packets(packets, channels)
        if not decoded:
            continue
        # anchor the burst at its first packet's tick time
        t0 = tick_to_time(decoded[0][0], offsets, tickrate)
        if t0 is None:
            continue
        pos = int(round(t0 * SAMPLE_RATE))
        for tick, pcm in decoded:
            desired = tick_to_time(tick, offsets, tickrate)
            if desired is not None:
                desired_idx = int(round(desired * SAMPLE_RATE))
                # real pause -> jump; duplicate/late tick -> keep sequential
                if desired_idx > pos + int(_MAX_SLIP_S * SAMPLE_RATE):
                    pos = desired_idx
            end = pos + len(pcm)
            if pos >= len(buf):
                break
            if end > len(buf):
                pcm = pcm[:len(buf) - pos]
                end = len(buf)
            buf[pos:end] += pcm
            pos = end
            placed += 1
    return placed


# ── demo voice extraction ──────────────────────────────────────────────────

def load_voice(demo: Path) -> list[dict]:
    p = DemoParser(str(demo))
    rows = p.parse_voice()
    out = []
    for r in rows:
        out.append({
            "tick": int(r["tick"]),
            "steamid": str(r["steamid"]),
            "bytes": bytes(r["bytes"]),
        })
    out.sort(key=lambda r: r["tick"])
    return out


def load_team_map(demo: Path) -> dict[str, int]:
    """Return {steamid: stable_team_id} for every player in the demo.

    ``parse_player_info().team_number`` is the CURRENT T/CT side (2=CT, 3=T),
    which FLIPS at halftime — so it is NOT a stable team identity and cannot be
    used to group teammates across the whole match (it would mix the POV team's
    voice with the opponents' in one half). Here we derive a stable id from each
    player's side in the FIRST half (before the swap). Because both teams swap
    sides at halftime, a player's first-half side uniquely identifies their team:
    teammates share the POV player's first-half side, opponents have the other.
    """
    p = DemoParser(str(demo))
    # Sample the per-player side over the whole match so we can (a) detect the
    # halftime flip and (b) read the first-half side for every player.
    header = p.parse_header()
    total = int(header.get("map_ticks") or 0)
    # map_ticks may not be present; fall back to a wide tick range.
    ticks = list(range(0, 2_000_000, 20000))
    if total and total > 0:
        ticks = list(range(0, total, max(1, total // 60)))
    try:
        side = p.parse_ticks(
            ["CCSPlayerController.m_iTeamNum", "CCSPlayerController.m_steamID"],
            ticks=ticks,
        )
    except Exception:
        side = None
    if side is None or side.empty:
        # Fallback: parse_player_info snapshot (pre-halftime if we can pick the
        # earliest). Usually this is fine when ticks aren't available.
        info = p.parse_player_info()
        mapping: dict[str, int] = {}
        for _, row in info.iterrows():
            sid = str(row.get("steamid", ""))
            tn = row.get("team_number")
            if sid and tn is not None and sid not in mapping:
                mapping[sid] = int(tn)
        return mapping

    # Stable team id for each player = their side in the earliest half. We take
    # the side at the EARLIEST sampled tick (first half) as the stable id. To be
    # robust to edge ticks, use the mode of the first 40% of that player's rows
    # (all pre-halftime since halftime is ~mid-match).
    from collections import Counter
    per_player: dict[str, list[int]] = {}
    for _, row in side.iterrows():
        sid = str(row.get("CCSPlayerController.m_steamID", ""))
        tn = row.get("CCSPlayerController.m_iTeamNum")
        if sid and tn is not None and tn not in (0,):
            per_player.setdefault(sid, []).append(int(tn))

    mapping: dict[str, int] = {}
    for sid, teams in per_player.items():
        if not teams:
            continue
        half = teams[: max(1, len(teams) // 3)]  # earliest ~1/3 rows = first half
        stable = Counter(half).most_common(1)[0][0]
        mapping[sid] = stable
    return mapping


def load_offsets(offsets_path: Path) -> dict:
    d = json.loads(offsets_path.read_text(encoding="utf-8"))
    # keys: total_rounds, total_duration_seconds, round_offsets, batches,
    #       per_round_ticks, per_round_durations
    ro = {int(k): float(v) for k, v in d["round_offsets"].items()}
    prt = {int(k): [int(a), int(b)] for k, (a, b) in d["per_round_ticks"].items()}
    prd = {int(k): float(v) for k, v in (d.get("per_round_durations") or {}).items()}
    return {"round_offsets": ro, "per_round_ticks": prt, "per_round_durations": prd}


def video_duration(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def tick_to_time(tick: int, offsets: dict, tickrate: int) -> float | None:
    prt = offsets["per_round_ticks"]
    prd = offsets.get("per_round_durations", {})
    # find round whose tick span contains tick
    for r in sorted(prt):
        a, b = prt[r]
        if a <= tick <= b:
            span = max(1, b - a)
            # The video is a CONCATENATION of COMPRESSED rounds (round video is
            # shorter than the raw game ticks). Map the tick fraction to the
            # round's actual (compressed) video duration, not game-time / tickrate,
            # otherwise voice from late in a round spills past the round's video
            # end into the next round -> echo/overlap.
            dur = prd.get(r)
            if dur and dur > 0:
                return offsets["round_offsets"][r] + (tick - a) / span * dur
            return offsets["round_offsets"][r] + (tick - a) / tickrate
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", required=True, help="FACEIT .dem path")
    ap.add_argument("--video", required=True, help="video to mix voice into")
    ap.add_argument("--steam-id", required=True, help="POV player steam64 id")
    ap.add_argument("--offsets", required=True, help="combined.round_offsets.json")
    ap.add_argument("--out", required=True, help="output mp4")
    ap.add_argument("--voice-volume", type=float, default=2.5,
                    help="gain applied to the voice track (default 2.5)")
    ap.add_argument("--voice-rms-target", type=float, default=0.08,
                    help="per-player target active-RMS for equal loudness (default 0.08)")
    ap.add_argument("--voice-max-gain", type=float, default=12.0,
                    help="max per-player normalization gain (default 12.0)")
    ap.add_argument("--tickrate", type=int, default=_TICKRATE)
    ap.add_argument("--force", action="store_true",
                    help="overwrite out even if a teamvoice marker exists")
    args = ap.parse_args()

    demo = Path(args.demo)
    video = Path(args.video)
    out = Path(args.out)
    marker = out.with_suffix(out.suffix + ".teamvoice.json")
    if marker.exists() and not args.force:
        print(f"[skip] {out.name} already has team voice mixed (--force to redo)")
        return

    offsets = load_offsets(Path(args.offsets))
    print(f"[voice] parsing {demo.name} ...")
    rows = load_voice(demo)
    if not rows:
        print("[voice] no voice data in demo")
        sys.exit(1)
    team_map = load_team_map(demo)
    pov_team = team_map.get(args.steam_id)
    if pov_team is None:
        print(f"[voice] steam id {args.steam_id} not found in demo player info")
        sys.exit(1)
    team_rows = [r for r in rows if team_map.get(r["steamid"]) == pov_team]
    print(f"[voice] {len(rows)} voice packets total, "
          f"{len(team_rows)} from POV team (team {pov_team})")
    if not team_rows:
        print(f"[ERROR] No voice data for the POV team (team {pov_team}) in {demo.name}. "
              f"Voice comms/indicators require per-player voice in the demo. "
              f"Nothing to mix — refusing to produce a silent 'comms' result.")
        sys.exit(1)

    # group by steamid so each player decodes in one pass
    by_player: dict[str, list[dict]] = {}
    for r in team_rows:
        by_player.setdefault(r["steamid"], []).append(r)

    dur = video_duration(video)
    n = int(dur * SAMPLE_RATE) + FRAME_SAMPLES

    # Place each player into its OWN buffer so we can normalize players
    # independently before summing. Previously everyone shared one buffer and got
    # a single global peak-normalize, so the loudest player set the ceiling and
    # quieter players (e.g. magixx) stayed buried under the team voice.
    player_bufs: dict[str, np.ndarray] = {}
    placed = 0
    for sid, rows_p in by_player.items():
        rows_p.sort(key=lambda r: r["tick"])
        pb = np.zeros(n, dtype=np.float64)
        placed += place_into_buffer(pb, rows_p, offsets, args.tickrate)
        player_bufs[sid] = pb
        print(f"  [voice] {sid}: {len(rows_p)} pkts decoded")

    # Equal-loudness: normalize each player to a target RMS measured over their
    # ACTIVE (non-silent) voice, so no one is buried under a louder teammate.
    # Whole-buffer RMS is diluted by silence, so we measure loudness only where
    # the player actually speaks. Cap the gain so a single soft or sparse player
    # isn't amplified into a wall of noise.
    #
    # PER-ROUND scope: a single whole-match gain penalizes a player who is loud
    # in one round (small gain) and quiet in another (stays buried) — e.g. magixx
    # talks loudly late but is nearly silent in round 1, and a whole-match gain
    # leaves his early rounds at ~14-57% of target. So the gain is computed and
    # applied PER ROUND: each player's voice in each round is normalized to the
    # target active-RMS. This keeps a player's voice audible in every round it
    # exists, regardless of how loud they are in other rounds.
    target_rms = args.voice_rms_target
    # Build per-round sample spans [start,end) from the concat sidecar.
    ro = offsets["round_offsets"]
    prd = offsets.get("per_round_durations", {})
    round_spans = []  # (round, start_sample, end_sample)
    for r in sorted(ro):
        start = ro[r]
        rdur = prd.get(r)
        end = start + rdur if rdur and rdur > 0 else ro.get(r + 1, start)
        round_spans.append((r, int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)))
    if not round_spans:
        round_spans = [(0, 0, n)]

    per_round = {sid: {} for sid in player_bufs}
    for sid, pb in player_bufs.items():
        pb_norm = np.zeros_like(pb)
        for r, s0, s1 in round_spans:
            seg = pb[s0:s1]
            nz = seg[seg != 0]
            active_rms = float(np.sqrt((nz ** 2).mean())) if len(nz) else 0.0
            gain = target_rms / active_rms if active_rms > 0 else 0.0
            gain = min(gain, args.voice_max_gain)
            per_round[sid][r] = round(gain, 3)
            if gain > 0:
                pb_norm[s0:s1] = seg * gain
        player_bufs[sid] = pb_norm
    print(f"  [voice] per-ROUND gains (x{target_rms:.3f} active-RMS target, "
          f"cap {args.voice_max_gain:.1f}x):")
    for sid, rg in per_round.items():
        vals = {r: g for r, g in sorted(rg.items()) if g}
        print(f"    {sid}: {vals}")

    buf = np.zeros(n, dtype=np.float64)
    for pb in player_bufs.values():
        buf += pb

    active = np.count_nonzero(buf != 0)
    print(f"[voice] placed {placed} packets; "
          f"{active / SAMPLE_RATE:.1f}s of audible voice "
          f"(rms {np.sqrt((buf**2).mean()):.4f})")

    if placed == 0:
        print("[voice] nothing to mix (no team voice in rendered rounds)")
        sys.exit(0)

    # Mix voice UNDER the video's audio track by summing PCM buffers in numpy
    # (replacing the old ffmpeg `amix` filter, which resampled and corrupted the
    # output -> continuous noise / "cutting out"). Steps:
    #   1. decode the video's audio to float32 mono PCM at SAMPLE_RATE,
    #   2. add the (normalized, gain-scaled) voice buffer onto it,
    #   3. re-encode the video with the new audio (video stream-copied).
    _peak = float(np.abs(buf).max())
    if _peak > 0:
        buf = buf * (0.9 / max(args.voice_volume, 0.1) / _peak)
    mix = (buf * args.voice_volume).astype(np.float32)
    if mix.shape[0] > int(dur * SAMPLE_RATE):
        mix = mix[:int(dur * SAMPLE_RATE)]

    # Decode video audio to mono PCM.
    va = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(video), "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True,
    )
    if va.returncode != 0 or not va.stdout:
        # No/invalid audio stream in the video -> start from silence.
        base = np.zeros(mix.shape[0], dtype=np.float32)
    else:
        base = np.frombuffer(va.stdout, dtype=np.float32).copy()
        if base.shape[0] < mix.shape[0]:
            base = np.pad(base, (0, mix.shape[0] - base.shape[0]))
        elif base.shape[0] > mix.shape[0]:
            base = base[:mix.shape[0]]
    summed = base + mix
    peak = float(np.abs(summed).max())
    if peak > 1.0:
        summed = summed * (0.95 / peak)
    summed16 = (np.clip(summed, -1.0, 1.0) * 32767).astype(np.int16)

    tmp_out = out
    if out.resolve() == video.resolve():
        tmp_out = out.with_name(out.stem + ".voice.mp4")
    try:
        # Encode video with the mixed audio; stream-copy video.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            import wave
            with wave.open(f.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(summed16.tobytes())
            wav_path = f.name
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-i", wav_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(tmp_out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg encode failed: {r.stderr[-600:]!r}")
        if tmp_out != out:
            tmp_out.replace(out)
    finally:
        Path(wav_path).unlink(missing_ok=True)

    marker.write_text(json.dumps({
        "voice_packets": len(team_rows),
        "placed": placed,
        "team": pov_team,
        "voice_volume": args.voice_volume,
        "source": str(demo),
    }, indent=2), encoding="utf-8")
    print(f"[voice] wrote {out.name} with team-only voice")


if __name__ == "__main__":
    main()
