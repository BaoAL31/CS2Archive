"""Test HLTV backlog priority buckets."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


from cs2archive.pov.create_backlog import get_priority  # noqa: E402


def test_rating_1_5_returns_high():
    assert get_priority(1.5) == "high"


def test_rating_1_54_returns_high():
    assert get_priority(1.54) == "high"


def test_rating_1_6_returns_high():
    assert get_priority(1.6) == "high"


def test_rating_1_49_returns_medium():
    assert get_priority(1.49) == "medium"


def test_rating_1_0_returns_medium():
    assert get_priority(1.0) == "medium"


def test_rating_0_99_returns_low():
    assert get_priority(0.99) == "low"


# ── Nickname -> Steam64 resolution must respect the demo's own roster ───────
#
# Two registered accounts can share a nickname through aliases (Senzu and
# Senzu-). The old resolver returned the literal nickname match blindly, even
# when that Steam64 was not a player in the demo being processed: the FaZe vs
# BC.Game card (2399635) then rendered a POV for a nonexistent player, the
# keyboard overlay's button_extract panicked with "no entry found for key",
# and ~90 GB of render + scale were thrown away (2026-10-23).

ACCOUNTS = [
    {"nickname": "Senzu", "steam_id": "76561198951772368",
     "steam_url": "https://steamcommunity.com/profiles/76561198951772368",
     "aliases": ["Senzu-"]},
    {"nickname": "Senzu-", "steam_id": "76561198920720017",
     "steam_url": "https://steamcommunity.com/profiles/76561198920720017",
     "aliases": ["Senzu"],
     "note": "JP account used for BC Games queues - real Senzu"},
]
DEMO_ROSTER = {"Senzu": "76561198920720017"}   # the demo's Senzu is the Senzu- account


def _patch_accounts(monkeypatch, accounts):
    import cs2archive.player_accounts as pa
    monkeypatch.setattr(pa, "_load_accounts", lambda: [dict(r) for r in accounts])


def test_resolve_prefers_the_account_that_is_in_the_demo(monkeypatch):
    from cs2archive.pov.create_backlog import _resolve_steam_id

    _patch_accounts(monkeypatch, ACCOUNTS)
    assert _resolve_steam_id("Senzu", DEMO_ROSTER) == "76561198920720017"


def test_resolve_without_a_roster_keeps_the_nickname_match(monkeypatch):
    from cs2archive.pov.create_backlog import _resolve_steam_id

    _patch_accounts(monkeypatch, ACCOUNTS)
    assert _resolve_steam_id("Senzu") == "76561198951772368"


def test_resolve_keeps_the_nickname_match_when_no_account_is_in_the_demo(monkeypatch):
    """Split demos: the roster may only cover the analysed part."""
    from cs2archive.pov.create_backlog import _resolve_steam_id

    _patch_accounts(monkeypatch, ACCOUNTS)
    assert _resolve_steam_id("Senzu", {"Someone Else": "76561190000000001"}) == (
        "76561198951772368")


def test_resolve_falls_back_to_the_demo_roster_name(monkeypatch):
    from cs2archive.pov.create_backlog import _resolve_steam_id

    _patch_accounts(monkeypatch, [])
    assert _resolve_steam_id("Senzu", DEMO_ROSTER) == "76561198920720017"


def test_resolve_matches_aliases_against_the_roster(monkeypatch):
    """The in-demo name may equal an ALIAS of an account whose nickname differs."""
    from cs2archive.pov.create_backlog import _resolve_steam_id

    _patch_accounts(monkeypatch, ACCOUNTS)
    assert _resolve_steam_id("Senzu-", {"Senzu": "76561198920720017"}) == (
        "76561198920720017")
