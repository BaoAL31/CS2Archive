"""Transcribe POV-team voice comms and sample strat-callout clips for review.

Flow:
  1. Decode per-player voice bursts from the demo (mix_team_voice machinery:
     packet-level rows, persistent Opus decoder, burst grouping).
  2. Transcribe each burst with faster-whisper (auto language per burst —
     FACEIT lobbies code-switch RU/EN mid-round).
  3. Match strat-callout keywords (EN + RU) over transcripts.
  4. Cut sample clips from the finished youtube video around the top hits
     (±lead/lag) + transcript.json + review.md for human review.

Usage:
    python scripts/faceit/transcribe_comms.py --demo <demo> --steam-id <id> \\
        --video youtube/<run>_overlay/video.mp4 [--limit 10]

No CS2 needed. GPU (CUDA) used when available, else CPU.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from config import settings  # noqa: E402

# Strat callouts: (category, weight, patterns). EN + RU (transliterated
# variants included — whisper often latinizes Russian: "rash", "mid").
STRATS: list[tuple[str, float, list[str]]] = [
    ("rush", 2.0, [r"\brush\b", r"раш", r"\brash\b", r"рашим", r"\bgo go\b",
                   r"выходим", r"выход", r"пошли", r"бежим", r"\bpush\b", r"пуш"]),
    ("default", 1.0, [r"default", r"дефолт", r"\bspread\b", r"разошлись",
                      r"по точкам", r"стой", r"\bhold\b", r"ждем", r"ждём"]),
    ("save", 1.5, [r"\bsave\b", r"сейв", r"сейвим", r"keep", r"не пикай",
                   r"dont peek", r"don'?t peek", r"сохрани"]),
    ("force_eco", 1.5, [r"\bforce\b", r"форс", r"\beco\b", r"эко", r"half buy",
                        r"deagle", r"дигл", r"фул ?эко", r"закуп"]),
    ("rotate", 1.5, [r"rotat", r"перетяжка", r"перетяг", r"переход", r"иди (на )?[абb]",
                     r"need help", r"помощь", r"хелп", r"\bhelp\b"]),
    ("fake", 1.5, [r"\bfake\b", r"фейк", r"фейкуй", r"шум", r"noise", r"отвлеки"]),
    ("contact", 1.0, [r"contact", r"контакт", r"тихо", r"quiet", r"shift", r"шифтуй",
                      r"на шифте"]),
    ("utility", 1.0, [r"smoke", r"смок", r"дым", r"flash", r"флеш", r"флешка",
                      r"molotov", r"молик", r"молотов", r"he\b", r"хаешка",
                      r"раскид", r"раскидка", r"кидай", r"дай (флеш|смок|дым)"]),
    ("site_call", 2.0, [r"\bramp\b", r"рампа", r"palace", r"палас", r"паласы",
                        r"\bmid\b", r"мид", r"window", r"окно", r"catwalk",
                        r"ковры", r"ковёр", r"apps", r"апарты", r"tunnel",
                        r"туннель", r"верхняя", r"нижняя", r"upper", r"lower",
                        r"connector", r"коннектор", r"ct\b", r"city",
                        r"jungle", r"джунгли", r"stairs", r"лестница"]),
    ("plant_defuse", 1.0, [r"plant", r"ставь", r"бомба", r"бомбу", r"\bbomb\b",
                           r"defuse", r"дефьюз", r"деф", r"киты", r"\bkits\b"]),
    ("stack", 1.0, [r"\bstack\b", r"стак", r"впятером", r"all mid", r"все мид",
                    r"gamble", r"угадай"]),
    ("lurk", 1.0, [r"\blurk\b", r"люрк", r"люркуй", r"соло"]),
    ("timeout_talk", 0.5, [r"round", r"раунд", r"попробуем", r"давай"]),
]
STRAT_RES = [(cat, w, [re.compile(p, re.IGNORECASE) for p in pats])
             for cat, w, pats in STRATS]


@dataclass
class Utterance:
    steam_id: str
    nick: str
    tick_start: int
    tick_end: int
    t_start: float = 0.0
    t_end: float = 0.0
    text: str = ""
    lang: str = ""
    hits: list[str] = field(default_factory=list)
    score: float = 0.0
    clamped: bool = False


def video_time_for_tick(tick: int, offsets: dict, tickrate: int) -> tuple[float, bool]:
    """Video seconds for a demo tick; clamps gap ticks to the nearest span.

    tick_to_time returns None for ticks outside recorded play spans
    (freeze-time/buy-phase and death gaps are never recorded). Clamping to
    the nearest span boundary keeps the clip at the right MOMENT; clamped
    clips are flagged since their convo audio is not in the footage.
    """
    from mix_team_voice import tick_to_time

    s = tick_to_time(tick, offsets, tickrate)
    if s is not None:
        return s, False
    spans: list[tuple[int, int, float, float]] = []
    prt = offsets.get("per_round_ticks", {}) or {}
    prd = offsets.get("per_round_durations", {}) or {}
    ro = offsets.get("round_offsets", {}) or {}
    for r in sorted(prt):
        a, b = prt[r]
        v0 = float(ro.get(r, 0.0))
        v1 = v0 + float(prd.get(r, 0.0) or 0.0)
        spans.append((a, b, v0, v1))
    if not spans:
        return 0.0, True
    if tick <= spans[0][0]:
        return spans[0][2], True
    for i, (a, b, v0, v1) in enumerate(spans):
        if a <= tick <= b:
            return v0 + (tick - a) / max(1, b - a) * (v1 - v0), False
        if tick < a:
            return spans[i - 1][3] if i > 0 else v0, True
    return spans[-1][3], True


def _decode_burst_wav(rows: list[dict], sample_rate: int) -> bytes:
    """Decode one burst's packets to 16-bit mono WAV bytes (for whisper)."""
    import io
    import wave

    import numpy as np

    from mix_team_voice import decode_player_packets, detect_channels

    channels = detect_channels(rows[0]["bytes"])
    packets = [(r["tick"], r["bytes"]) for r in sorted(rows, key=lambda r: r["tick"])]
    decoded = decode_player_packets(packets, channels)
    pcm = np.concatenate([p for _, p in decoded]) if decoded else np.zeros(0, dtype=np.float32)
    # Resample to 16 kHz for whisper (polyphase-ish via linear interp; voice
    # content is narrowband enough that this is transparent).
    if sample_rate != 16000:
        import numpy as _np
        idx = _np.linspace(0, len(pcm) - 1, max(1, int(len(pcm) * 16000 / sample_rate)))
        pcm = _np.interp(idx, _np.arange(len(pcm)), pcm).astype(_np.float32)
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _load_model():
    from faster_whisper import WhisperModel

    # ctranslate2 loads CUDA DLLs (cublas64_12 etc.) via DLL search — the pip
    # nvidia wheels ship them under site-packages/nvidia/*/bin, which is not
    # on PATH by default.
    try:
        import site as _site
        for _sp in _site.getsitepackages():
            _nb = Path(_sp) / "nvidia"
            if _nb.is_dir():
                for _bin in sorted(_nb.glob("*/bin")):
                    if str(_bin) not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

    for device, compute in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel("small", device=device, compute_type=compute)
            # Canary: CUDA init is lazy — a silent 0.5s transcription forces
            # cublas/cudnn load now, so a broken GPU stack falls back to cpu
            # BEFORE the real work instead of crashing mid-run.
            import io
            import wave

            import numpy as np

            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.zeros(8000, dtype=np.float32) * 32767).astype(np.int16).tobytes())
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(buf.getvalue())
                canary = f.name
            try:
                list(model.transcribe(canary, language="en"))
            finally:
                Path(canary).unlink(missing_ok=True)
            print(f"  [stt] faster-whisper small on {device}/{compute}")
            return model
        except Exception as e:
            print(f"  [stt] {device} unavailable ({str(e)[:120]})")
    raise SystemExit("[ERROR] faster-whisper loads on neither cuda nor cpu")


def transcribe_bursts(model, bursts: list[tuple[str, str, list[dict]]]) -> list[Utterance]:
    """Transcribe (steam_id, nick, rows) bursts -> Utterances (text only)."""
    import tempfile

    from mix_team_voice import SAMPLE_RATE, group_voice_rows

    out: list[Utterance] = []
    for sid, nick, rows in bursts:
        for group in group_voice_rows(sorted(rows, key=lambda r: r["tick"])):
            dur_s = (group[-1]["tick"] - group[0]["tick"]) / 64.0 + 0.32
            if dur_s < 0.4:
                continue  # sub-syllable blip, not a callout
            wav = _decode_burst_wav(group, SAMPLE_RATE)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav)
                path = f.name
            try:
                segments, info = model.transcribe(path, language=None, vad_filter=False)
                text = " ".join(s.text.strip() for s in segments).strip()
                lang = getattr(info, "language", "") or ""
            finally:
                Path(path).unlink(missing_ok=True)
            if not text:
                continue
            out.append(Utterance(
                steam_id=sid, nick=nick,
                tick_start=int(group[0]["tick"]), tick_end=int(group[-1]["tick"]),
                text=text, lang=lang,
            ))
    print(f"  [stt] {len(out)} transcribed utterances from {len(bursts)} player-bursts")
    return out


def detect_strats(utterances: list[Utterance]) -> list[Utterance]:
    """Score utterances against strat patterns (EN + RU)."""
    hits: list[Utterance] = []
    for u in utterances:
        cats: dict[str, float] = {}
        for cat, w, res in STRAT_RES:
            if any(r.search(u.text) for r in res):
                cats[cat] = w
        if cats:
            u.hits = sorted(cats)
            u.score = round(sum(cats.values()), 2)
            hits.append(u)
    hits.sort(key=lambda u: -u.score)
    print(f"  [strat] {len(hits)} callout hits from {len(utterances)} utterances")
    return hits


def cut_sample(video: Path, dest: Path, t0: float, t1: float) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    r = subprocess.run(
        [settings.ffmpeg_exe, "-y", "-ss", f"{max(0.0, t0):.3f}", "-to", f"{t1:.6f}",
         "-i", str(video), "-c", "copy", "-movflags", "+faststart",
         "-f", "mp4", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not tmp.is_file():
        print(f"  [warn] clip cut failed [{t0:.1f}-{t1:.1f}]: {(r.stderr or '')[-200:]}")
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe team comms, sample strat callouts.")
    ap.add_argument("--demo", required=True, help="FACEIT .dem path")
    ap.add_argument("--steam-id", required=True, help="POV player steam64 (team scope)")
    ap.add_argument("--video", required=True, help="Finished youtube video.mp4 to cut clips from")
    ap.add_argument("--offsets", required=True, help="youtube video.round_offsets.json (video-time map)")
    ap.add_argument("--out-dir", default=None, help="Review dir (default: renders/voice-review/<video-stem>/)")
    ap.add_argument("--limit", type=int, default=10, help="Max sampled clips (default 10)")
    ap.add_argument("--retranscribe", action="store_true",
                    help="Redo STT even when transcript.json exists (default: resume)")
    ap.add_argument("--lead", type=float, default=8.0, help="Seconds before utterance in clip")
    ap.add_argument("--lag", type=float, default=4.0, help="Seconds after utterance end in clip")
    ap.add_argument("--tickrate", type=int, default=64)
    args = ap.parse_args()

    demo = Path(args.demo)
    video = Path(args.video)
    out_dir = Path(args.out_dir) if args.out_dir else (
        PROJECT_ROOT / "renders" / "voice-review" / video.stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))
    from mix_team_voice import (SAMPLE_RATE, group_voice_rows, load_offsets,
                                load_team_map, load_voice, tick_to_time)
    from faceit_names import canonical_nick, known_pro_steam_ids

    offsets = load_offsets(Path(args.offsets))
    rows = load_voice(demo)
    team_map = load_team_map(demo, args.steam_id)
    pov_team = team_map.get(args.steam_id)
    if pov_team is None:
        raise SystemExit(f"[ERROR] steam {args.steam_id} not in demo team map")
    team_rows = [r for r in rows if team_map.get(r["steamid"]) == pov_team]
    print(f"  [voice] {len(team_rows)} POV-team packets")

    # Demo-header names for all speakers (pros canonicalized).
    import demoparser2 as dp
    names = {}
    try:
        for _, row in dp.DemoParser(str(demo)).parse_player_info().iterrows():
            sid = str(row.get("steamid", "")).strip()
            if sid and sid.lower() != "nan":
                names[sid] = str(row.get("name", "")).strip()
    except Exception as e:
        print(f"  [warn] player names unavailable: {e}")
    pros = known_pro_steam_ids()
    by_player: dict[str, list[dict]] = {}
    for r in team_rows:
        by_player.setdefault(r["steamid"], []).append(r)
    bursts = []
    for sid, rs in by_player.items():
        nick = pros.get(sid) or canonical_nick(names.get(sid, sid))
        bursts.append((sid, nick, rs))

    transcript_path = out_dir / "transcript.json"
    if transcript_path.is_file() and not args.retranscribe:
        utterances = [Utterance(**u) for u in
                      json.loads(transcript_path.read_text(encoding="utf-8"))]
        print(f"  [resume] loaded {len(utterances)} utterances from {transcript_path.name}")
    else:
        model = _load_model()
        utterances = transcribe_bursts(model, bursts)
        transcript_path.write_text(json.dumps([u.__dict__ for u in utterances],
                                              indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  [OK] transcript: {transcript_path.name} ({len(utterances)} utterances)")
    for u in utterances:
        s, c1 = video_time_for_tick(u.tick_start, offsets, args.tickrate)
        e, c2 = video_time_for_tick(u.tick_end, offsets, args.tickrate)
        u.t_start, u.t_end, u.clamped = s, max(e, s + 0.5), (c1 or c2)

    hits = detect_strats(utterances)

    transcript_path = out_dir / "transcript.json"
    transcript_path.write_text(json.dumps([u.__dict__ for u in utterances],
                                          indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"  [OK] transcript: {transcript_path.name} ({len(utterances)} utterances)")

    # Diverse sample: top hits round-robin across rounds (tick order), cap.
    picked: list[Utterance] = []
    seen_rounds: set[int] = set()
    prt = offsets.get("per_round_ticks", {}) or {}
    for u in hits:
        rn = next((r for r, (a, b) in prt.items() if a <= u.tick_start <= b), 0)
        if rn in seen_rounds and len(picked) >= len(prt):
            continue
        seen_rounds.add(rn)
        picked.append(u)
        if len(picked) >= args.limit:
            break

    review = [f"# Strat callout review — {video.name}", ""]
    for i, u in enumerate(picked, 1):
        dest = out_dir / f"strat-{i:02d}-{u.nick}-r{u.tick_start}.mp4"
        ok = cut_sample(video, dest, u.t_start - args.lead, u.t_end + args.lag)
        clamp_note = " [pre-round call — convo audio not in footage]" if u.clamped else ""
        review.append(f"## {i}. {u.hits} {u.nick} @ {u.t_start:.1f}s "
                      f"(tick {u.tick_start}, lang={u.lang or '?'}, score={u.score}){clamp_note}")
        review.append(f"> {u.text}")
        review.append(f"clip: `{dest.name}`{' (CUT FAILED)' if not ok else ''}")
        review.append("")
    (out_dir / "review.md").write_text("\n".join(review), encoding="utf-8")
    print(f"  [OK] {len(picked)} sample clips + review.md in {out_dir}")
    for line in review[2:]:
        if line.startswith("##"):
            print("  " + line)


if __name__ == "__main__":
    main()
