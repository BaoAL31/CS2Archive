"""The hook must render the POV's crosshair, not a second crosshair system.

Bug this pins: `render_hook.HOOK_CFG_LINES` was a hand-rolled cfg that never
loaded `assets/cs2_pov.cfg`, so hook renders went out without
``cl_show_observer_crosshair 0``. CS2 then draws the *observer* crosshair during
demo playback instead of the player's cvars — the POV render (which does load the
base cfg) looked right while every hook showed the wrong crosshair.
"""

from __future__ import annotations

from cs2archive.pov.render_hook import hook_cfg_lines
from cs2archive.pov.render_pov import _sequence_cfg

PLAYER = ["cl_crosshair_length 3", "cl_crosshair_gap 2", "cl_crosshaircolor_g 229"]


def test_hook_cfg_shares_the_pov_base() -> None:
    hook = hook_cfg_lines(PLAYER).splitlines()
    pov = _sequence_cfg(PLAYER).splitlines()

    for probe in ("crosshair 1", "cl_show_observer_crosshair 0"):
        assert probe in hook, f"{probe} missing from the hook cfg"
        assert probe in pov


def test_player_crosshair_wins_last_in_both() -> None:
    hook = hook_cfg_lines(PLAYER).splitlines()
    pov = _sequence_cfg(PLAYER).splitlines()

    assert hook[-1] == PLAYER[-1]
    assert pov[-1] == PLAYER[-1]
    # Nothing may re-override the crosshair after the player's cvars.
    after = hook[hook.index(PLAYER[-1]) + 1:]
    assert not any(line.startswith("cl_crosshair") for line in after)


def test_hook_keeps_its_hud_delta() -> None:
    hook = hook_cfg_lines(PLAYER).splitlines()

    assert hook[0] == "cl_draw_only_deathnotices 0"
    assert "cl_drawhud 1" in hook
    assert "cl_showtextmsg 0" in hook  # CHAT_HIDE_CFG
    # The hook's HUD lines must not resurrect the wide avatar row.
    assert "cl_teamcounter_playercount_instead_of_avatars true" in hook


def test_hook_cfg_carries_rename_lines_once() -> None:
    hook = hook_cfg_lines(PLAYER, {"123": "flameZ"}).splitlines()

    assert hook.count('mirv_replace_name byXuid add x123 "flameZ"') == 1


def test_autoexec_disables_observer_crosshair(tmp_path, monkeypatch) -> None:
    from cs2archive.pov import render_pov

    autoexec = tmp_path / "autoexec_render.cfg"
    render_crosshair = tmp_path / "render_crosshair.cfg"
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", autoexec)
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", render_crosshair)

    render_pov._write_render_autoexec(["cl_crosshair_length 3"])

    for path in (autoexec, render_crosshair):
        text = path.read_text(encoding="utf-8")
        assert "cl_show_observer_crosshair 0" in text
        assert text.index("cl_show_observer_crosshair 0") < text.index("cl_crosshair_length 3")
