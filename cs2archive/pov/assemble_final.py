"""Join the delivered POV video in ONE pass.

Delivered order: ``hook -> intro card + buy phase -> POV body -> outro``.

Each component is generated — and resumed — independently by its own step
(``render_hook``/``assemble_hook``, ``create_repeek_intro``/``intro_prepend``,
``generate_outro``). This module only joins them, so a component can be
re-rendered without touching the others.

Why one pass: the old flow ran three separate full-file passes over the
multi-GB body (intro prepend, hook prepend, outro append), each reading and
rewriting the whole file. Joining once reads and writes the total bytes once.

Audio policy (see ``docs/bugs/audio-sync-atempo-drift.md``):

* **The picture is a stream copy.** Frame timestamps are never touched, and
  every part must share one video profile — a mismatch is fatal, because this
  pass cannot normalize the picture without re-encoding the delivered
  bitstream.
* **The audio is always rebuilt per part on the picture timeline.** Each part's
  audio is decoded on its own (a standalone decode honours that file's AAC edit
  list, i.e. its encoder delay is trimmed), trimmed AND padded to that part's
  exact PICTURE length, joined with the concat filter and encoded once.

  It is deliberately NOT stream-copied, even when every part is already AAC
  48k stereo. Matching codec settings do not prove that each part's encoder
  priming and packet tail line up with its picture length: appending the
  packets re-inserts one encoder delay per join, which measured +26 ms per
  round join on the concat path (0.44 s late by round 18, YEKINDAR Dust2).
  Rebuilding is the only join that is exact by construction, and it costs one
  AAC encode — the same encode the old hook-prepend pass already paid.

The outro carries no audio at all; the rebuild gives it a silent stretch of its
own picture length, which is what the old ``-c copy`` append relied on.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from cs2archive.config import settings

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe

# House audio format. Every component is normalized to this so the copy join
# stays available.
HOUSE_AUDIO = ("aac", 48000, 2)


class AudioProbeError(RuntimeError):
    """ffprobe could not determine whether a part carries audio.

    Distinct from "the part has no audio stream": conflating the two would let
    an inspection failure silently drop that component's sound from the
    delivered video.
    """


def _log(msg: str) -> None:
    print(msg, flush=True)


def probe_video_params(path: Path) -> dict | None:
    """Codec identity of the first video stream, or None when unprobeable."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        streams = (json.loads(r.stdout or "{}").get("streams") or [])
        if not streams:
            return None
        s = streams[0]
        return {
            "codec": str(s.get("codec_name") or ""),
            "width": int(s.get("width") or 0),
            "height": int(s.get("height") or 0),
            "pix_fmt": str(s.get("pix_fmt") or ""),
        }
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def probe_audio_params(path: Path) -> tuple[str, int, int] | None:
    """(codec, sample_rate, channels) of the first audio stream.

    Returns ``None`` ONLY when the probe succeeded and the file genuinely has
    no audio stream. A probe that could not be completed — ffprobe missing,
    timeout, unreadable file, non-zero exit, malformed output — raises
    ``AudioProbeError``. The two cases must never be conflated: reading an
    inspection failure as "this component is silent" would replace the body's
    sound with silence and still report success.
    """
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioProbeError(f"cannot run ffprobe on {path.name}: {exc}") from exc
    if r.returncode != 0:
        raise AudioProbeError(
            f"ffprobe failed on {path.name} (rc={r.returncode}): "
            f"{(r.stderr or '').strip()[:200]}"
        )
    try:
        streams = (json.loads(r.stdout or "{}").get("streams") or [])
    except ValueError as exc:
        raise AudioProbeError(
            f"ffprobe returned unparseable output for {path.name}: {exc}"
        ) from exc
    if not streams:
        # Probe succeeded, no audio stream: a confirmed silent component.
        return None
    s = streams[0]
    try:
        return (str(s.get("codec_name") or ""),
                int(s.get("sample_rate") or 0),
                int(s.get("channels") or 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioProbeError(
            f"ffprobe returned malformed audio params for {path.name}: {exc}"
        ) from exc


def probe_picture_duration(path: Path) -> float:
    """Frame-exact picture length (``nb_frames / fps``), 0.0 when unknown.

    This — not the container duration — is the sync reference: each part's
    audio is trimmed/padded to it so the joined track cannot slip against the
    picture.
    """
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,r_frame_rate,avg_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return 0.0
        streams = (json.loads(r.stdout or "{}").get("streams") or [])
        if not streams:
            return 0.0
        s = streams[0]
        fps_raw = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
        num, _, den = str(fps_raw).partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        frames = int(s.get("nb_frames") or 0)
        if fps <= 0 or frames <= 0:
            return 0.0
        return frames / fps
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 0.0


def video_join_compatible(parts: list[Path]) -> tuple[bool, str]:
    """Whether the parts can share one stream-copied picture join.

    Returns ``(ok, reason)``. A mismatch is fatal: the picture is never
    re-encoded here, so joining different profiles would produce a file that
    decodes wrong (or not at all) past the first seam.
    """
    if not parts:
        return False, "no parts"
    ref = probe_video_params(parts[0])
    if ref is None:
        return False, f"cannot probe video params of {parts[0].name}"
    for p in parts[1:]:
        got = probe_video_params(p)
        if got is None:
            return False, f"cannot probe video params of {p.name}"
        if got != ref:
            return False, f"{p.name} video params {got} != {ref}"
    return True, ""


def _audio_rebuild_graph(
    parts: list[Path],
    durations: list[float],
    has_audio: list[bool],
) -> str:
    """Per-part decode -> picture-exact trim/pad -> concat filter -> one encode.

    Each part is decoded on its own (a standalone decode honours that file's
    AAC edit list, i.e. its encoder delay is trimmed), then trimmed AND padded
    to that part's exact picture length. No overlap, no crossfade: the picture
    is a hard cut, so the audio must be too, or the joined track plays early by
    the overlap.
    """
    bits: list[str] = []
    labels: list[str] = []
    for i, dur in enumerate(durations):
        label = f"[af{i}]"
        if has_audio[i]:
            # Input 0 is the concat list (picture only); each part is a
            # separate input starting at 1, so the audio stream of part ``i``
            # is ``[i+1:a]``.
            bits.append(
                f"[{i + 1}:a]aresample=48000,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"atrim=end={dur:.6f},asetpts=PTS-STARTPTS,"
                f"apad=whole_dur={dur:.6f},atrim=end={dur:.6f}{label}"
            )
        else:
            # A silent part still has to occupy its own picture length.
            bits.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=end={dur:.6f},asetpts=PTS-STARTPTS{label}"
            )
        labels.append(label)
    bits.append(f"{''.join(labels)}concat=n={len(parts)}:v=0:a=1[aout]")
    return ";".join(bits)


def concat_parts(parts: list[Path], output: Path) -> Path:
    """Join ``parts`` in order into ``output`` in a single ffmpeg pass.

    The picture is always a stream copy (frame timestamps untouched) and the
    parts must share one video profile — a mismatch is fatal, never silently
    repaired.

    The audio is ALWAYS rebuilt per part on the picture timeline and encoded
    once. It is never stream-copied, even when every part is already AAC 48k
    stereo: matching codec settings do not prove that each part's encoder
    priming and packet tail line up with its picture length, and appending the
    packets re-inserts one encoder delay per join (measured +26 ms per round
    join on the concat path, 0.44 s late by round 18). Rebuilding is the only
    join that is exact by construction.
    """
    parts = [Path(p) for p in parts]
    output = Path(output)
    if not parts:
        raise SystemExit("[ERROR] assemble_final: no parts to join")
    for p in parts:
        if not p.is_file() or p.stat().st_size < 100_000:
            raise SystemExit(f"[ERROR] assemble_final: part missing/empty: {p}")

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".part")
    tmp.unlink(missing_ok=True)

    total_mb = sum(p.stat().st_size for p in parts) / 1024 / 1024
    _log(f"  [assemble] {len(parts)} part(s), {total_mb:.0f} MB total "
         f"(single pass)")

    ok, reason = video_join_compatible(parts)
    if not ok:
        # The picture is stream-copied, so a profile mismatch cannot be
        # repaired here. Re-encoding the picture would silently change the
        # delivered bitstream; refuse and let the caller re-render the
        # offending component at the export profile.
        raise SystemExit(
            f"ASSEMBLE_VIDEO_INCOMPATIBLE: {reason} — refusing to join "
            "components with different picture profiles (the picture is "
            "stream-copied, so it cannot be normalized in this pass)."
        )

    durations = [probe_picture_duration(p) for p in parts]
    try:
        audio_params = [probe_audio_params(p) for p in parts]
    except AudioProbeError as exc:
        # An unreadable audio probe is NOT a silent component. Treating it as
        # one would replace that part's sound with silence and still report a
        # successful join.
        raise SystemExit(
            f"ASSEMBLE_AUDIO_PROBE_FAILED: {exc} — refusing to join: an audio "
            "probe that could not be completed is not the same as a component "
            "with no audio."
        ) from exc
    has_audio = [params is not None for params in audio_params]
    unprobed = [p.name for p, d in zip(parts, durations) if d <= 0]
    if unprobed:
        # Without a frame-exact picture length there is no reference to trim
        # and pad each part's audio to, so the join cannot be made exact.
        raise SystemExit(
            "ASSEMBLE_AUDIO_UNPROBED: cannot read nb_frames/fps for "
            + ", ".join(unprobed)
            + " — refusing to join audio without a picture-exact reference."
        )
    graph = _audio_rebuild_graph(parts, durations, has_audio)
    _log("  [assemble] audio rebuilt per part on the picture timeline "
         "(one AAC encode)")

    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / "files.txt"
        lst.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
            encoding="utf-8",
        )
        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
        for p in parts:
            cmd += ["-i", str(p)]
        cmd += [
            "-filter_complex", graph,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
            # Deliberately NO ``-movflags +faststart``. On a 30-minute POV that
            # is a full-file rewrite of the ~13 GB output at the very end
            # (measured 5.5 min on a 1855 s body), which is exactly the kind of
            # extra pass this module exists to remove. The pre-existing
            # delivered layout has the moov atom at the end (the old
            # hook-prepend/outro-append passes did not move it), and the file
            # is uploaded to YouTube, which re-processes it.
            "-f", "mp4", str(tmp),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if r.returncode != 0 or not tmp.is_file():
            log_path = output.with_name(output.name + ".assemble.log")
            try:
                log_path.write_text(
                    f"returncode={r.returncode}\ncommand={json.dumps(cmd)}\n"
                    f"{r.stderr or ''}", encoding="utf-8")
            except OSError:
                log_path = None
            tmp.unlink(missing_ok=True)
            _log(f"[ERROR] assemble failed (rc={r.returncode}"
                 + (f"; full log: {log_path}" if log_path else "") + ")")
            for ln in (r.stderr or "").splitlines()[-8:]:
                _log(f"  ffmpeg: {ln[:300]}")
            sys.exit(1)

    tmp.replace(output)
    mb = output.stat().st_size / 1024 / 1024
    _log(f"  [assemble] OK -> {output.name} ({mb:.0f} MB)")
    return output
