"""A ready local POV must not wait behind unrelated demo ingestion."""
import asyncio
import json
import pytest
from datetime import datetime, timedelta

from cs2archive.listener import daemon


def test_ready_hltv_queue_renders_before_discovery_and_persists_day_roll(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "_sort_queue", lambda cards, **kwargs: list(cards))
    state = daemon.State(tmp_path / "listener.json")
    card = "backlog/ready.json"
    (tmp_path / "backlog").mkdir()
    (tmp_path / card).write_text(json.dumps({"player": "ZywOo", "rating": 1.91}))
    state.data["queue"] = [card]
    state.data["daily"] = {"day": (datetime.now() - timedelta(days=1)).date().isoformat(),
                           "completed": ["yesterday-a", "yesterday-b"]}
    state.save()
    events = []
    def fetch(*args, **kwargs):
        events.append("discovery")
        raise AssertionError("ready render should run before network discovery")
    def render(path, *args):
        saved = json.loads(state.path.read_text())
        assert saved["daily"]["day"] == datetime.now().date().isoformat()
        assert saved["daily"]["completed"] == []
        events.append("render")
        return True
    def finish(s, path, **kwargs):
        s.data["queue"].remove(path)
        daemon._daily(s)["completed"].append(path)
        s.save()
    monkeypatch.setattr(daemon, "fetch_hltv_page_html", fetch)
    monkeypatch.setattr(daemon, "_pipeline_lock_held", lambda: False)
    monkeypatch.setattr(daemon, "_run_pipeline", render)
    monkeypatch.setattr(daemon, "_finish_card", finish)
    monkeypatch.setattr(daemon, "_load_score_indexes", lambda: {})
    state.data["teams"] = ["Vitality"]
    args = daemon.build_parser().parse_args(["--no-refresh-teams"])
    asyncio.run(daemon.poll_once(args, state))
    assert events == ["render"]
    assert daemon._slots_left(state) == 1


@pytest.mark.parametrize("blocked_by", ["live_hltv", "pipeline_lock", "daily_cap"])
def test_queue_guards_survive_the_ready_first_path(monkeypatch, tmp_path, blocked_by):
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    state = daemon.State(tmp_path / "listener.json")
    card = "backlog/faceit/test.json" if blocked_by == "live_hltv" else "backlog/ready.json"
    path = tmp_path / card
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    state.data["queue"] = [card]
    if blocked_by == "daily_cap":
        daemon._daily(state)["completed"] = ["one", "two"]
    monkeypatch.setattr(daemon, "_pipeline_lock_held", lambda: blocked_by == "pipeline_lock")
    def unexpected_render(*args):
        raise AssertionError("queue guard must prevent this render")
    monkeypatch.setattr(daemon, "_run_pipeline", unexpected_render)
    daemon._drain_queue(daemon.build_parser().parse_args([]), state, hltv_busy=True)
    assert state.data["queue"] == [card]
