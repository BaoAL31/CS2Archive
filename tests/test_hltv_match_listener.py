import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hltv.match_listener import (
    State,
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


def test_event_and_team_filter():
    matches = parse_match_links(RESULTS)
    selected = select_matches(matches, {"100"}, ["Team Alpha"])
    assert [m.match_id for m in selected] == ["100"]
    assert parse_event_match_ids(RESULTS) == {"100", "101"}


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
