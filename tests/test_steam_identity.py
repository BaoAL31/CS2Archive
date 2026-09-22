"""Steam-first identity: nicks are display-only, steam_id is the key.

Regression cover for the donk-16:9 class of bugs (FACEIT nick donk666
never matching nick-keyed lookups that live under donk).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))


@pytest.fixture()
def accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import faceit_names as fn
    acc = tmp_path / "player_accounts.json"
    acc.write_text(json.dumps([
        {"nickname": "donk", "faceit_nickname": "donk666",
         "steam_id": "76561198386265483", "faceit_id": "fid-1"},
        {"nickname": "s1mple", "faceit_nickname": "s1mplecsgod",
         "steam_id": "76561198034202275", "faceit_id": "fid-2"},
    ]))
    av_dir = tmp_path / "avatars"
    (av_dir / "donk" / "hltv").mkdir(parents=True)
    (av_dir / "donk" / "hltv" / "donk.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(fn, "ACCOUNTS_PATH", acc)
    monkeypatch.setattr(fn, "AVATAR_DIR", av_dir)
    monkeypatch.setattr(fn, "_LOADED", False)
    monkeypatch.setattr(fn, "_CANON", {})
    monkeypatch.setattr(fn, "_FACEIT_IDS", {})
    monkeypatch.setattr(fn, "_STEAM_IDS", {})
    monkeypatch.setattr(fn, "_FACEIT_NICK", {})
    return fn


def test_canonical_nick_for_steam(accounts):
    assert accounts.canonical_nick_for_steam("76561198386265483", "donk666") == "donk"


def test_canonical_nick_for_steam_fallback(accounts):
    assert accounts.canonical_nick_for_steam("nope", "donk666") == "donk"
    assert accounts.canonical_nick_for_steam("nope", "stranger") == "stranger"
    assert accounts.canonical_nick_for_steam("nope") == ""


def test_avatar_path_for_steam(accounts):
    hit = accounts.avatar_path_for_steam("76561198386265483", "donk666")
    assert hit is not None and hit.name == "donk.png"


def test_avatar_path_for_steam_unknown(accounts):
    assert accounts.avatar_path_for_steam("nope", "stranger") is None
