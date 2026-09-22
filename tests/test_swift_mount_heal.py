"""Swift mount self-heal: a kill between mount and restore must not wedge renders.

Regression: every aborted/crashed render stranded gameinfo.gi + journal,
and the next run hard-failed ("already active") — each relaunch restarted
CS2 from scratch. mounted_hud now restores the stranded session when no
live renderer exists. Uses a fake csgo dir; never touches the real install.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "overlay"))


@pytest.fixture()
def fake_csgo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import swift_demoui as sd
    monkeypatch.setattr(sd, "_live_render_processes", lambda: False)
    csgo = tmp_path / "csgo"
    csgo.mkdir()
    orig = b"\tGame\tcsgo\n"
    mounted = sd._mounted_gameinfo(orig)
    (csgo / "gameinfo.gi").write_bytes(mounted)  # stranded: mounted on disk
    (csgo / sd.JOURNAL).write_text(json.dumps({
        "original": base64.b64encode(orig).decode(),
        "mounted_sha256": sd._sha(mounted),
    }))
    (csgo / sd.MOUNT).mkdir(parents=True)
    menu = tmp_path / "menu.vpk"
    menu.write_bytes(b"m")
    sess = tmp_path / "session.vpk"
    sess.write_bytes(b"s")
    return sd, csgo, orig, menu, sess


def test_stranded_mount_heals_and_restores(fake_csgo):
    sd, csgo, orig, menu, sess = fake_csgo
    with sd.mounted_hud(csgo, menu, sess):
        assert b"cs2archive_swift" in (csgo / "gameinfo.gi").read_bytes()
    assert (csgo / "gameinfo.gi").read_bytes() == orig
    assert not (csgo / sd.JOURNAL).exists()


def test_unrelated_gameinfo_error_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import swift_demoui as sd
    monkeypatch.setattr(sd, "_live_render_processes", lambda: False)
    csgo = tmp_path / "csgo2"
    csgo.mkdir()
    (csgo / "gameinfo.gi").write_bytes(b"no base entry here\n")
    menu = tmp_path / "m.vpk"
    menu.write_bytes(b"m")
    sess = tmp_path / "s.vpk"
    sess.write_bytes(b"s")
    with pytest.raises(RuntimeError, match="base Game csgo SearchPath"):
        with sd.mounted_hud(csgo, menu, sess):
            pass
