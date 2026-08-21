import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hltv.match_listener import (
    State,
    select_highest_per_map,
    sort_card_records,
    parse_event_match_ids,
    parse_match_links,
    parse_top_teams,
    select_matches,
)


RESULTS = """
<a href="/matches/100/team-alpha-vs-team-beta">alpha vs beta</a>
<a href="/matches/100/team-alpha-vs-team-beta">duplicate</a>
<a href="/matches/101/team-gamma-vs-team-delta">gamma vs delta</a>
"""


def test_parse_results_deduplicates_matches():
    matches = parse_match_links(RESULTS)
    assert [m.match_id for m in matches] == ["100", "101"]
    assert matches[0].team1 == "team alpha"
    assert matches[0].team2 == "team beta"


def test_parse_results_ignores_sidebar_match_links():
    html = """
    <aside><a href="/matches/999/upcoming-vs-match">upcoming</a></aside>
    <div class="results-holder">
      <div class="result-con">
        <a class="a-reset" href="/matches/100/team-alpha-vs-team-beta">done</a>
      </div>
    </div>
    """
    assert [m.match_id for m in parse_match_links(html)] == ["100"]


def test_event_and_team_filter():
    matches = parse_match_links(RESULTS)
    selected = select_matches(matches, {"100"}, ["Team Alpha"])
    assert [m.match_id for m in selected] == ["100"]
    assert parse_event_match_ids(RESULTS) == {"100", "101"}


def test_results_event_label_matches_when_event_page_ids_are_stale():
    html = """
    <div class="results-holder">
      <div class="result-con">
        <a href="/matches/100/team-alpha-vs-team-beta">done</a>
        <span class="event-name">Esports World Cup 2026</span>
      </div>
    </div>
    """
    matches = parse_match_links(html)
    selected = select_matches(
        matches, set(), ["Team Alpha"], "esports world cup 2026"
    )
    assert [m.match_id for m in selected] == ["100"]


def test_parse_top_twenty_teams():
    html = """
    <div class="ranking-item"><div class="ranking-item-team-name">Spirit</div></div>
    <div class="ranking-item"><div class="ranking-item-team-name">Vitality</div></div>
    """
    assert parse_top_teams(html) == ["Spirit", "Vitality"]


def test_state_round_trips_atomically(tmp_path: Path):
    path = tmp_path / "listener.json"
    state = State(path)
    state.data["queue"].append("backlog/example.json")
    state.save()
    loaded = State(path)
    assert loaded.data["queue"] == ["backlog/example.json"]
    assert json.loads(path.read_text())["version"] == 1


def test_select_highest_per_map_uses_rating():
    cards = []
    for name, map_name, rating in (
        ("low", "Ancient", 1.5),
        ("best", "Ancient", 1.8),
        ("other", "Mirage", 1.6),
    ):
        path = Path("backlog") / f"{name}.json"
        cards.append((str(path).replace("\\", "/"),
                      {"player": name, "map": map_name, "rating": rating}))
    assert select_highest_per_map(cards) == [
        ("backlog/best.json", {"player": "best", "map": "Ancient", "rating": 1.8}),
        ("backlog/other.json", {"player": "other", "map": "Mirage", "rating": 1.6}),
    ]


def test_queue_sorts_by_rating_descending():
    cards = [
        ("backlog/donk.json", {"rating": 2.43}),
        ("backlog/kyousuke.json", {"rating": 2.54}),
        ("backlog/low.json", {"rating": 1.8}),
    ]
    assert sort_card_records(cards) == [
        "backlog/kyousuke.json",
        "backlog/donk.json",
        "backlog/low.json",
    ]
