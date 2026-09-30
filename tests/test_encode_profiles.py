"""Golden test: every named encode profile reproduces the exact argument list it replaced.

CR-04. Why this exists: the first migration pass had a real bug. `EDIT` was built with
`const_bitrate=False` on the belief that "these two sites never passed `-b:v 0`" — that belief came
from reading the sites rather than from git, and it was wrong: `render_edit_timeline.py` had always
sent `-b:v 0`, so the migration silently dropped it, changing the rate-control mode of a delivered
artifact. A reviewer caught it with `git show`.

These lists are the pre-migration arguments, captured from `HEAD` (the commit before CR-04 touched
them). If a profile changes, this test fails and the change has to be deliberate.
"""
from __future__ import annotations

from cs2archive import encode

# GHOST PROFILE: the bug above, pinned so it cannot come back. EDIT must carry -b:v 0.
def test_edit_profile_keeps_const_bitrate_mode():
    assert encode.codec_args(encode.EDIT) == [
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "14",
    ]


def test_edit_bare_profile_omits_const_bitrate_mode():
    # shorts/render_shorts sent no -b:v at either site (one re-appended it by hand, later in order).
    assert encode.codec_args(encode.EDIT_BARE) == [
        "-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14",
    ]


def test_edit_and_edit_bare_are_distinguishable():
    # the whole point of two profiles: the sites disagree, so the profiles must too
    assert encode.codec_args(encode.EDIT) != encode.codec_args(encode.EDIT_BARE)


def test_final_profile_matches_the_documented_delivery_profile():
    assert encode.codec_args(encode.FINAL) == [
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "15",
        "-maxrate", "60M", "-bufsize", "120M",
    ]


def test_scale_profile_matches_the_mezzanine_upscale_pass():
    # concat_rounds' 1080p -> 1440p pass; note it gained a bufsize the site never sent, which is
    # inert for a CQ+maxrate encode but is recorded here rather than hidden.
    assert encode.codec_args(encode.SCALE) == [
        "-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "0", "-cq", "8",
        "-maxrate", "200M", "-bufsize", "400M",
    ]


def test_every_named_profile_is_reachable_by_name():
    for name, profile in encode.BY_NAME.items():
        assert encode.args_for(name) == encode.codec_args(profile)


def test_dissolve_profile_mirrors_scale():
    # concat_rounds' obvious-defuse junction pass: same situation as SCALE
    # (already-encoded input, re-encoded again by scale + overlay after), so
    # the same numbers under a distinct name. Pinned so drift shows up here.
    assert encode.codec_args(encode.DISSOLVE) == encode.codec_args(encode.SCALE)
