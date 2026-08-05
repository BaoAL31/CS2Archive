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
import struct
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


# ── Ogg-Opus container (raw opus frames -> ffmpeg-decodable stream) ────────

def _crc32(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _ogg_page(serial: int, seq: int, granule: int, header_type: int, payload: bytes) -> bytes:
    rem = len(payload)
    segs: list[int] = []
    while rem >= 255:
        segs.append(255)
        rem -= 255
    segs.append(rem)
    seg_table = bytes(segs)
    header = (b"OggS" + bytes([0, header_type]) + struct.pack("<q", granule)
              + struct.pack("<I", serial) + struct.pack("<I", seq)
              + b"\x00\x00\x00\x00" + bytes([len(seg_table)]) + seg_table)
    crc = _crc32(header + payload)
    return header[:22] + struct.pack("<I", crc) + header[26:] + payload


def build_ogg(packets: list[bytes]) -> bytes:
    """Wrap raw opus frames in an Ogg-Opus stream (mono, 48 kHz timing)."""
    serial = 0x51CE
    head = struct.pack("<8sBBHIHB", b"OpusHead", 1, 1, 0, SAMPLE_RATE, 0, 0)
    vendor = b"cs2archive-mix-team-voice"
    tags = struct.pack("<8sI", b"OpusTags", len(vendor)) + vendor + struct.pack("<I", 0)
    out = _ogg_page(serial, 0, 0, 0x02, head)
    out += _ogg_page(serial, 1, 0, 0x00, tags)
    granule = 0
    for i, pkt in enumerate(packets):
        granule += FRAME_SAMPLES
        out += _ogg_page(serial, 2 + i, granule, 0x00, pkt)
    return out


def decode_packets(packets: list[bytes]) -> np.ndarray:
    """Decode raw opus frames to float32 mono PCM @ 48 kHz."""
    if not packets:
        return np.zeros(0, dtype=np.float32)
    ogg = build_ogg(packets)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg)
        ogg_path = f.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, "-f", "f32le", "-ac", "1", "-"],
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg opus decode failed: {r.stderr[-400:]!r}")
        return np.frombuffer(r.stdout, dtype=np.float32).copy()
    finally:
        Path(ogg_path).unlink(missing_ok=True)


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
    p = DemoParser(str(demo))
    info = p.parse_player_info()
    mapping: dict[str, int] = {}
    for _, row in info.iterrows():
        sid = str(row.get("steamid", ""))
        tn = row.get("team_number")
        if sid and tn is not None and sid not in mapping:
            mapping[sid] = int(tn)
    return mapping


def load_offsets(offsets_path: Path) -> dict:
    d = json.loads(offsets_path.read_text(encoding="utf-8"))
    # keys: total_rounds, total_duration_seconds, round_offsets, batches,
    #       per_round_ticks, per_round_durations
    ro = {int(k): float(v) for k, v in d["round_offsets"].items()}
    prt = {int(k): [int(a), int(b)] for k, (a, b) in d["per_round_ticks"].items()}
    return {"round_offsets": ro, "per_round_ticks": prt}


def video_duration(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def tick_to_time(tick: int, offsets: dict, tickrate: int) -> float | None:
    prt = offsets["per_round_ticks"]
    starts = sorted(prt)
    # find round whose tick span contains tick
    for r in starts:
        a, b = prt[r]
        if a <= tick <= b:
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

    # group by steamid (order preserved) so each player decodes in one pass
    by_player: dict[str, list[bytes]] = {}
    for r in team_rows:
        by_player.setdefault(r["steamid"], []).append(r["bytes"])

    dur = video_duration(video)
    buf = np.zeros(int(dur * SAMPLE_RATE) + FRAME_SAMPLES, dtype=np.float64)
    placed = 0
    for sid, packets in by_player.items():
        pcm = decode_packets(packets)
        if len(pcm) != len(packets) * FRAME_SAMPLES:
            print(f"  [warn] {sid}: decoded {len(pcm)} != {len(packets)*FRAME_SAMPLES}")
        # re-map ticks for this player
        ticks = [r["tick"] for r in team_rows if r["steamid"] == sid]
        for i, tick in enumerate(ticks):
            t = tick_to_time(tick, offsets, args.tickrate)
            if t is None:
                continue
            idx = int(t * SAMPLE_RATE)
            seg = pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            if idx + FRAME_SAMPLES > len(buf):
                seg = seg[:len(buf) - idx]
            buf[idx:idx + len(seg)] += seg
            placed += 1
        print(f"  [voice] {sid}: {len(packets)} pkts decoded")

    active = np.count_nonzero(buf != 0)
    print(f"[voice] placed {placed} packets; "
          f"{active / SAMPLE_RATE:.1f}s of audible voice "
          f"(rms {np.sqrt((buf**2).mean()):.4f})")

    if placed == 0:
        print("[voice] nothing to mix (no team voice in rendered rounds)")
        sys.exit(0)

    # write voice track as f32le wav (ffmpeg amix input)
    mix = (buf * args.voice_volume).astype(np.float32)
    with tempfile.NamedTemporaryFile(suffix=".f32", delete=False) as f:
        f.write(mix.tobytes())
        wav_path = f.name
    tmp_out = out
    if out.resolve() == video.resolve():
        tmp_out = out.with_name(out.stem + ".voice.mp4")
    try:
        # mix voice under the video's audio; stream-copy video
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", wav_path,
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(tmp_out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg mix failed: {r.stderr[-600:]!r}")
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
