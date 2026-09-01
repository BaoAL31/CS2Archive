import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from datetime import datetime, timedelta

from hltv.match_listener import (
    State,
    Match,
    ScheduledMatch,
    DAILY_UPLOAD_LIMIT,
    initialize_result_baseline,
    _actionable_matches,
    select_best_card,
    sort_card_records,
    parse_event_match_ids,
    parse_match_links,
    parse_scheduled_matches,
    parse_top_teams,
    select_matches,
    event_busy,
    event_matches_url,
    has_pending_hltv,
    should_poll_faceit,
    _daily,
    _prune_queue,
    _queue_room,
    _slots_left,
    _pending_upload_metas,
    _spawn_upload_terminal,
    _start_upload_after_pipeline,
    _upload_cmd,
    _youtube_run_id_for_meta,
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


def test_select_best_card_one_per_match():
    cards = []
    for name, map_name, rating in (
        ("low", "Ancient", 1.5),
        ("best", "Ancient", 1.8),
        ("other", "Mirage", 1.6),
    ):
        path = Path("backlog") / f"{name}.json"
        cards.append((str(path).replace("\\", "/"),
                      {"player": name, "map": map_name, "rating": rating}))
    assert select_best_card(cards) == [
        ("backlog/best.json", {"player": "best", "map": "Ancient", "rating": 1.8}),
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


def test_baseline_skips_existing_results_but_allows_later_ids(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    existing = Match("100", "https://hltv/matches/100/alpha-vs-beta",
                     "alpha-vs-beta", "alpha", "beta")
    later = Match("101", "https://hltv/matches/101/gamma-vs-delta",
                  "gamma-vs-delta", "gamma", "delta")
    initialize_result_baseline(state, [existing])
    assert _actionable_matches(state, [existing]) == []
    state.data["result_baseline_ids"].append("101")
    assert _actionable_matches(state, [later]) == []
    unseen = Match("102", "https://hltv/matches/102/epsilon-vs-zeta",
                   "epsilon-vs-zeta", "epsilon", "zeta")
    assert [m.match_id for m in _actionable_matches(state, [unseen])] == ["102"]


DONK_CARD = {
    "player": "donk",
    "map": "Ancient",
    "hltv_url": "https://www.hltv.org/matches/2396943/spirit-vs-furia-blast-open-porto-2026",
    "demo_path": "demos/hltv/2396943-spirit-vs-furia-blast-open-porto/spirit-vs-furia-m1-ancient.dem",
}


def test_youtube_run_id_matches_pipeline_overlay_dir():
    assert _youtube_run_id_for_meta(DONK_CARD) == (
        "2396943_spirit-vs-furia-m1-ancient_donk_Ancient"
    )


def _write_pending_meta(dir_path: Path, video: Path, **extra) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": "donk | Ancient",
        "video_path": str(video),
        "privacy": "private",
        "upload_status": "pending",
        "youtube_id": None,
        **extra,
    }
    path = dir_path / "upload_meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    return path


def test_pending_upload_prefers_overlay_and_skips_completed(tmp_path: Path):
    card = "backlog/match/high/donk-ancient.json"
    (tmp_path / card).parent.mkdir(parents=True)
    (tmp_path / card).write_text(json.dumps(DONK_CARD), encoding="utf-8")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    run_id = _youtube_run_id_for_meta(DONK_CARD)
    overlay = _write_pending_meta(tmp_path / "youtube" / f"{run_id}_overlay", video)
    _write_pending_meta(tmp_path / "youtube" / run_id, video)
    assert _pending_upload_metas(card, root=tmp_path) == [overlay]

    overlay.write_text(json.dumps({
        "video_path": str(video),
        "upload_status": "completed",
        "youtube_id": "abc",
    }), encoding="utf-8")
    assert _pending_upload_metas(card, root=tmp_path) == []


def test_pending_upload_skips_missing_video(tmp_path: Path):
    card = "backlog/match/high/donk-ancient.json"
    (tmp_path / card).parent.mkdir(parents=True)
    (tmp_path / card).write_text(json.dumps(DONK_CARD), encoding="utf-8")
    run_id = _youtube_run_id_for_meta(DONK_CARD)
    _write_pending_meta(
        tmp_path / "youtube" / f"{run_id}_overlay",
        tmp_path / "missing.mp4",
    )
    assert _pending_upload_metas(card, root=tmp_path) == []


def test_upload_cmd_targets_this_meta_only(tmp_path: Path):
    video = tmp_path / "video.mp4"
    thumb = tmp_path / "thumb.png"
    video.write_bytes(b"x")
    thumb.write_bytes(b"y")
    meta_path = _write_pending_meta(
        tmp_path / "youtube" / "run_overlay", video, thumbnail_path=str(thumb)
    )
    cmd = _upload_cmd(meta_path)
    assert cmd is not None
    assert cmd[2].endswith("upload_pending.py")
    assert "--dir" in cmd and str(meta_path.parent) in cmd
    assert "--limit" in cmd and "1" in cmd
    joined = " ".join(cmd)
    assert "upload_youtube.py" not in joined
    assert "upload_pending.py" in joined


def test_spawn_upload_dry_run_does_not_popen(monkeypatch):
    called = []
    monkeypatch.setattr("hltv.match_listener.subprocess.Popen", lambda *a, **k: called.append((a, k)))
    _spawn_upload_terminal(["python", "upload_youtube.py"], dry_run=True)
    assert called == []


def test_spawn_upload_opens_new_console(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr("hltv.match_listener.subprocess.Popen", fake_popen)
    cmd = ["python", "-u", "scripts/upload/upload_youtube.py", "video.mp4"]
    _spawn_upload_terminal(cmd, dry_run=False)
    assert captured["cmd"] == cmd
    assert captured["kwargs"].get("creationflags") == subprocess.CREATE_NEW_CONSOLE


def test_start_upload_after_pipeline_dry_run_does_not_popen(monkeypatch):
    called = []
    monkeypatch.setattr("hltv.match_listener.subprocess.Popen", lambda *a, **k: called.append((a, k)))
    _start_upload_after_pipeline("backlog/x.json", dry_run=True)
    assert called == []


def test_start_upload_spawns_upload_pending_for_this_meta(monkeypatch, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    meta_path = _write_pending_meta(tmp_path / "youtube" / "run_overlay", video)
    monkeypatch.setattr(
        "hltv.match_listener._pending_upload_metas",
        lambda card, **k: [meta_path],
    )
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr("hltv.match_listener.subprocess.Popen", fake_popen)
    _start_upload_after_pipeline("backlog/x.json", dry_run=False)
    joined = " ".join(captured["cmd"])
    assert "upload_pending.py" in joined
    assert "upload_youtube.py" not in joined
    assert str(meta_path.parent) in captured["cmd"]
    assert "--limit" in captured["cmd"]
    assert captured["kwargs"].get("creationflags") == subprocess.CREATE_NEW_CONSOLE


FACEIT_CARD = {
    "player": "donk",
    "map": "Mirage",
    "is_faceit": True,
    "faceit_match_id": "1-abc",
    "demo_path": "demos/faceit/1-abc.dem",
}


def test_youtube_run_id_for_faceit_card():
    assert _youtube_run_id_for_meta(FACEIT_CARD) == "1-abc_1-abc_donk_Mirage"


def test_event_matches_url_uses_event_id():
    assert event_matches_url(
        "https://www.hltv.org/events/8249/blast-open-porto-2026"
    ).endswith("/events/8249/matches")


def test_parse_scheduled_matches_reads_upcoming_and_live():
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    unix_ms = int(noon.timestamp() * 1000)
    html = f"""
    <div class="upcomingMatch" data-zonedgrouping-entry-unix="{unix_ms}">
      <a class="a-reset" href="/matches/200/alpha-vs-beta">alpha vs beta</a>
    </div>
    <div class="liveMatch-container">
      <div class="matchTime matchLive">LIVE</div>
      <a class="a-reset" href="/matches/201/gamma-vs-delta">gamma vs delta</a>
    </div>
    """
    parsed = parse_scheduled_matches(html)
    by_id = {item.match_id: item for item in parsed}
    assert by_id["200"].unix_ms == unix_ms
    assert by_id["200"].live is False
    assert by_id["200"].slug == "alpha-vs-beta"
    assert by_id["201"].live is True


def test_parse_scheduled_matches_ignores_rating_matchlive_class():
    html = """
    <div data-zonedgrouping-entry-unix="1788530400000">
      <div class="match-wrapper" live="false" data-match-id="2396947">
        <a href="/matches/2396947/falcons-vs-g2-blast-open-porto-2026">
          <div class="match-rating matchLive"></div>
          <div class="match-time" data-unix="1788530400000">00:00</div>
        </a>
      </div>
    </div>
    """
    parsed = parse_scheduled_matches(html)
    assert len(parsed) == 1
    assert parsed[0].live is False
    assert parsed[0].unix_ms == 1788530400000


def test_parse_scheduled_matches_reads_live_attribute():
    html = """
    <div data-zonedgrouping-entry-unix="1788530400000">
      <div class="match-wrapper" live="true">
        <a href="/matches/201/gamma-vs-delta">gamma vs delta</a>
      </div>
    </div>
    """
    parsed = parse_scheduled_matches(html)
    assert parsed[0].live is True


def test_event_busy_when_match_starts_within_24h():
    now = datetime(2026, 9, 1, 20, 39)
    soon = now + timedelta(hours=18)
    scheduled = [ScheduledMatch("200", unix_ms=int(soon.timestamp() * 1000))]
    assert event_busy(scheduled, now) is True


def test_event_idle_when_next_match_is_beyond_24h():
    now = datetime(2026, 9, 1, 20, 39)
    later = now + timedelta(hours=25)
    scheduled = [ScheduledMatch("200", unix_ms=int(later.timestamp() * 1000))]
    assert event_busy(scheduled, now) is False


def test_event_busy_when_live_even_if_unscheduled():
    now = datetime(2026, 9, 1, 20, 39)
    scheduled = [ScheduledMatch("200", live=True)]
    assert event_busy(scheduled, now) is True


def test_event_idle_when_only_completed_results_remain():
    now = datetime(2026, 9, 1, 20, 39)
    later = now + timedelta(hours=48)
    scheduled = [ScheduledMatch("200", unix_ms=int(later.timestamp() * 1000))]
    assert event_busy(scheduled, now) is False


def test_daily_slots_reset_on_new_day(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["completed"] = ["a", "b", "c"]
    assert _slots_left(state) == 0
    daily["day"] = "2000-01-01"
    assert _slots_left(state) == DAILY_UPLOAD_LIMIT
    assert _queue_room(state) == DAILY_UPLOAD_LIMIT


def test_should_poll_faceit_only_on_idle_hltv_day(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    assert should_poll_faceit(state, hltv_busy=False)
    assert not should_poll_faceit(state, hltv_busy=True)
    state.data["queue"] = ["backlog/match/high/donk.json"]
    assert has_pending_hltv(state)
    assert not should_poll_faceit(state, hltv_busy=False)


def test_should_poll_faceit_again_when_slots_remain(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["completed"] = ["faceit/2026-09-01/high/neityu-nuke.json"]
    daily["faceit_queued"] = daily["completed"]
    assert _slots_left(state) == DAILY_UPLOAD_LIMIT - 1
    assert should_poll_faceit(state, hltv_busy=False)


def test_should_not_scrape_faceit_inside_cooldown(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    now = datetime(2026, 9, 1, 21, 0)
    _daily(state)["faceit_last_scrape"] = now.isoformat()
    assert not should_poll_faceit(state, hltv_busy=False, now=now + timedelta(minutes=5))
    assert should_poll_faceit(state, hltv_busy=False, now=now + timedelta(minutes=16))


def test_prune_keeps_one_faceit_card_per_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("hltv.match_listener.ROOT", tmp_path)
    cards = []
    for match_id, player, rating in (
        ("m1", "donk", 1.8),
        ("m2", "ropz", 1.7),
        ("m3", "sh1ro", 1.6),
        ("m1", "magixx", 1.2),
    ):
        rel = f"backlog/faceit/2026-09-01/high/{player}-{match_id}.json"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "player": player,
            "map": "Mirage",
            "rating": rating,
            "is_faceit": True,
            "faceit_match_id": match_id,
        }), encoding="utf-8")
        cards.append(rel)
    kept = _prune_queue(cards, indexes={
        "ranking": {},
        "player_demand": {},
        "team_demand": {},
        "highlight_players": {},
        "fixtures": [],
    })
    players = {
        json.loads((tmp_path / rel).read_text(encoding="utf-8"))["player"]
        for rel in kept
    }
    assert players == {"donk", "ropz", "sh1ro"}
