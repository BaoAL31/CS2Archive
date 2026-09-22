import json

import pytest

from scripts.misc import install_csdm_startup_fix as fix


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    stock = tmp_path / "server.dll"
    stock.write_bytes(b"stock")
    candidate = tmp_path / "candidate.dll"
    candidate.write_bytes(b"patched")
    monkeypatch.setattr(fix, "STOCK_SHA256", fix.digest(stock))
    monkeypatch.setattr(fix, "PATCHED_SHA256", fix.digest(candidate))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"playback": {"cs2PluginVersion": "latest", "launchParameters": '-width 1920 +exec "my config.cfg"'}, "other": 5}))
    return settings, tmp_path, candidate, tmp_path / "journal.json"


def test_install_idempotence_and_undo_preserve_unrelated_settings(deployment):
    settings, directory, binary, journal = deployment
    before = json.loads(settings.read_text())
    fix.install(*deployment)
    fix.install(*deployment)
    data = json.loads(settings.read_text())
    assert data["playback"]["launchParameters"] == before["playback"]["launchParameters"] + " +csdm_initialize"
    assert data["playback"]["cs2PluginVersion"] == fix.VERSION
    assert (directory / f"server_{fix.VERSION}.dll").read_bytes() == binary.read_bytes()
    assert (directory / "server.dll").read_bytes() == b"stock"
    data["other"] = 9
    settings.write_text(json.dumps(data))
    fix.undo(settings, journal)
    assert json.loads(settings.read_text()) == {**before, "other": 9}


def test_rejects_new_stock_version_before_mutating(deployment):
    settings, directory, _, journal = deployment
    original = settings.read_bytes()
    (directory / "server.dll").write_bytes(b"new version")
    with pytest.raises(RuntimeError, match="stock plugin changed"):
        fix.install(*deployment)
    assert settings.read_bytes() == original
    assert not journal.exists()


def test_undo_refuses_to_clobber_later_launch_edits(deployment):
    settings, _, _, journal = deployment
    fix.install(*deployment)
    data = json.loads(settings.read_text())
    data["playback"]["launchParameters"] += " -some-new-option"
    settings.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="changed since installation"):
        fix.undo(settings, journal)
    assert json.loads(settings.read_text()) == data


def test_failed_settings_write_can_be_recovered(deployment, monkeypatch):
    settings, _, _, journal = deployment
    original = json.loads(settings.read_text())
    write = fix.write_json
    def fail_settings(path, data):
        if path == settings:
            raise OSError("simulated interruption")
        write(path, data)
    monkeypatch.setattr(fix, "write_json", fail_settings)
    with pytest.raises(OSError):
        fix.install(*deployment)
    monkeypatch.setattr(fix, "write_json", write)
    fix.undo(settings, journal)
    assert json.loads(settings.read_text()) == original


def test_production_preflight_preserves_plugin_and_initialization(deployment):
    from scripts.hook_aware import ensure_csdm_steam_launch

    settings, _, _, _ = deployment
    fix.install(*deployment)
    ensure_csdm_steam_launch(settings)
    playback = json.loads(settings.read_text())["playback"]
    assert playback["cs2PluginVersion"] == fix.VERSION
    assert playback["launchParameters"].split().count(fix.COMMAND) == 1
