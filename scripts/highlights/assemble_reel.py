"""Assemble Highlight Reel: per-segment avatar overlay + crossfade concat.

Reads edit_timeline.json, takes the rendered segment MP4s (segments/seg-NNN-*.mp4),
bakes each segment's POV player avatar (transparent cutout + white outline,
bottom-centre — same treatment as render_shorts.py) into a 60fps intermediate,
then concatenates all segments with crossfade transitions (video xfade + audio
acrossfade) into a single reel.

Usage:
    python scripts/highlights/assemble_reel.py renders/hl-<stem>/edit_timeline.json
    python scripts/highlights/assemble_reel.py renders/hl-<stem>/edit_timeline.json --fade 0.5
    python scripts/highlights/assemble_reel.py renders/hl-<stem>/edit_timeline.json --no-avatar

Output:
    renders/hl-<stem>/reel.mp4        (final reel)
    renders/hl-<stem>/reel_tmp/       (60fps avatar-composited intermediates, resumable)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

import numpy as np  # noqa: E402

from PIL import Image, ImageFilter  # noqa: E402

from shorts.render_shorts import (  # noqa: E402
    AVATAR_DIR,
    AVATAR_EXTS,
)
from config import settings  # noqa: E402

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe

TARGET_FPS = 60
AVATAR_DEFAULT_HEIGHT = 336      # proportional to shorts' 600px on a 1920-tall canvas
AVATAR_BOTTOM_MARGIN = 0          # cutouts sit flush against the bottom edge
AVATAR_OUTLINE_WIDTH = 2
REEL_AVATAR_HEAD_ZONE_FRAC = 0.30  # top fraction of the subject bbox used to measure head width
REEL_AVATAR_BODY_FACTOR = 2.2      # crop = head + ~1.2x head-height of shoulders below it
REEL_AVATAR_BG_TOLERANCE = 24  # gentle flood-fill tolerance (30 eroded s1mple's dark hair/shoulders)
REEL_AVATAR_BG_FALLBACK_TOLERANCE = 20  # retry tolerance if the head-region sanity check fails
REEL_AVATAR_BG_CONFIDENT_DIST = 80  # L1 distance from corner bg below which a pixel is treated as subject & never removed
REEL_AVATAR_BG_SUBJECT_DIST = 60  # looser subject estimate used by the head-retention sanity check
REEL_AVATAR_BG_MAX_HEAD_LOSS = 0.12  # max fraction of the head subject lost before triggering the fallback
REEL_AVATAR_MAX_HEAD_COMPONENTS = 6  # head-region connected components above this = eroded -> fall back to FACEIT
FADE_DEFAULT = 0.4


def _dbg(label: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] [{label}] {msg}", flush=True)


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0.0
    return float(json.loads(r.stdout).get("format", {}).get("duration", 0))


def _probe_resolution(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return (0, 0)
    parts = r.stdout.strip().split(",")
    return (int(parts[0]), int(parts[1]))


def _has_audio(path: Path) -> bool:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and r.stdout.strip() != ""


def _resolve_nickname(steam_id: str) -> str:
    """Look up a steam_id -> nickname from player_accounts.json."""
    accounts_path = _PROJECT_ROOT / ".data" / "player_accounts.json"
    try:
        accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
        for a in accounts:
            if a.get("steam_id") == steam_id and a.get("nickname"):
                return a["nickname"]
    except (OSError, json.JSONDecodeError):
        pass
    return "player"


def _avatar_bg_quality_ok(img: Image.Image) -> bool:
    """True if background removal yields a clean, non-fragmented head.

    A player whose subject is close in colour to the background (e.g. s1mple's
    dark-on-dark HLTV avatar) gets its head/silhouette eroded into many pieces by
    the flood-fill. We count connected components in the head region — a high
    count means the removal is unreliable, so we fall back to a cleaner avatar
    rather than baking a holey cutout.
    """
    a = np.array(_remove_photo_background(img))
    op = a[:, :, 3] > 40
    rows = np.where(op.any(1))[0]
    if len(rows) == 0:
        return False
    top = int(rows.min())
    hz = op[top : int(top + 0.45 * (rows.max() - top + 1))]
    if not hz.any():
        return False
    from scipy import ndimage
    return int(ndimage.label(hz)[1]) <= REEL_AVATAR_MAX_HEAD_COMPONENTS


def _is_already_transparent(path: Path) -> bool:
    """True when an avatar file already carries real transparency (a bodyshot PNG)."""
    try:
        im = Image.open(path).convert("RGBA")
        return bool((np.array(im)[:, :, 3] < 255).any())
    except Exception:
        return False


def _resolve_avatar_cutout(nickname: str) -> Path | None:
    """Best avatar for a nickname — HLTV preferred, FACEIT fallback.

    Mirrors render_shorts._resolve_avatar_path: the HLTV folder wins even when
    it only holds opaque JPGs. But if the HLTV avatar's background removal is
    unreliable (e.g. s1mple's dark-on-dark JPG, which erodes into many pieces),
    we fall back to a clean transparent FACEIT avatar instead of baking a bad
    cutout. Within a folder pick the largest image by pixel area. Returns None
    when the player has no avatar folder at all (e.g. randos), in which case no
    cutout overlay is baked.
    """
    # Normalize: strip whitespace, lowercase, drop trailing separators
    # (e.g. "Senzu-" -> "senzu") so the avatar folder lookup matches.
    name = nickname.strip().lower().rstrip("-_ ")

    def _best_in(folder: Path) -> Path | None:
        if not folder.is_dir():
            return None
        cands: list[Path] = []
        for ext in AVATAR_EXTS:
            cands.extend(folder.glob(f"{name}*.{ext.lstrip('.')}"))
        best: Path | None = None
        best_area = -1
        for p in cands:
            try:
                w, h = Image.open(p).size
            except Exception:
                continue
            area = w * h
            if area > best_area:
                best, best_area = p, area
        return best

    hl = _best_in(AVATAR_DIR / name / "hltv")
    if hl is not None:
        # An already-transparent avatar (a proper HLTV bodyshot PNG) needs no
        # background removal, so it's inherently clean — use it directly without
        # the fragmentation check (that check only applies to opaque JPGs).
        if _is_already_transparent(hl):
            return hl
        try:
            if _avatar_bg_quality_ok(Image.open(hl)):
                return hl
        except Exception:
            return hl  # never fail hard on a quality probe
        # HLTV removal was unreliable (fragmented head) — fall back to FACEIT.
        f = _best_in(AVATAR_DIR / name / "faceit")
        if f is not None:
            _dbg("avatar", f"HLTV removal unreliable for {name} - using FACEIT avatar")
            return f
    return hl  # best effort: no usable FACEIT either



def _segment_files(timeline: Path) -> list[tuple[dict, Path]]:
    """Ordered (segment, mp4 path) pairs for the edit timeline."""
    tl = json.loads(timeline.read_text(encoding="utf-8"))
    segments = tl["segments"]
    seg_dir = timeline.parent / "segments"
    pairs = []
    for i, seg in enumerate(segments, start=1):
        name = (
            f"seg-{i:03d}-pov-{seg.get('pov_steam_id', 'unknown')}"
            f"-tick-{seg['start_tick']}-to-{seg['end_tick']}.mp4"
        )
        p = seg_dir / name
        if not p.is_file():
            raise FileNotFoundError(f"Segment file missing: {p}")
        pairs.append((seg, p))
    return pairs


def _bg_flood_mask(a, tol: int) -> np.ndarray:
    """Border-connected background mask: pixels within ``tol`` (max per-channel
    distance from the corner colour) reachable from the image border."""
    h, w = a.shape[:2]
    bg = a[0, 0, :3].astype(int)

    def near(px):
        return int(np.abs(px[:3].astype(int) - bg).max()) <= tol

    from collections import deque
    mask = np.zeros((h, w), bool)
    seed = deque()
    for y in range(h):
        for x in (0, w - 1):
            if near(a[y, x]):
                seed.append((y, x)); mask[y, x] = True
    for x in range(w):
        for y in (0, h - 1):
            if near(a[y, x]):
                seed.append((y, x)); mask[y, x] = True
    while seed:
        y, x = seed.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and not mask[ny, nx] and near(a[ny, nx]):
                mask[ny, nx] = True
                seed.append((ny, nx))
    return mask


def _remove_photo_background(img: Image.Image) -> Image.Image:
    """Flood-fill from the borders to make an opaque photo/JPG avatar transparent.

    JPG avatars (e.g. HLTV's) have a solid/gradient background with no alpha.
    We grow a transparent region inward from the border, BUT pixels that are
    clearly the subject (L1 distance from the corner colour > ``CONFIDENT_DIST``)
    are never removed — this stops the fill from eating faces/shoulders when the
    background is messy (s1mple's gradient background lost 40% of his face at the
    old tolerance). A head-region sanity check retries far more conservatively if
    too much of the head would still be lost. Images that already carry alpha
    (transparent PNGs) are returned unchanged. Local to the reel workflow.
    """
    rgba = img.convert("RGBA")
    a = np.array(rgba)
    if (a[:, :, 3] < 255).any():
        return rgba  # already has transparency — leave it alone
    corner = a[0, 0, :3].astype(int)
    dist = np.abs(a[:, :, :3].astype(int) - corner).sum(2)
    confident = dist > REEL_AVATAR_BG_CONFIDENT_DIST  # protected subject core

    mask = _bg_flood_mask(a, REEL_AVATAR_BG_TOLERANCE) & ~confident

    # Sanity check: how much of the (looser) head subject would be lost?
    subject = dist > REEL_AVATAR_BG_SUBJECT_DIST
    rows = np.where(subject.any(1))[0]
    if len(rows) and not np.all(np.invert(mask)):
        top = int(rows.min())
        hz = subject[top : int(top + 0.4 * (rows.max() - top + 1))]
        if hz.any():
            removed = (hz & mask[top : int(top + 0.4 * (rows.max() - top + 1))]).sum()
            if removed / max(1, hz.sum()) > REEL_AVATAR_BG_MAX_HEAD_LOSS:
                _dbg("avatar", "bg-removal head-loss too high - retrying conservatively")
                mask = _bg_flood_mask(a, REEL_AVATAR_BG_FALLBACK_TOLERANCE) & ~confident

    a = a.copy()
    a[:, :, 3] = np.where(mask, 0, a[:, :, 3])
    return Image.fromarray(a)


def _bake_outline(img: Image.Image, outline_width: int, work: int) -> Image.Image:
    """Grow a smooth white outline around the alpha silhouette at ``work``-supersampled res."""
    a_out = np.array(img.getchannel("A"), dtype=np.int32)
    kernel = (outline_width * work) * 2 + 1
    grown = np.array(
        img.getchannel("A").filter(ImageFilter.MaxFilter(kernel)), dtype=np.int32
    )
    ring = np.clip(grown - a_out, 0, 255).astype(np.uint8)
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    white.putalpha(Image.fromarray(ring))
    return Image.alpha_composite(white, img)


def _prepare_reel_avatar_overlay(
    avatar_path: Path,
    target_height: int,
    outline_width: int = AVATAR_OUTLINE_WIDTH,
    dst: Path | None = None,
) -> Path:
    """Reel-specific cutout: transparent bg, outline the FULL avatar, then cut.

    Order follows the spec: the avatar is outlined as a whole FIRST, then cut to
    the head+shoulders region, then placed bottom-centre. Cutting before
    outlining would wrap the cut edge in a white line across the bottom of the
    cutout; outlining first means the crop simply slices through the already-
    outlined body, so there's no bottom line. The head+shoulders crop keeps every
    player's face the same size on screen even though source HLTV avatars have
    wildly different framing. Local to the reel workflow (shorts untouched).
    """
    img = _remove_photo_background(Image.open(avatar_path).convert("RGBA"))
    display_h = max(1, target_height // 2)
    WORK = 2  # supersample factor: outline rendered 2x, then downscaled to smooth it

    a = np.array(img)
    sub = a[:, :, 3] > 40
    if not sub.any():
        # No visible subject — plain centre-scaled cutout.
        img = img.resize((max(1, display_h), max(1, display_h)), Image.LANCZOS)
        if outline_width > 0:
            img = _bake_outline(img, outline_width, 1)
        out = dst or avatar_path.with_name(f"{avatar_path.stem}_{target_height}px_reel.png")
        img.save(out)
        return out

    rows = np.where(sub.any(1))[0]
    top = int(rows.min())
    bottom = int(rows.max())
    sub_h = bottom - top + 1
    # Head width = widest subject row within the top head-zone of the subject.
    hz_end = min(bottom + 1, top + max(1, int(round(sub_h * REEL_AVATAR_HEAD_ZONE_FRAC))))
    head_w = 0
    for y in range(top, hz_end):
        wc = np.where(sub[y])[0]
        if len(wc):
            head_w = max(head_w, int(wc.max() - wc.min() + 1))
    head_w = max(1, head_w)
    # Crop box (source coords): head + body_factor*head below, small headroom above.
    crop_bottom = min(bottom + 1, top + int(round(head_w * REEL_AVATAR_BODY_FACTOR)))
    crop_top = max(0, top - max(1, int(round(head_w * 0.12))))
    crop_h = crop_bottom - crop_top

    # 1. Scale the FULL avatar to a working resolution where the crop is display_h*WORK tall.
    scale = (display_h * WORK) / max(1, crop_h)
    img = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    # 2. Outline the FULL avatar FIRST.
    if outline_width > 0:
        img = _bake_outline(img, outline_width, WORK)
    # 3. Cut to head+shoulders (scaled crop box) — a clean slice through the
    #    already-outlined body, so no horizontal outline line across the bottom.
    cs = int(round(crop_top * scale))
    ce = int(round(crop_bottom * scale))
    img = img.crop((0, max(0, cs), img.width, min(img.height, ce)))
    # 4. Small transparent margin + downscale to the final display size.
    pad = (outline_width + 1) * WORK
    canvas = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    canvas.paste(img, (pad, pad), img)
    img = canvas.resize(
        (max(1, canvas.width // WORK), max(1, canvas.height // WORK)), Image.LANCZOS
    )

    out = dst or avatar_path.with_name(f"{avatar_path.stem}_{target_height}px_reel.png")
    img.save(out)
    return out


def _composite_pass(
    pairs: list[tuple[dict, Path]],
    work_dir: Path,
    avatar_map: dict[str, Path | None],
    avatar_height: int,
    avatar_bottom_margin: int,
    avatar_outline_width: int,
) -> list[Path]:
    """Bake avatar + 60fps into per-segment intermediates (resumable)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    overlay_cache: dict[str, Path] = {}

    # Probe canvas size from the first segment (all segments are same res)
    first = pairs[0][1]
    w, h = _probe_resolution(first)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"Cannot probe resolution of {first}")
    _dbg("composite", f"canvas {w}x{h}, {len(pairs)} segments, target {TARGET_FPS}fps")

    outs = []
    for i, (seg, src) in enumerate(pairs, start=1):
        dst = work_dir / f"seg-{i:03d}.mp4"
        if dst.is_file() and dst.stat().st_size >= 1_048_576:
            _dbg("composite", f"[SKIP] {dst.name} exists")
            outs.append(dst)
            continue

        nick = _resolve_nickname(seg.get("pov_steam_id", ""))
        avatar_src = avatar_map.get(nick)
        cmd = [FFMPEG, "-y", "-i", str(src)]

        if avatar_src is not None:
            overlay = overlay_cache.get(nick)
            if overlay is None:
                overlay = _prepare_reel_avatar_overlay(
                    avatar_src, avatar_height,
                    outline_width=avatar_outline_width,
                    dst=work_dir / f"_avatar_{nick}.png",
                )
                overlay_cache[nick] = overlay
                _dbg("avatar", f"{nick}: {avatar_src.name} -> {overlay.name}")
            cmd.extend(["-i", str(overlay)])
            vf = (
                "[0:v]fps={fps},settb=AVTB,setpts=PTS-STARTPTS[v];"
                "[v][1:v]overlay=(main_w-overlay_w)/2:"
                "(main_h-overlay_h-{margin}):format=auto,format=yuv420p[out]"
            ).format(fps=TARGET_FPS, margin=avatar_bottom_margin)
        else:
            _dbg("avatar", f"{nick}: no cutout found - no overlay for seg {i:03d}")
            vf = "[0:v]fps={fps},settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[out]".format(
                fps=TARGET_FPS
            )

        cmd += [
            "-filter_complex", vf,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "13",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "4.2",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(
                f"composite pass failed for {src.name} (rc={r.returncode}): {r.stderr[-2000:]}"
            )
        _dbg("composite", f"seg {i:03d}: {src.name} -> {dst.name} "
                          f"({dst.stat().st_size / 1e6:.0f} MB, nick={nick})")
        outs.append(dst)
    return outs


def _xfade_pass(
    intermediates: list[Path],
    output: Path,
    fade: float,
    has_audio: bool,
) -> None:
    """Concatenate intermediates with crossfade transitions."""
    n = len(intermediates)
    durations = [_probe_duration(p) for p in intermediates]
    total = sum(durations) - (n - 1) * fade
    _dbg("xfade", f"{n} clips, fade={fade}s, estimated reel: {total:.1f}s "
                  f"({total / 60:.1f} min)")

    cmd = [FFMPEG, "-y"]
    for p in intermediates:
        cmd.extend(["-i", str(p)])

    parts = []
    video_labels = []
    for i in range(n):
        parts.append(
            f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}]"
        )
        video_labels.append(f"[v{i}]")
    if has_audio:
        for i in range(n):
            parts.append(
                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]"
            )

    # Video: chain of xfades. Offset k = sum(dur_0..k-1) - k*fade.
    cur = f"[v0]"
    offset = 0.0
    for k in range(1, n):
        offset += durations[k - 1] - fade
        out_lbl = f"[x{k}]" if k < n - 1 else "[vout]"
        parts.append(
            f"{cur}[v{k}]xfade=transition=fade:duration={fade}:offset={offset:.6f}{out_lbl}"
        )
        cur = out_lbl

    if has_audio:
        cur_a = "[a0]"
        for k in range(1, n):
            out_lbl = f"[ax{k}]" if k < n - 1 else "[aout]"
            parts.append(f"{cur_a}[a{k}]acrossfade=d={fade}{out_lbl}")
            cur_a = out_lbl

    cmd += ["-filter_complex", ";".join(parts)]
    cmd += ["-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += [
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "14",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "4.2",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output),
    ]

    t0 = time.time()
    _dbg("xfade", "encoding reel (single pass)...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    elapsed = time.time() - t0
    if r.returncode != 0:
        raise RuntimeError(f"xfade pass failed (rc={r.returncode}): {r.stderr[-3000:]}")
    _dbg("xfade", f"reel encoded in {elapsed:.0f}s -> {output.name} "
                  f"({output.stat().st_size / 1e9:.2f} GB)")


def assemble_reel(
    timeline_path: Path,
    fade: float = FADE_DEFAULT,
    avatar: bool = True,
    avatar_height: int = AVATAR_DEFAULT_HEIGHT,
    avatar_bottom_margin: int = AVATAR_BOTTOM_MARGIN,
    avatar_outline_width: int = AVATAR_OUTLINE_WIDTH,
) -> Path:
    timeline_path = timeline_path.resolve()
    pairs = _segment_files(timeline_path)
    base_out = timeline_path.parent
    work_dir = base_out / "reel_tmp"
    output = base_out / "reel.mp4"

    print(f"Assemble Reel: {len(pairs)} segments from {timeline_path.parent.name}")
    print(f"Output: {output}")

    avatar_map: dict[str, Path | None] = {}
    if avatar:
        for seg, _ in pairs:
            nick = _resolve_nickname(seg.get("pov_steam_id", ""))
            if nick not in avatar_map:
                avatar_map[nick] = _resolve_avatar_cutout(nick)
        found = sum(1 for v in avatar_map.values() if v is not None)
        _dbg("avatar", f"cutouts resolved: {found}/{len(avatar_map)} "
                       f"({', '.join(avatar_map) or '-'})")

    intermediates = _composite_pass(
        pairs, work_dir, avatar_map,
        avatar_height, avatar_bottom_margin, avatar_outline_width,
    )

    has_audio = all(_has_audio(p) for p in intermediates)
    if not has_audio:
        _dbg("xfade", "some intermediates lack audio - reel will be video-only")

    _xfade_pass(intermediates, output, fade, has_audio)

    dur = _probe_duration(output)
    w, h = _probe_resolution(output)
    print(f"\nDone. Reel: {output} ({w}x{h}, {dur:.1f}s)")
    return output


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble Highlight Reel (avatar overlay + crossfades)")
    ap.add_argument("edit_timeline", type=Path, help="Path to edit_timeline.json")
    ap.add_argument("--fade", type=float, default=FADE_DEFAULT,
                    help=f"Crossfade duration in seconds (default: {FADE_DEFAULT})")
    ap.add_argument("--no-avatar", action="store_true",
                    help="Skip the per-segment avatar overlay (bottom-centre)")
    ap.add_argument("--avatar-height", type=int, default=AVATAR_DEFAULT_HEIGHT,
                    help=f"Avatar target height in px on the 1080p canvas (default: {AVATAR_DEFAULT_HEIGHT})")
    ap.add_argument("--avatar-bottom-margin", type=int, default=AVATAR_BOTTOM_MARGIN,
                    help=f"Clearance from the bottom edge in px (default: {AVATAR_BOTTOM_MARGIN})")
    ap.add_argument("--avatar-outline-width", type=int, default=AVATAR_OUTLINE_WIDTH,
                    help=f"White outline width around the avatar in px (default: {AVATAR_OUTLINE_WIDTH}; 0 = none)")
    args = ap.parse_args()

    try:
        assemble_reel(
            args.edit_timeline,
            fade=args.fade,
            avatar=not args.no_avatar,
            avatar_height=args.avatar_height,
            avatar_bottom_margin=args.avatar_bottom_margin,
            avatar_outline_width=args.avatar_outline_width,
        )
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
