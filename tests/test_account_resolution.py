"""Account-first video-settings resolution (FACEIT nick vs HLTV nick).

Regression: a FACEIT card for player=donk666 resolved prosettings as a
miss (entry lives under donk) and rendered 1920x1080 16:9 Native instead
of the account's 1280x960 4:3 Stretched. find_account bridges the alias.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def accounts_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import _backlog_common as bc
    p = tmp_path / "player_accounts.json"
    p.write_text(json.dumps([
        {"nickname": "donk", "faceit_nickname": "donk666",
         "steam_id": "76561198386265483",
         "capture_width": 1280, "capture_height": 960,
         "aspect_ratio": "4:3", "scaling_mode": "Stretched"},
        {"nickname": "s1mple", "faceit_nickname": "s1mplecsgod",
         "steam_id": "76561198034202275",
         "capture_width": 1280, "capture_height": 960,
         "aspect_ratio": "4:3", "scaling_mode": "Stretched"},
    ]))
    monkeypatch.setattr(bc, "ACCOUNTS_PATH", p)
    return p


def test_find_account_by_steam(accounts_file: Path):
    from _backlog_common import find_account
    assert find_account(steam_id="76561198386265483")["nickname"] == "donk"


def test_find_account_by_faceit_nickname(accounts_file: Path):
    from _backlog_common import find_account
    acct = find_account(player="donk666")
    assert acct["nickname"] == "donk"
    assert acct["capture_width"] == 1280
    assert acct["aspect_ratio"] == "4:3"


def test_find_account_nickname_case_insensitive(accounts_file: Path):
    from _backlog_common import find_account
    assert find_account(player="S1MPLE")["steam_id"] == "76561198034202275"


def test_find_account_steam_beats_nick(accounts_file: Path):
    from _backlog_common import find_account
    acct = find_account(player="s1mple", steam_id="76561198386265483")
    assert acct["nickname"] == "donk"


def test_find_account_miss_returns_empty(accounts_file: Path):
    from _backlog_common import find_account
    assert find_account(player="nosuchplayer") == {}
    assert find_account() == {}
