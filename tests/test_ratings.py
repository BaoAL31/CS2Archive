"""Tests for HLTV ratings scraper parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers.ratings import parse_match_ratings_html

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ratings_match_stats.html"


def test_parse_ratings_extracts_hltv_player_url() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    result = parse_match_ratings_html(html, "https://www.hltv.org/matches/123/team-a-vs-team-b")

    assert result is not None
    players = result["tables"][0]["players"]
    s1zzi = next(p for p in players if p["nickname"] == "s1zzi")

    assert s1zzi["hltv_player_url"] == "https://www.hltv.org/player/23756/s1zzi"
    assert s1zzi["rating"] == "1.54"


def test_parse_ratings_without_profile_links_omits_url_field() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    result = parse_match_ratings_html(html, "https://www.hltv.org/matches/123/team-a-vs-team-b")

    assert result is not None
    players = result["tables"][0]["players"]
    guest = next(p for p in players if p["nickname"] == "UnknownGuest")

    assert "hltv_player_url" not in guest
    assert guest["rating"] == "0.88"


def test_parse_ratings_json_serializable_without_profile_links() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    result = parse_match_ratings_html(html, "https://www.hltv.org/matches/123/team-a-vs-team-b")

    encoded = json.dumps(result)
    decoded = json.loads(encoded)

    assert decoded["tables"][0]["players"][1]["nickname"] == "UnknownGuest"
    assert "hltv_player_url" not in decoded["tables"][0]["players"][1]
