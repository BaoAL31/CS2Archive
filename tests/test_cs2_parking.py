"""Window parking must not move captures outside the desktop."""
from pathlib import Path
from unittest.mock import Mock


def test_sequence_cfg_disables_background_sleep():
    cfg = Path('assets/cs2_pov.cfg').read_text()
    assert 'engine_no_focus_sleep 0' in cfg.splitlines()
    assert 'fps_max_tools 0' in cfg.splitlines()
    assert 'fps_max_ui 0' in cfg.splitlines()


def test_park_preserves_visible_window_geometry(monkeypatch):
    from cs2archive import cs2_minimizer as windows

    api = Mock()
    api.IsWindow.return_value = True
    api.IsIconic.return_value = False
    monkeypatch.setattr(windows, 'user32', api)
    windows.park_cs2(123)
    api.GetWindowRect.assert_not_called()
    api.SetWindowPos.assert_called_once_with(
        123, windows.HWND_BOTTOM, 0, 0, 0, 0,
        windows.SWP_NOMOVE | windows.SWP_NOSIZE | windows.SWP_NOACTIVATE)
