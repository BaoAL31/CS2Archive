"""Tests for HLTV player avatar scraping helpers."""

from __future__ import annotations

import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PlayerAccount
from scrapers import hltv_player_resolver as resolver
from scrapers.player_images import (
    _bodyshot_url_matches_player,
    _cdn_bodyshot_urls,
    _player_id_from_url,
    _promote_hltv_identity,
)


def test_bodyshot_url_matches_player_id() -> None:
    pid = "23756"
    assert _bodyshot_url_matches_player(
        f"https://img-cdn.hltv.org/playerbodyshot/{pid}/abc.png?w=400",
        pid,
    )
    assert _bodyshot_url_matches_player(
        f"https://img-cdn.hltv.org/playerbodyshot/abc.png?playerid={pid}&w=400",
        pid,
    )
    assert not _bodyshot_url_matches_player(
        "https://img-cdn.hltv.org/playerbodyshot/99999/abc.png?w=400",
        pid,
    )
    assert not _bodyshot_url_matches_player(
        f"https://img-cdn.hltv.org/playerbodyshot/abc.png?playerid=99999&w=400",
        pid,
    )
    assert not _bodyshot_url_matches_player(
        "https://img-cdn.hltv.org/playerbodyshot/abc.png?w=400",
        pid,
    )
    assert not _bodyshot_url_matches_player(
        f"https://img-cdn.hltv.org/playerbodyshot/{pid}/abc.png?w=400",
        "",
    )


def test_cdn_bodyshot_urls_include_player_id() -> None:
    pid = "25619"
    urls = _cdn_bodyshot_urls(pid)
    assert len(urls) == 4
    assert any(f"/playerbodyshot/{pid}/" in url for url in urls)
    assert any(f"playerid={pid}" in url for url in urls)
    assert all("img-cdn.hltv.org/playerbodyshot" in url for url in urls)
    assert all("w=400" in url or "w=300" in url for url in urls)


def test_cdn_bodyshot_urls_empty_for_blank_id() -> None:
    assert _cdn_bodyshot_urls("") == []
    assert _cdn_bodyshot_urls("   ") == []


def test_player_id_from_url() -> None:
    assert _player_id_from_url("https://www.hltv.org/player/23756/s1zzi") == "23756"


def test_legacy_avatar_without_hltv_id_not_cache_eligible() -> None:
    account = PlayerAccount(nickname="legacy")
    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = Path(tmpdir) / "legacy.png"
        buf = BytesIO()
        Image.new("RGBA", (400, 417), (0, 0, 0, 0)).save(buf, format="PNG")
        png_path.write_bytes(buf.getvalue())
        assert resolver.avatar_cache_eligible(png_path, account) is False


def test_promote_hltv_identity_updates_account() -> None:
    account = PlayerAccount(nickname="ropz")
    resolution = {
        "player_url": "https://www.hltv.org/player/25619/ropz",
        "player_id": "25619",
        "source": "ratings",
    }
    with patch("player_accounts.update_hltv_player") as update_mock:
        _promote_hltv_identity(account, resolution)
        update_mock.assert_called_once_with(
            "ropz",
            "25619",
            "https://www.hltv.org/player/25619/ropz",
        )
