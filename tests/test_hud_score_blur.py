"""Full-HUD policy for Shorts + Hook: compact alive count, score blur.

Covers ``cs2archive/hud_score_blur.py`` and its two call sites —
``shorts/render_shorts.py`` (CSDM cfg + 9:16 composite) and
``pov/render_hook.py`` + ``pov/assemble_hook.py`` (CSDM cfg + assembly).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from cs2archive.hud_score_blur import (
    COMPACT_PLAYERCOUNT_CFG,
    HUD_POLICY,
    score_blur_filter,
)


def _patch_demo_parser():
    """Stub the demo roster parse the spec-lock block needs.

    These tests write a 1-byte placeholder ``.dem``; the real demoparser2
    panics on it, so the roster (steamid -> in-game name) is faked instead.
    """
    parser = MagicMock()
    parser.parse_player_info.return_value.to_dict.return_value = [
        {"steamid": 1, "name": "n"},
        {"steamid": 123, "name": "n"},
    ]
    return patch("demoparser2.DemoParser", return_value=parser)


def test_compact_playercount_uses_repo_proven_cvar():
    assert "cl_teamcounter_playercount_instead_of_avatars 1" in COMPACT_PLAYERCOUNT_CFG
    # Cheat-gated display cvars are silent no-ops without the HLAE
    # force-apply (flameZ 4K shipped the full avatar bar without it).
    assert "mirv_cvar_hack cl_teamcounter_playercount_instead_of_avatars 1" in COMPACT_PLAYERCOUNT_CFG


def test_score_blur_filter_blurs_score_row():
    fc = score_blur_filter("0:v", "sbase", 1728, 1080, tag="t0")
    assert "boxblur=" in fc
    assert "overlay=" in fc
    assert "[sbase]" in fc
    assert "xfade" not in fc


def test_score_blur_box_matches_measured_geometry():
    from cs2archive.hud_score_blur import score_blur_box
    x, y, w, h = score_blur_box(1728, 1080)
    # NiKo Mirage source: score-cell dividers center-38 / center / center+38.
    # Neighboring TeSeS portrait ends at the left divider; alive counts sit below.
    assert (x, y, w, h) == (1728 // 2 - 38, 34, 76, 36)
    assert x <= 1728 // 2 - 32 and x + w >= 1728 // 2 + 28
    assert y <= 42 and y + h >= 58


def test_score_blur_filter_single_stage_no_extra_labels():
    a = score_blur_filter("0:v", "out_a", 1920, 1080, tag="aa")
    b = score_blur_filter("0:v", "out_b", 1920, 1080, tag="bb")
    # Strong boxblur per box; output terminates at [out].
    assert a.count("boxblur=") == 1 and b.count("boxblur=") == 1
    assert a.rsplit(";", 1)[-1].endswith("[out_a]")
    assert b.rsplit(";", 1)[-1].endswith("[out_b]")
    assert "overlay=" in a and "overlay=" in b


def test_shorts_csdm_config_uses_full_hud_with_showcount(tmp_path):
    from cs2archive.shorts.render_shorts import _build_csdm_config

    demo = tmp_path / "test.dem"
    demo.write_bytes(b"0")
    shorts = [{"short_type": "4k", "pov_steam_id": "123",
               "start_tick": 1000, "end_tick": 5000, "kill_ticks": []}]
    with patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars",
               return_value=[]), _patch_demo_parser():
        cfg = _build_csdm_config(shorts, demo, tmp_path)
    seq = cfg["sequences"][0]
    # Killfeed-only HUD: no score row, no team bar — which is why the score
    # blur is off by default (there is nothing to hide).
    assert "cl_draw_only_deathnotices 1" in seq["cfg"]
    assert "mirv_cvar_hack cl_draw_only_deathnotices 1" in seq["cfg"]
    # The cfg line is what lands last; CSDM's own field stays at its default
    # and must not be relied on for the HUD policy.


def test_full_bar_gutter_boxes_cover_digits_avoid_panels():
    from cs2archive.hud_score_blur import score_blur_box, score_blur_boxes
    boxes = score_blur_boxes(1440, 810, compact=False)
    assert len(boxes) == 2
    (ax, ay, aw, ah), (bx, by, bw, bh) = boxes
    # Same-size cells (user spec): identical dimensions, adjacent, shared top.
    assert (aw, ah) == (bw, bh)
    assert ax + aw == bx
    assert ay == by
    # Independently extracted flameZ frames: dividers x682/720/758,
    # top y34, fading bottom ~70. Avatar mode was subsequently confirmed
    # with an explicit cvar reset; score-cell bounds remained identical.
    assert boxes == [(682, 25, 38, 27), (720, 25, 38, 27)]
    assert score_blur_boxes(1440, 1080, compact=False) == [
        (682, 34, 38, 36), (720, 34, 38, 36)]
    # Digits at x688..712 / 733..748, y42..58; adjacent alive panels
    # outside x682..758 and timer glyphs above y27 must remain untouched.
    assert ax <= 688 and ax + aw >= 712
    assert bx <= 733 and bx + bw >= 748
    assert ay > round(27 * 810 / 1080)
    assert ay <= round(42 * 810 / 1080) and ay + ah >= round(58 * 810 / 1080)
    # Compact default unchanged (hook path).
    (cx, cy, cw, ch) = score_blur_boxes(1440, 810)[0]
    assert (cx, cy, cw, ch) == score_blur_box(1440, 810)


def test_native_avatar_cells_match_three_measured_sources():
    from cs2archive.hud_score_blur import score_blur_boxes

    # Independent 1920x1080 avatar sources: magixx Anubis (10/7),
    # tom1jed Dust2 (4/1), magixx Cache (4/0). Separator centers match.
    assert score_blur_boxes(1920, 1080, compact=False) == [
        (922, 34, 38, 36), (960, 34, 38, 36)]
    native = score_blur_boxes(1920, 1080, compact=False)
    stretched = score_blur_boxes(1440, 1080, compact=False)
    for (nx, ny, nw, nh), (sx, sy, sw, sh) in zip(native, stretched):
        assert nx - sx == (1920 - 1440) // 2
        assert (ny, nw, nh) == (sy, sw, sh)  # no width-proportional scaling
    fc = score_blur_filter("0:v", "out", 1920, 1080, compact=False)
    assert "crop=38:36:922:34" in fc
    assert "crop=38:36:960:34" in fc


def test_full_bar_calculated_bounds_for_all_capture_aspects():
    from cs2archive.capture_res import _ASPECTS, capture_size_for_aspect
    from cs2archive.hud_score_blur import score_blur_box, score_blur_boxes

    # Geometry contract only: 5:4/16:10/21:9/32:9 lack measured source
    # fixtures. Passing this test is NOT empirical in-game validation.
    expected_sizes = {
        "4:3": (1440, 1080), "5:4": (1350, 1080),
        "16:9": (1920, 1080), "16:10": (1728, 1080),
        "21:9": (1920, 822), "32:9": (1920, 540),
    }
    assert set(expected_sizes) == set(_ASPECTS)
    for aspect, expected in expected_sizes.items():
        w, h = capture_size_for_aspect(aspect, max_width=1920, max_height=1080)
        assert (w, h) == expected
        a, b = score_blur_boxes(w, h, compact=False)
        assert a == (w // 2 - 38, round(34 / 1080 * h),
                     38, round(36 / 1080 * h))
        assert b == (w // 2, a[1], a[2], a[3])
        for x, y, bw, bh in (a, b):
            assert 0 <= x < x + bw <= w
            assert 0 <= y < y + bh <= h
        # Compact path unchanged.
        assert score_blur_boxes(w, h) == [score_blur_box(w, h)]


def test_full_bar_filter_covers_two_gutters():
    fc = score_blur_filter("0:v", "sbase", 1440, 810, compact=False)
    assert fc.count("boxblur=") == 2
    assert fc.endswith("[sbase]")
    assert fc.count("overlay=") == 2
    # Even-aligned chroma-safe crops preserve the measured horizontal limits.
    assert "crop=38:28:682:24" in fc
    assert "crop=38:28:720:24" in fc


def _capture_shorts_filter(**kwargs) -> str:
    from cs2archive.shorts.render_shorts import _composite_9x16

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs_run):
        captured.append(cmd)
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(Path, "stat", return_value=MagicMock(st_size=2_000_000)):
        _composite_9x16(Path("src.mp4"), Path("dst.mp4"), **kwargs)
    cmd = captured[0]
    return cmd[cmd.index("-filter_complex") + 1]


def test_shorts_composite_skips_score_blur_by_default():
    # The shorts HUD is killfeed-only: no score row exists, so blurring the
    # digit gutters would smudge clear gameplay. Blur is opt-in only.
    fc = _capture_shorts_filter(kill_feed_path=Path("kf.mp4"))
    assert "boxblur=" not in fc


def test_shorts_composite_score_blur_opt_in():
    fc = _capture_shorts_filter(kill_feed_path=Path("kf.mp4"), score_blur=True)
    assert "boxblur=" in fc


def test_shorts_score_blur_follows_hud_policy():
    from cs2archive.shorts import render_shorts as rs

    # A full-HUD render is the only case with a score row to hide; the policy
    # constant and the blur default must never disagree.
    assert rs.HUD_SHOWS_SCORE is False
    assert rs.SHORTS_HUD_POLICY.endswith("-v2")


def test_hook_build_config_uses_full_hud(tmp_path):
    from cs2archive.pov.render_hook import build_config

    demo = tmp_path / "x.dem"
    demo.write_bytes(b"0")
    plan = [{"pov_steam_id": "123", "pov_nick": "donk", "label": "4k",
             "tier": "4k", "round": 1,
             "windows": [{"start_tick": 100, "end_tick": 200}]}]
    cfg = build_config(plan, demo, tmp_path, 1280, 960, 60,
                       player_cvars=["crosshair 1"])
    seq = cfg["sequences"][0]
    assert "cl_draw_only_deathnotices 0" in seq["cfg"]
    assert "cl_teamcounter_playercount_instead_of_avatars 1" in seq["cfg"]
    assert seq["showOnlyDeathNotices"] is False


def test_hook_assembly_blurs_score_by_default():
    from cs2archive.pov.assemble_hook import build_hook_filter

    fc = build_hook_filter([0, 2], [0, 2], end_fade=0.5, total=6.0,
                           width=2560, height=1440, fps=60.0)
    assert "boxblur=" in fc
    assert "concat=n=2:v=1:a=1" in fc
    assert "xfade" not in fc


def test_hook_assembly_score_blur_opt_out():
    from cs2archive.pov.assemble_hook import build_hook_filter

    fc = build_hook_filter([0], [0], end_fade=0.5, total=4.0,
                           width=2560, height=1440, fps=60.0,
                           score_blur=False)
    assert "boxblur=" not in fc
    assert "fade=t=out:st=3.500:d=0.5" in fc


def test_hud_policy_stamp_value():
    assert HUD_POLICY == "full-hud+showcount+scoreblur+nochat-v14"


def test_score_blur_box_valid_for_every_capture_aspect():
    from cs2archive.capture_res import _ASPECTS, capture_size_for_aspect
    from cs2archive.hud_score_blur import score_blur_box

    for aspect in _ASPECTS:
        w, h = capture_size_for_aspect(aspect)
        x, y, bw, bh = score_blur_box(w, h)
        assert 0 <= x and x + bw <= w, aspect
        assert 0 <= y and y + bh <= h, aspect
        # Box straddles frame centre (the team bar is centre-anchored).
        assert x < w // 2 < x + bw, aspect


def test_blur_radii_valid_for_every_box_and_aspect():
    import re
    from cs2archive.capture_res import _ASPECTS, capture_size_for_aspect
    from cs2archive.hud_score_blur import score_blur_filter

    for aspect in _ASPECTS:
        w, h = capture_size_for_aspect(aspect)
        for compact in (True, False):
            fc = score_blur_filter("0:v", "sbase", w, h, compact=compact)
            for m in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", fc):
                cw, ch, cx, cy = (int(g) for g in m.groups())
                assert cx % 2 == 0 and cy % 2 == 0, (aspect, compact)
                assert cw % 2 == 0 and ch % 2 == 0, (aspect, compact)
            for m in re.finditer(
                    r"boxblur=luma_radius=(\d+):luma_power=(\d+):"
                    r"chroma_radius=(\d+):chroma_power=(\d+)", fc):
                lr, lp, cr, cp = (int(g) for g in m.groups())
                assert 1 <= lr <= 13 and 1 <= cr <= 6, (aspect, compact)
                assert lp >= 1 and cp >= 1, (aspect, compact)


def test_chat_hide_cfg_covers_console_text():
    # cl_chatfilters / tv_relaytextchat / hud_saytext_time do not exist in CS2
    # (verified against the CS2 convar dump). The relayed "Console: ..." lines
    # are TextMsg HUD prints killed by cl_showtextmsg; hidehud 128 hides the
    # chat panel itself. tv_nochat only covers SourceTV spectator chat.
    # The display-side kill for the bottom-left message list is the CLIENT
    # timer cl_screenmessage_notifytime: screenmessage_show/hidehud are
    # cheat-locked no-ops in renders and the bare screenmessage_notifytime
    # (no cl_) is the server-side timer (wrong-cvar bug, 2026-10-07 — frames
    # proved the full set without the cl_ line still draws).
    from cs2archive.chat_hide import CHAT_HIDE_CFG

    assert "cl_showtextmsg 0" in CHAT_HIDE_CFG
    assert "hidehud 128" in CHAT_HIDE_CFG
    assert "cl_screenmessage_notifytime 0" in CHAT_HIDE_CFG
    assert "screenmessage_notifytime 0" not in CHAT_HIDE_CFG
    assert "cl_chatfilters 63" not in CHAT_HIDE_CFG
    # Cheat gate: sv_cheats must precede the cheat-gated display kills or
    # they are silently ignored (the old failure — frames proved it).
    assert CHAT_HIDE_CFG.index("sv_cheats 1") < CHAT_HIDE_CFG.index("hidehud 128")
    assert CHAT_HIDE_CFG.index("sv_cheats 1") < CHAT_HIDE_CFG.index("screenmessage_show 0")
    # HLAE force-apply bypasses cheat protection without needing sv_cheats.
    assert "mirv_cvar_hack hidehud 128" in CHAT_HIDE_CFG
    assert "mirv_cvar_hack screenmessage_show 0" in CHAT_HIDE_CFG


def test_chat_hide_wired_into_shorts_and_hook_cfgs(tmp_path):
    from cs2archive.chat_hide import CHAT_HIDE_CFG
    from cs2archive.shorts.render_shorts import _build_csdm_config
    from cs2archive.pov.render_hook import build_config

    demo = tmp_path / "x.dem"
    demo.write_bytes(b"0")
    with patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars",
               return_value=[]), _patch_demo_parser():
        cfg = _build_csdm_config(
            [{"short_type": "4k", "pov_steam_id": "1",
              "start_tick": 1, "end_tick": 200, "kill_ticks": []}],
            demo, tmp_path)
    for line in CHAT_HIDE_CFG:
        assert line in cfg["sequences"][0]["cfg"]

    plan = [{"pov_steam_id": "1", "pov_nick": "n", "label": "4k",
             "tier": "4k", "round": 1,
             "windows": [{"start_tick": 1, "end_tick": 200}]}]
    cfg = build_config(plan, demo, tmp_path, 1280, 960, 60, player_cvars=[])
    for line in CHAT_HIDE_CFG:
        assert line in cfg["sequences"][0]["cfg"]


def test_chat_hide_wired_into_pov_autoexec(tmp_path, monkeypatch):
    from cs2archive.chat_hide import CHAT_HIDE_CFG
    from cs2archive.pov import render_pov

    autoexec = tmp_path / "autoexec_render.cfg"
    xhair = tmp_path / "render_crosshair.cfg"
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", autoexec)
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", xhair)
    render_pov._write_render_autoexec([], None, None, False)
    content = autoexec.read_text(encoding="utf-8")
    for line in CHAT_HIDE_CFG:
        assert line in content
    assert "tv_relaytextchat 2" not in content
    assert "cl_chatfilters" not in content


def test_chat_hide_wired_into_highlights_cfg(tmp_path):
    from cs2archive.chat_hide import CHAT_HIDE_CFG
    from cs2archive.highlights import render_edit_timeline as ret

    demo = tmp_path / "x.dem"
    demo.write_bytes(b"0")
    segments = [{"pov_steam_id": "1", "start_tick": 1, "end_tick": 200}]
    with patch.object(ret, "_get_player_crosshair_cvars", return_value=[]):
        cfg = ret._build_csdm_config({"segments": segments}, demo, tmp_path,
                                     segments=segments)
    for line in CHAT_HIDE_CFG:
        assert line in cfg["sequences"][0]["cfg"]


def _shorts_timeline(tmp_path: Path):
    import json

    demo = tmp_path / "demos" / "faceit" / "test.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    demo.write_bytes(b"0")
    tl = tmp_path / "short_timeline.json"
    tl.write_text(json.dumps({
        "short_type": "short_timeline", "demo_path": str(demo), "map": "Mirage",
        "short_count": 1,
        "shorts": [{"short_type": "4k", "pov_steam_id": "1",
                    "start_tick": 100, "end_tick": 500, "kill_ticks": []}],
    }), encoding="utf-8")
    return tl


def test_shorts_full_render_swaps_launch_autoexec(tmp_path):
    from cs2archive.shorts.render_shorts import render_shorts

    tl = _shorts_timeline(tmp_path)
    with patch("cs2archive.shorts.render_shorts._run_csdm_hook_aware"), \
         patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars",
               return_value=[]), \
         patch("cs2archive.shorts.render_shorts._find_sequence_files",
               return_value=[]), \
         _patch_demo_parser(), \
         patch("cs2archive.pov.render_pov._write_render_autoexec") as mock_write, \
         patch("cs2archive.faceit.intro_prepend._swap_autoexec") as mock_swap, \
         patch("cs2archive.faceit.intro_prepend._restore_autoexec") as mock_restore:
        try:
            render_shorts(tl, batch_size=0)
        except RuntimeError:
            pass  # no sequence files — CSDM was mocked
    assert mock_write.called
    assert mock_swap.called
    assert mock_restore.called


def test_shorts_composite_only_leaves_autoexec_alone(tmp_path):
    from cs2archive.shorts.render_shorts import render_shorts

    tl = _shorts_timeline(tmp_path)
    (tmp_path / "segments").mkdir()
    with patch("cs2archive.pov.render_pov._write_render_autoexec") as mock_write, \
         patch("cs2archive.faceit.intro_prepend._swap_autoexec") as mock_swap, \
         patch("cs2archive.faceit.intro_prepend._restore_autoexec") as mock_restore, \
         patch("cs2archive.shorts.render_shorts._find_sequence_files",
               return_value=[]):
        try:
            render_shorts(tl, composite_only=True)
        except (RuntimeError, FileNotFoundError):
            pass  # no sequence files to composite (or no segments dir)
    assert not mock_write.called
    assert not mock_swap.called
    assert not mock_restore.called
