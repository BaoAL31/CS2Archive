from unittest.mock import patch

from cs2archive.shorts.render_shorts import _build_csdm_config


def test_short_sequence_locks_actual_demo_player(tmp_path):
    import pandas as pd

    short = {"pov_steam_id": "123", "pov_nick": "Canonical", "start_tick": 100, "end_tick": 500}
    roster = pd.DataFrame([{"steamid": 123, "name": "DemoNick"}])
    with patch("cs2archive.shorts.render_shorts._get_player_crosshair_cvars", return_value=[]), \
         patch("demoparser2.DemoParser") as parser:
        parser.return_value.parse_player_info.return_value = roster
        cfg = _build_csdm_config([short], tmp_path / "test.dem", tmp_path)
    text = cfg["sequences"][0]["cfg"]
    assert 'mirv_script_load "mirv_script_spec_lock_name.js"' in text
    assert 'mirv_script_spec_lock_name "DemoNick"' in text
    assert text.index('mirv_script_spec_lock_name "DemoNick"') > text.index("exec spec_lock")
    assert text.rindex("cl_draw_only_deathnotices 1") > text.rindex("cl_draw_only_deathnotices 0")
