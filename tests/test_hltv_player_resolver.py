"""Tests for scrapers/hltv_player_resolver.py — pure HLTV player resolution."""

from __future__ import annotations

import json
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import player_accounts
from models import PlayerAccount
from scrapers import hltv_player_resolver as resolver


def test_hltv_player_id_from_url() -> None:
    assert resolver.hltv_player_id_from_url("https://www.hltv.org/player/23756/s1zzi") == "23756"
    assert resolver.hltv_player_id_from_url("https://www.hltv.org/player/25619/ropz") == "25619"
    assert resolver.hltv_player_id_from_url("https://example.com/player/bad") is None
    assert resolver.hltv_player_id_from_url(None) is None


def test_resolve_from_accounts_case_insensitive() -> None:
    accounts = [
        PlayerAccount(
            nickname="ropz",
            hltv_player_id="25619",
            hltv_player_url="https://www.hltv.org/player/25619/ropz",
        )
    ]
    result = resolver.resolve_from_accounts(accounts, "RoPz")
    assert result == {
        "player_url": "https://www.hltv.org/player/25619/ropz",
        "player_id": "25619",
        "source": "account",
    }


def test_resolve_from_ratings_by_player_key() -> None:
    ratings = {
        "tables": [
            {
                "map": "Nuke",
                "team": "FaZe",
                "players": [
                    {
                        "nickname": "ropz",
                        "rating": "1.42",
                        "hltv_player_url": "https://www.hltv.org/player/25619/ropz",
                    },
                    {
                        "nickname": "karrigan",
                        "rating": "0.95",
                        "hltv_player_url": "https://www.hltv.org/player/8183/karrigan",
                    },
                ],
            }
        ]
    }
    result = resolver.resolve_from_ratings(ratings, "RoPz")
    assert result == {
        "player_url": "https://www.hltv.org/player/25619/ropz",
        "player_id": "25619",
        "source": "ratings",
    }
    assert resolver.resolve_from_ratings(ratings, "missing") is None


def test_roster_nicknames_from_ratings() -> None:
    ratings = {
        "tables": [
            {
                "players": [
                    {"nickname": "ropz", "rating": "1.2"},
                    {"nickname": "karrigan", "rating": "0.9"},
                ]
            },
            {
                "players": [
                    {"nickname": "ropz", "rating": "1.1"},
                    {"nickname": "broky", "rating": "1.0"},
                ]
            },
        ]
    }
    assert resolver.roster_nicknames_from_ratings(ratings) == [
        "ropz",
        "karrigan",
        "broky",
    ]


def test_avatar_cache_eligible() -> None:
    account = PlayerAccount(
        nickname="ropz",
        hltv_player_id="25619",
        hltv_player_url="https://www.hltv.org/player/25619/ropz",
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = Path(tmpdir) / "ropz.png"
        buf = BytesIO()
        Image.new("RGBA", (400, 417), (0, 0, 0, 0)).save(buf, format="PNG")
        png_path.write_bytes(buf.getvalue())

        assert resolver.avatar_cache_eligible(png_path, account) is True
        assert resolver.avatar_cache_eligible(png_path, None) is False
        assert resolver.avatar_cache_eligible(
            png_path,
            PlayerAccount(nickname="ropz"),
        ) is False

        small_path = Path(tmpdir) / "small.png"
        small_buf = BytesIO()
        Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(small_buf, format="PNG")
        small_path.write_bytes(small_buf.getvalue())
        assert resolver.avatar_cache_eligible(small_path, account) is False


def test_update_hltv_player_persists_and_round_trips() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        accounts_file = Path(tmpdir) / "player_accounts.json"
        original_file = player_accounts.ACCOUNTS_FILE
        player_accounts.ACCOUNTS_FILE = accounts_file

        try:
            player_accounts.add_account("ropz", steam_url="https://steamcommunity.com/profiles/76561197991272318")
            updated = player_accounts.update_hltv_player(
                "ropz",
                "25619",
                "https://www.hltv.org/player/25619/ropz",
            )
            assert updated.hltv_player_id == "25619"
            assert updated.hltv_player_url == "https://www.hltv.org/player/25619/ropz"

            loaded = player_accounts.get_account("ropz")
            assert loaded is not None
            assert loaded.hltv_player_id == "25619"
            assert loaded.hltv_player_url == "https://www.hltv.org/player/25619/ropz"

            raw = json.loads(accounts_file.read_text(encoding="utf-8"))
            assert len(raw) == 1
            assert "hltv_player_id" in raw[0]
            assert raw[0]["hltv_player_id"] == "25619"
        finally:
            player_accounts.ACCOUNTS_FILE = original_file


def test_resolve_from_roster_by_player_key() -> None:
    roster = [
        {"nickname": "ropz", "playerUrl": "https://www.hltv.org/player/25619/ropz"},
        {"nickname": "broky", "playerUrl": "https://www.hltv.org/player/18987/broky"},
    ]
    result = resolver.resolve_from_roster(roster, "RoPz")
    assert result == {
        "player_url": "https://www.hltv.org/player/25619/ropz",
        "player_id": "25619",
        "source": "roster",
    }
    assert resolver.resolve_from_roster(roster, "missing") is None


def _fixture_search_html() -> str:
    return (Path(__file__).parent / "fixtures" / "hltv_search_results.html").read_text(
        encoding="utf-8"
    )


def _match_ratings() -> dict:
    return {
        "tables": [
            {
                "players": [
                    {"nickname": "ropz", "rating": "1.42"},
                    {"nickname": "broky", "rating": "1.10"},
                    {"nickname": "karrigan", "rating": "0.95"},
                ]
            }
        ]
    }


def test_parse_search_player_candidates() -> None:
    candidates = resolver.parse_search_player_candidates(_fixture_search_html())
    assert len(candidates) == 5
    assert candidates[0]["player_id"] == "11111"
    assert candidates[1]["nickname"] == "ropz"
    assert candidates[1]["player_url"] == "https://www.hltv.org/player/25619/ropz"


def test_disambiguate_search_single_roster_match() -> None:
    candidates = resolver.parse_search_player_candidates(_fixture_search_html())
    pick = resolver.disambiguate_search_candidates(candidates, ["ropz"], "ropz")
    assert pick is not None
    assert pick["player_id"] == "25619"
    assert pick["nickname"] == "ropz"


def test_disambiguate_search_exact_tiebreaker() -> None:
    html = """
    <html><body>
      <a href="/player/25619/ropz">ropz</a>
      <a href="/player/18987/broky">broky</a>
    </body></html>
    """
    candidates = resolver.parse_search_player_candidates(html)
    roster = resolver.roster_nicknames_from_ratings(_match_ratings())
    pick = resolver.disambiguate_search_candidates(candidates, roster, "RoPz")
    assert pick is not None
    assert pick["player_id"] == "25619"


def test_disambiguate_search_no_guess_when_zero_roster_matches() -> None:
    candidates = resolver.parse_search_player_candidates(_fixture_search_html())
    roster = ["only-on-roster"]
    assert resolver.disambiguate_search_candidates(candidates, roster, "ropz") is None


def test_disambiguate_search_no_guess_when_multiple_without_exact_match() -> None:
    html = """
    <html><body>
      <a href="/player/18987/broky">broky</a>
      <a href="/player/8183/karrigan">karrigan</a>
    </body></html>
    """
    candidates = resolver.parse_search_player_candidates(html)
    roster = resolver.roster_nicknames_from_ratings(_match_ratings())
    assert resolver.disambiguate_search_candidates(candidates, roster, "ropz") is None


def test_resolve_from_search_with_fixture() -> None:
    result = resolver.resolve_from_search(_fixture_search_html(), _match_ratings(), "ropz")
    assert result == {
        "player_url": "https://www.hltv.org/player/25619/ropz",
        "player_id": "25619",
        "source": "search",
    }


def test_resolve_hltv_player_search_after_roster() -> None:
    ratings = _match_ratings()
    roster = [{"nickname": "broky", "playerUrl": "https://www.hltv.org/player/18987/broky"}]
    search_html = _fixture_search_html()

    roster_hit = resolver.resolve_hltv_player(
        "broky",
        [],
        ratings,
        roster=roster,
        search_html=search_html,
    )
    assert roster_hit is not None
    assert roster_hit["source"] == "roster"

    search_hit = resolver.resolve_hltv_player(
        "ropz",
        [],
        ratings,
        roster=roster,
        search_html=search_html,
    )
    assert search_hit is not None
    assert search_hit["source"] == "search"
    assert search_hit["player_id"] == "25619"


def test_resolve_hltv_player_cascade_order() -> None:
    accounts = [
        PlayerAccount(
            nickname="cached",
            hltv_player_id="11111",
            hltv_player_url="https://www.hltv.org/player/11111/cached",
        )
    ]
    ratings = {
        "tables": [
            {
                "players": [
                    {
                        "nickname": "ropz",
                        "hltv_player_url": "https://www.hltv.org/player/25619/ropz",
                    }
                ]
            }
        ]
    }
    roster = [
        {"nickname": "ropz", "playerUrl": "https://www.hltv.org/player/99999/wrong"},
    ]

    account_first = resolver.resolve_hltv_player("cached", accounts, ratings, roster=roster)
    assert account_first is not None
    assert account_first["source"] == "account"

    ratings_first = resolver.resolve_hltv_player("ropz", [], ratings, roster=roster)
    assert ratings_first is not None
    assert ratings_first["source"] == "ratings"
    assert ratings_first["player_id"] == "25619"

    roster_only = resolver.resolve_hltv_player("ropz", [], {}, roster=roster)
    assert roster_only is not None
    assert roster_only["source"] == "roster"
    assert roster_only["player_id"] == "99999"

    search_only = resolver.resolve_hltv_player(
        "ropz",
        [],
        _match_ratings(),
        roster=[],
        search_html=_fixture_search_html(),
    )
    assert search_only is not None
    assert search_only["source"] == "search"


def test_load_ratings_json_missing_file() -> None:
    assert resolver.load_ratings_json(Path("/nonexistent/ratings.json")) == {}


def test_existing_player_accounts_json_loads_without_migration() -> None:
    legacy = [
        {
            "nickname": "legacy",
            "faceit_url": "",
            "faceit_nickname": "",
            "steam_url": "https://steamcommunity.com/profiles/76561197991272318",
            "steam_id": "76561197991272318",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
    ]
    account = PlayerAccount(**legacy[0])
    assert account.hltv_player_id == ""
    assert account.hltv_player_url == ""
