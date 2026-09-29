"""Single source for every NVENC encode profile. CR-04.

Why this exists: `h264_nvenc` argument lists were hand-typed in 19 files. docs/agents/rendering.md
describes "two profiles" (mezzanine, final), but the code actually used **seven** distinct
combinations — CQ14 appeared in two delivered paths (`render_shorts`, `render_edit_timeline`) with
no documentation at all, and that is exactly the drift CR-12 caught in `assemble_reel`.

So instead of pretending there are two, every combination in use is **named here**, and each call
site splices it in with ``*encode.codec_args(encode.<NAME>)``. A new number must be added here, in
one reviewable place, instead of being typed into whichever command needed it.

These are the codec-side arguments only (`-c:v` through the rate control). Sites keep their own
`-profile:v` / `-pix_fmt` / `-level` / colour tags / `-g` / audio, because that shape genuinely
differs per command and is not the drifting part.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NVENC:
    """One rate-control combination.

    ``const_bitrate`` mirrors whether the original command carried ``-b:v 0``. It is a field rather
    than always-on because two delivered paths (shorts, edit-timeline segments) did NOT have it, and
    adding it changes the encoder's rate-control mode — i.e. shipped output. Profiles reproduce the
    arguments that were really in use.
    """
    preset: str
    cq: int | None = None
    maxrate: str | None = None
    bufsize: str | None = None
    const_bitrate: bool = True

    def args(self) -> list[str]:
        out = ["-c:v", "h264_nvenc", "-preset", self.preset]
        if self.const_bitrate:
            out += ["-b:v", "0"]
        if self.cq is not None:
            out += ["-cq", str(self.cq)]
        if self.maxrate:
            out += ["-maxrate", self.maxrate]
        if self.bufsize:
            out += ["-bufsize", self.bufsize]
        return out


# ── inter-stage (mezzanine) ─────────────────────────────────────────────────────────────────────
# CAPTURE: what render_pov's CSDM config asks for. p7 = highest-quality preset, no cap (it feeds
# the concat step, so a cap here would only lose information).
CAPTURE = NVENC(preset="p7", cq=15)
# SCALE: concat_rounds' 1080p->1440p CUDA-Lanczos upscale pass. p5 because the input is already
# encoded, so a slower preset buys little; 200M cap so the upscale is not the bottleneck.
SCALE = NVENC(preset="p5", cq=8, maxrate="200M", bufsize="400M")
# FREEZE: overlay/lineup_freeze PiP clips. Same numbers as SCALE but p7 (short clips, cheap).
FREEZE = NVENC(preset="p7", cq=8, maxrate="200M", bufsize="400M")

# ── delivery ────────────────────────────────────────────────────────────────────────────────────
# FINAL: uploaded verbatim (YouTube copy + outro append are -c copy). 1440p text/UI edges are the
# most ringing-prone content, hence CQ15 with a 60M cap — well below YouTube's own re-encode, but
# not so low that the cap clips on busy motion.
FINAL = NVENC(preset="p7", cq=15, maxrate="60M", bufsize="120M")
# FINAL_NOCAP: the same quantiser without a rate cap, used by prepend/intermediate passes that are
# re-encoded again later (a cap there would bake in a generation loss).
FINAL_NOCAP = NVENC(preset="p7", cq=15)

# ── undocumented, kept deliberately pending measurement ─────────────────────────────────────────
# EDIT: highlights/render_edit_timeline segment renders. CQ14 with const-bitrate mode, as that site
# has always sent it (`-preset p7 -b:v 0 -cq 14`). NOT one of the two documented profiles — kept
# as-is rather than silently changed, because it is a delivered artifact (CR-12 sibling).
EDIT = NVENC(preset="p7", cq=14)

# EDIT_BARE: the two shorts/render_shorts sites. Both sit one flag away from EDIT and the difference
# is a per-call-site accident, not policy — captured here rather than smoothed over:
#   render_shorts:500  `-preset p7 -cq 14`                      (no -b:v)
#   render_shorts:580  `-preset p7 -cq 14 -b:v 0 -profile:v …`   (flag re-appended by hand, later order)
# The rate-control mode differs between them and between EDIT, so each site keeps exactly what it had.
EDIT_BARE = NVENC(preset="p7", cq=14, const_bitrate=False)

# ── platform / tooling ──────────────────────────────────────────────────────────────────────────
# BILIBILI: bilibili.tv's own ingest spec, deliberately not a YouTube profile.
BILIBILI = NVENC(preset="p4", maxrate="18M")
# PREVIEW: dev-only preview renders (overlay/speaker_rows_preview).
PREVIEW = NVENC(preset="p7")

BY_NAME = {
    "capture": CAPTURE, "scale": SCALE, "freeze": FREEZE, "final": FINAL,
    "final_nocap": FINAL_NOCAP, "edit": EDIT, "edit_bare": EDIT_BARE,
    "bilibili": BILIBILI, "preview": PREVIEW,
}


def codec_args(profile: NVENC) -> list[str]:
    """The codec-side argument list for a profile, ready to splice into an ffmpeg command."""
    return profile.args()


def args_for(name: str) -> list[str]:
    """Same, by profile name (for callers that select a profile dynamically)."""
    return BY_NAME[name].args()
