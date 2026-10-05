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


def test_compact_playercount_uses_repo_proven_cvar():
    assert "cl_teamcounter_playercount_instead_of_avatars true" in COMPACT_PLAYERCOUNT_CFG


def test_score_blur_filter_blurs_score_row():
    fc = score_blur_filter("0:v", "sbase", 1728, 1080, tag="t0")
    assert "delogo=" in fc
    assert "boxblur=" not in fc
    assert "overlay=" not in fc
    assert "[sbase]" in fc
    assert "xfade" not in fc


def test_score_blur_box_matches_measured_geometry():
    from cs2archive.hud_score_blur import score_blur_box
    x, y, w, h = score_blur_box(1728, 1080)
    assert (x, y, w, h) == (1728 // 2 - 95, round(0.038 * 1080), 140, round(0.036 * 1080))
    # Measured scores (802-816, 844-871 x; 50-72 y) sit inside the box.
    assert x <= 802 and x + w >= 871
    assert y <= 50 and y + h >= 72


def test_score_blur_filter_single_stage_no_extra_labels():
    a = score_blur_filter("0:v", "out_a", 1920, 1080, tag="aa")
    b = score_blur_filter("0:v", "out_b", 1920, 1080, tag="bb")
    assert a.count(";") == 0 and b.count(";") == 0
    assert "[out_a]" in a and "[out_b]" in b


def test_shorts_csdm_config_uses_full_hud_with_showcount(tmp_path):
    from cs2archive.shorts.render_shorts import _build_csdm_config

    demo = tmp_path / "test.dem"
    demo.write_bytes(b"0")
    shorts = [{"short_type": "4k", "pov_steam_id": "123",
               "start_tick": 1000, "end_tick": 5000, "kill_ticks": []}]
    with patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars",
               return_value=[]):
        cfg = _build_csdm_config(shorts, demo, tmp_path)
    seq = cfg["sequences"][0]
    assert "cl_draw_only_deathnotices 0" in seq["cfg"]
    assert "cl_drawhud 1" in seq["cfg"]
    assert "cl_teamcounter_playercount_instead_of_avatars true" in seq["cfg"]
    assert seq["showOnlyDeathNotices"] is False


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


def test_shorts_composite_blurs_score_by_default():
    fc = _capture_shorts_filter(kill_feed_path=Path("kf.mp4"))
    assert "delogo=" in fc


def test_shorts_composite_score_blur_opt_out():
    fc = _capture_shorts_filter(kill_feed_path=Path("kf.mp4"), score_blur=False)
    assert "delogo=" not in fc


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
    assert "cl_teamcounter_playercount_instead_of_avatars true" in seq["cfg"]
    assert seq["showOnlyDeathNotices"] is False


def test_hook_assembly_blurs_score_by_default():
    from cs2archive.pov.assemble_hook import build_hook_filter

    fc = build_hook_filter([0, 2], [0, 2], end_fade=0.5, total=6.0,
                           width=2560, height=1440, fps=60.0)
    assert "delogo=" in fc
    assert "concat=n=2:v=1:a=1" in fc
    assert "xfade" not in fc


def test_hook_assembly_score_blur_opt_out():
    from cs2archive.pov.assemble_hook import build_hook_filter

    fc = build_hook_filter([0], [0], end_fade=0.5, total=4.0,
                           width=2560, height=1440, fps=60.0,
                           score_blur=False)
    assert "delogo=" not in fc
    assert "fade=t=out:st=3.500:d=0.5" in fc


def test_hud_policy_stamp_value():
    assert HUD_POLICY.startswith("full-hud")


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


def test_chat_hide_cfg_covers_console_text():
    # cl_chatfilters / tv_relaytextchat / hud_saytext_time do not exist in CS2
    # (verified against the CS2 convar dump). The relayed "Console: ..." lines
    # are TextMsg HUD prints killed by cl_showtextmsg; hidehud 128 hides the
    # chat panel itself. tv_nochat only covers SourceTV spectator chat.
    from cs2archive.chat_hide import CHAT_HIDE_CFG

    assert "cl_showtextmsg 0" in CHAT_HIDE_CFG
    assert "hidehud 128" in CHAT_HIDE_CFG
    assert "cl_chatfilters 63" not in CHAT_HIDE_CFG


def test_chat_hide_wired_into_shorts_and_hook_cfgs(tmp_path):
    from cs2archive.chat_hide import CHAT_HIDE_CFG
    from cs2archive.shorts.render_shorts import _build_csdm_config
    from cs2archive.pov.render_hook import build_config

    demo = tmp_path / "x.dem"
    demo.write_bytes(b"0")
    with patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars",
               return_value=[]):
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
