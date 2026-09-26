"""Swift HUD integration must never leave the user's game configuration changed."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "pov"))
from overlay.swift_demoui import (
    PACKAGE, mounted_hud, patch_runtime, read_vpk, write_vpk,
    restore, validate_render_profile, require_swift_capture,
)


def test_mount_restores_exact_gameinfo_after_render_failure(tmp_path):
    csgo = tmp_path / "csgo"
    csgo.mkdir()
    original = b'"GameInfo"\r\n{\r\n SearchPaths\r\n {\r\n  Game csgo // base\r\n }\r\n}\r\n'
    (csgo / "gameinfo.gi").write_bytes(original)
    menu, session = tmp_path / "menu.vpk", tmp_path / "session.vpk"
    menu.write_bytes(b"menu")
    session.write_bytes(b"session")
    with pytest.raises(RuntimeError, match="render failed"):
        with mounted_hud(csgo, menu, session):
            text = (csgo / "gameinfo.gi").read_text()
            assert text.index("cs2archive_swift/session.vpk") < text.index("cs2archive_swift/menu.vpk")
            assert text.index("cs2archive_swift/menu.vpk") < text.index("Game csgo // base")
            with pytest.raises(RuntimeError, match="active|restore"):
                with mounted_hud(csgo, menu, session):
                    pass
            raise RuntimeError("render failed")
    assert (csgo / "gameinfo.gi").read_bytes() == original
    assert not (csgo / "overrides/cs2archive_swift/session.vpk").exists()


def test_vpk_roundtrip_preserves_resource_names_bytes_and_crc():
    resources = {
        "panorama/scripts/hud/swift_demo_voice_data.vjs_c": b"data\0\xff",
        "panorama/scripts/hud/swift_demo_voice.vjs_c": b"script",
    }
    packed = write_vpk(resources)
    assert read_vpk(packed) == resources
    with pytest.raises(ValueError, match="CRC"):
        read_vpk(packed[:-1] + bytes([packed[-1] ^ 1]))


def test_unrecognized_gameinfo_is_never_modified(tmp_path):
    original = b"changed format without Game csgo"
    (tmp_path / "gameinfo.gi").write_bytes(original)
    with pytest.raises(RuntimeError, match="refusing"):
        with mounted_hud(tmp_path, Path("absent"), Path("absent")):
            pass
    assert (tmp_path / "gameinfo.gi").read_bytes() == original
    assert not (tmp_path / ".cs2archive-swift-session.json").exists()


def test_concurrent_edit_is_preserved_and_recovery_keeps_original(tmp_path):
    import base64
    import json
    original = b"SearchPaths\n{\n Game csgo\n}\n"
    gameinfo = tmp_path / "gameinfo.gi"
    gameinfo.write_bytes(original)
    menu, session = tmp_path / "a.vpk", tmp_path / "b.vpk"
    menu.write_bytes(b"a")
    session.write_bytes(b"b")
    with pytest.raises(RuntimeError, match="changed during render"):
        with mounted_hud(tmp_path, menu, session):
            gameinfo.write_bytes(b"a concurrent user edit")
    journal = tmp_path / ".cs2archive-swift-session.json"
    assert base64.b64decode(json.loads(journal.read_text())["original"]) == original
    assert gameinfo.read_bytes() == b"a concurrent user edit"
    gameinfo.write_bytes(original)
    restore(tmp_path)
    assert not journal.exists()


def test_existing_footage_requires_compatible_profile_and_is_not_deleted(tmp_path):
    clip = tmp_path / "round-001.mp4"
    clip.write_bytes(b"saved progress")
    with pytest.raises(RuntimeError, match="different voice indicator"):
        validate_render_profile(tmp_path, "swift", "123")
    with pytest.raises(RuntimeError, match="step 2"):
        require_swift_capture(tmp_path, "123")
    validate_render_profile(tmp_path, "off", "123")
    assert clip.read_bytes() == b"saved progress"


def test_swift_profile_can_resume_but_cannot_change_player_or_style(tmp_path):
    validate_render_profile(tmp_path, "swift", "123")
    (tmp_path / "round-001.mp4").write_bytes(b"new footage")
    validate_render_profile(tmp_path, "swift", "123")
    require_swift_capture(tmp_path, "123")
    for style, player in (("off", "123"), ("swift", "456")):
        with pytest.raises(RuntimeError):
            validate_render_profile(tmp_path, style, player)


def test_speaking_rows_drop_swift_chrome_for_native_hud():
    source = (PACKAGE / "swift_demo_voice.js").read_text(encoding="utf-8")
    adapted = patch_runtime(source, ["111"], {})
    assert adapted.count("function _StyleNativeSpeaking(") == 1
    assert 'notice.style.backgroundColor = "#00000000"' in adapted
    assert "if (CS2ArchiveVoice.native) _StyleNativeSpeaking" in adapted
    assert '"native": true' in adapted
    chrome = patch_runtime(source, ["111"], {}, native=False)
    assert '"native": false' in chrome
    assert "if (CS2ArchiveVoice.native) _StyleNativeSpeaking" in chrome
    assert chrome.count("function _StyleNativeSpeaking(") == 1


def test_pinned_runtime_filters_team_after_slot_change_and_never_changes_audio(tmp_path):
    import subprocess
    import shutil
    source = PACKAGE / "swift_demo_voice.js"
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not source.is_file() or not Path(node).is_file():
        pytest.skip("Install Swift package and Node for actual upstream-runtime verification")
    adapted = tmp_path / "hud.js"
    adapted.write_text(patch_runtime(source.read_text(encoding="utf-8"), ["111", "222"], {"111": "donk"}), encoding="utf-8")
    harness = Path(__file__).with_name("swift_demoui_runtime.cjs")
    result = subprocess.run([node, str(harness), str(adapted)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_renderer_mounts_prepared_hud_with_names_and_restores_on_failure(tmp_path, monkeypatch):
    import render_pov
    from overlay import swift_demoui
    from types import SimpleNamespace
    gameinfo = tmp_path / "gameinfo.gi"
    original = b"SearchPaths\n{\n Game csgo\n}\n"
    gameinfo.write_bytes(original)
    menu, session = tmp_path / "menu-source.vpk", tmp_path / "session-source.vpk"
    menu.write_bytes(b"menu")
    session.write_bytes(b"session")
    calls = []
    def prepared(demo, steam_id, output, names, *, native=True):
        calls.append((demo, steam_id, output, names, native))
        return menu, session
    monkeypatch.setattr(swift_demoui, "prepare", prepared)
    monkeypatch.setattr(render_pov, "GAME_CFG", tmp_path / "cfg")
    args = SimpleNamespace(voice_indicators="swift", rename='{"123":"donk"}')
    with pytest.raises(RuntimeError, match="capture failed"):
        with render_pov._voice_hud_session("demo.dem", tmp_path, "123", args):
            assert b"cs2archive_swift" in gameinfo.read_bytes()
            raise RuntimeError("capture failed")
    assert calls == [(Path("demo.dem"), "123", tmp_path, {"123": "donk"}, True)]
    assert gameinfo.read_bytes() == original
    args.voice_indicators = "off"
    with render_pov._voice_hud_session("demo.dem", tmp_path, "123", args):
        assert gameinfo.read_bytes() == original
    assert len(calls) == 1
    assert gameinfo.read_bytes() == original
