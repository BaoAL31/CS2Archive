import json
import subprocess
import sys
from pathlib import Path


from datetime import date, datetime, timedelta

from cs2archive.listener.daemon import (
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
    parse_results_headline_date,
    parse_scheduled_matches,
    parse_top_teams,
    select_matches,
    event_busy,
    event_matches_url,
    _faceit_window_hours,
    should_poll_faceit,
    _daily,
    _prune_queue,
    _queue_room,
    _slots_left,
    _mark_existing_backlog_done,
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


def test_parse_results_headline_date():
    assert parse_results_headline_date("Results for August 31st 2026") == date(2026, 8, 31)
    assert parse_results_headline_date("Results for September 1st 2026") == date(2026, 9, 1)
    assert parse_results_headline_date("Results for September 2nd 2026") == date(2026, 9, 2)
    assert parse_results_headline_date("Results for September 3rd 2026") == date(2026, 9, 3)
    assert parse_results_headline_date("garbage") is None


def test_parse_match_links_keeps_only_requested_headline_dates():
    html = """
    <div class="results-holder">
      <div class="standard-headline">Results for September 2nd 2026</div>
      <div class="result-con">
        <a href="/matches/10/new-vs-new">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
      <div class="standard-headline">Results for August 31st 2026</div>
      <div class="result-con">
        <a href="/matches/2396941/vitality-vs-legacy-blast-open-porto-2026">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
    </div>
    """
    assert [m.match_id for m in parse_match_links(html)] == ["10", "2396941"]
    recent = parse_match_links(html, on_dates={date(2026, 9, 2), date(2026, 9, 1)})
    assert [m.match_id for m in recent] == ["10"]


def test_parse_match_links_keeps_featured_results_when_dating():
    html = """
    <div class="results-holder">
      <div class="standard-headline">Featured results</div>
      <div class="result-con">
        <a href="/matches/2396950/mouz-vs-vitality-blast-open-porto-2026">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
      <div class="result-con">
        <a href="/matches/2396949/spirit-vs-falcons-blast-open-porto-2026">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
      <div class="standard-headline">Results for September 6th 2026</div>
      <div class="result-con">
        <a href="/matches/20/today-vs-today">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
      <div class="standard-headline">Results for August 31st 2026</div>
      <div class="result-con">
        <a href="/matches/2396941/vitality-vs-legacy-blast-open-porto-2026">x</a>
        <span class="event-name">BLAST Open Porto 2026</span>
      </div>
    </div>
    """
    recent = parse_match_links(
        html, on_dates={date(2026, 9, 6), date(2026, 9, 5)})
    assert [m.match_id for m in recent] == ["2396950", "2396949", "20"]


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


def test_candidate_cards_one_upload_slot_per_match(monkeypatch, tmp_path):
    """A two-map series must not eat both daily upload slots.

    9z vs NAVI queued b1t/Nuke *and* makazze/Cache on 2026-10-06, spending the
    whole 2/day budget and deferring spirit vs mouz and vitality vs falcons.
    """
    import cs2archive.listener.daemon as daemon

    slug = "2398735-9z-vs-natus-vincere-esl-pro-league-season-24"
    # HLTV URLs carry the id separately from the team slug.
    url = f"https://www.hltv.org/matches/2398735/{slug.removeprefix('2398735-')}"
    folder = tmp_path / "backlog" / slug / "high"
    folder.mkdir(parents=True)
    for player, map_name, rating in (("b1t", "Nuke", 1.68),
                                     ("makazze", "Cache", 1.77)):
        (folder / f"{player}-{map_name.lower()}.json").write_text(
            json.dumps({"player": player, "map": map_name, "rating": rating}),
            encoding="utf-8",
        )
    monkeypatch.setattr(daemon, "ROOT", tmp_path)

    match = Match(
        match_id="2398735",
        url=url,
        slug=slug,
        team1="9z",
        team2="natus vincere",
        event="ESL Pro League Season 24",
    )

    assert daemon._candidate_cards(match) == [
        f"backlog/{slug}/high/makazze-cache.json",
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


def test_existing_backlog_cards_mark_match_done():
    record = {"status": "discovered", "attempts": 0}
    cards = ["backlog/2396941/medium/ropz-nuke.json"]
    _mark_existing_backlog_done(record, cards)
    assert record["status"] == "completed"
    assert record["cards"] == cards
    assert record["completed_cards"] == cards
    assert record["skip_reason"] == "existing backlog"
    _mark_existing_backlog_done(record, cards)
    assert record["completed_cards"] == cards


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
    monkeypatch.setattr("cs2archive.listener.daemon.subprocess.Popen", lambda *a, **k: called.append((a, k)))
    _spawn_upload_terminal(["python", "upload_youtube.py"], dry_run=True)
    assert called == []


def test_spawn_upload_opens_new_console(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr("cs2archive.listener.daemon.subprocess.Popen", fake_popen)
    cmd = ["python", "-u", "cs2archive/upload/upload_youtube.py", "video.mp4"]
    _spawn_upload_terminal(cmd, dry_run=False)
    assert captured["cmd"] == cmd
    assert captured["kwargs"].get("creationflags") == subprocess.CREATE_NEW_CONSOLE


def test_start_upload_after_pipeline_dry_run_does_not_popen(monkeypatch):
    called = []
    monkeypatch.setattr("cs2archive.listener.daemon.subprocess.Popen", lambda *a, **k: called.append((a, k)))
    _start_upload_after_pipeline("backlog/x.json", dry_run=True)
    assert called == []


def test_start_upload_spawns_upload_pending_for_this_meta(monkeypatch, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    meta_path = _write_pending_meta(tmp_path / "youtube" / "run_overlay", video)
    monkeypatch.setattr(
        "cs2archive.listener.daemon._pending_upload_metas",
        lambda card, **k: [meta_path],
    )
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr("cs2archive.listener.daemon.subprocess.Popen", fake_popen)
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


def test_event_busy_when_match_starts_within_12h():
    now = datetime(2026, 9, 1, 20, 39)
    soon = now + timedelta(hours=8)
    scheduled = [ScheduledMatch("200", unix_ms=int(soon.timestamp() * 1000))]
    assert event_busy(scheduled, now) is True


def test_event_idle_when_next_match_is_beyond_12h():
    now = datetime(2026, 9, 1, 20, 39)
    later = now + timedelta(hours=13)
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
    daily["completed"] = ["a", "b"]
    assert _slots_left(state) == 0
    daily["day"] = "2000-01-01"
    assert _slots_left(state) == DAILY_UPLOAD_LIMIT
    assert _queue_room(state) == DAILY_UPLOAD_LIMIT


def test_should_poll_faceit_when_slots_remain(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    assert should_poll_faceit(state)
    _daily(state)["completed"] = ["a", "b"]
    assert not should_poll_faceit(state)


def test_should_poll_faceit_again_when_slots_remain(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["completed"] = ["faceit/2026-09-01/high/neityu-nuke.json"]
    daily["faceit_queued"] = daily["completed"]
    assert _slots_left(state) == DAILY_UPLOAD_LIMIT - 1
    assert should_poll_faceit(state)


def test_should_not_scrape_faceit_inside_cooldown(tmp_path: Path):
    state = State(tmp_path / "listener.json")
    now = datetime(2026, 9, 1, 21, 0)
    _daily(state)["faceit_last_scrape"] = now.isoformat()
    assert not should_poll_faceit(state, now=now + timedelta(minutes=5))
    assert should_poll_faceit(state, now=now + timedelta(minutes=16))


def test_faceit_window_stretches_over_render_blocked_gap():
    now = datetime(2026, 9, 17, 12, 0)
    assert _faceit_window_hours(None, now) == 1
    assert _faceit_window_hours("bad", now) == 1
    assert _faceit_window_hours((now - timedelta(minutes=20)).isoformat(), now) == 1
    assert _faceit_window_hours((now - timedelta(hours=4)).isoformat(), now) == 5
    assert _faceit_window_hours((now - timedelta(hours=30)).isoformat(), now) == 24


def test_prune_keeps_one_faceit_card_per_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("cs2archive.listener.daemon.ROOT", tmp_path)
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


def test_star_gate_keeps_only_proven_povs(tmp_path: Path, monkeypatch):
    """Cherry-pick, do not queue every rating >= 1.0 POV (FACEIT rule)."""
    import cs2archive.listener.daemon as daemon

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    verdicts = {
        "donk": (True, False, 2.43, True, None),
        "magixx": (False, False, 1.10, True, None),
    }
    monkeypatch.setattr(
        "cs2archive.scoring.demand_eligibility",
        lambda nick, **kw: verdicts[nick],
    )
    paths = []
    for player in ("donk", "magixx"):
        rel = f"backlog/slug/high/{player}-anubis.json"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"player": player, "map": "Anubis"}),
                        encoding="utf-8")
        paths.append(rel)

    match = Match(match_id="2398725", url="https://www.hltv.org/matches/1/x",
                  slug="x", team1="spirit", team2="parivision", event="ESL")

    assert daemon._star_gate_cards(paths, match) == [paths[0]]


def test_star_gate_normalises_hltv_padded_nicks(monkeypatch):
    """HLTV ratings cells are padded (' donk ') — the index key must still hit."""
    import cs2archive.listener.daemon as daemon

    seen: list[str] = []

    def fake(nick, **kw):
        seen.append(nick)
        return (nick == "donk", False, 2.4 if nick == "donk" else None, True, None)

    monkeypatch.setattr("cs2archive.scoring.demand_eligibility", fake)

    assert daemon._star_eligible(" donk\u00a0") == (True, "")
    assert seen == ["donk"]
    assert daemon._star_eligible(" \u200bmagixx ")[0] is False


def test_star_gate_fails_closed_without_demand_payload(tmp_path: Path, monkeypatch):
    """A dead star refresh must narrow the gate, never widen it."""
    import cs2archive.listener.daemon as daemon

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(
        "cs2archive.scoring.demand_eligibility",
        lambda nick, **kw: (False, False, None, False, None),
    )
    rel = "backlog/slug/high/donk-anubis.json"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"player": "donk", "map": "Anubis"}),
                    encoding="utf-8")

    match = Match(match_id="1", url="https://www.hltv.org/matches/1/x", slug="x",
                  team1="spirit", team2="parivision", event="ESL")

    assert daemon._star_gate_cards([rel], match) == []


def test_indexed_team_tolerates_the_glued_event_suffix():
    """HLTV slugs leave the event on team 2 ("tyloo esl pro league season 24")."""
    import cs2archive.listener.daemon as daemon

    index = {"Natus Vincere": 2.0, "9z": 1.05}

    assert daemon._resolve_indexed_team("natus vincere esl pro league season 24",
                                        index) == "Natus Vincere"
    assert daemon._resolve_indexed_team("9z", index) == "9z"
    assert daemon._resolve_indexed_team("unknown team", index) == "unknown team"


def test_order_matches_by_demand_puts_the_biggest_fixture_first(monkeypatch):
    import cs2archive.listener.daemon as daemon

    monkeypatch.setattr(
        daemon, "_match_demand_points",
        lambda match, indexes=None: {"small": 10, "big": 500_000}[match.team1],
    )
    matches = [
        Match(match_id="1", url="u1", slug="s1", team1="small", team2="b",
              event="ESL"),
        Match(match_id="2", url="u2", slug="s2", team1="big", team2="a",
              event="ESL"),
    ]

    assert [m.match_id for m in daemon.order_matches_by_demand(matches, {})] == ["2", "1"]


def _guard_args(dry_run: bool = False):
    from types import SimpleNamespace
    return SimpleNamespace(dry_run=dry_run)


def _mk_guard_card(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return name


def test_faceit_scrape_queues_whatever_quality_passes_up_to_room(
        monkeypatch, tmp_path: Path):
    """No FACEIT-specific per-day cap: the quality gate decides how many
    POVs queue (up to the remaining daily room). A second scrape with a
    first card already queued must still scrape and may queue more."""
    import asyncio

    import cs2archive.listener.daemon as ml
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["faceit_queued"] = ["backlog/faceit/2026-09-29/high/old.json"]
    picks = [
        {"player": "donk", "match_id": "m1", "kills": 30, "deaths": 10,
         "kd": 3.0, "adr": 130.0, "map": "Mirage"},
        {"player": "m0NESY", "match_id": "m2", "kills": 28, "deaths": 12,
         "kd": 2.3, "adr": 120.0, "map": "Dust2"},
    ]
    scraped: dict = {}

    async def fake_discover(n=0, *, hours=0, count=25, skip_youtube_demand=True):
        scraped["n"] = n
        return (list(picks), [])

    monkeypatch.setattr(ml, "discover_faceit_tracks", fake_discover)
    monkeypatch.setattr(ml, "download_and_backlog", lambda picks: None)
    monkeypatch.setattr(ml, "rel_card_for_pick",
                        lambda pick: f"backlog/faceit/card-{pick['match_id']}.json")
    monkeypatch.setattr(ml, "_completed_faceit_keys", lambda: set())
    monkeypatch.setattr(ml, "remember_picks", lambda picks: None)
    enqueued: list[str] = []
    monkeypatch.setattr(ml, "_enqueue",
                        lambda st, cards, idx: enqueued.extend(cards))
    asyncio.run(ml._maybe_queue_faceit(_guard_args(), state, None))
    # room is 2 (nothing completed, queue empty) so both quality picks queue
    assert scraped["n"] == 2
    assert enqueued == ["backlog/faceit/card-m1.json",
                        "backlog/faceit/card-m2.json"]
    assert _daily(state)["faceit_queued"] == [
        "backlog/faceit/2026-09-29/high/old.json",
        "backlog/faceit/card-m1.json",
        "backlog/faceit/card-m2.json",
    ]


def test_faceit_scrape_respects_remaining_room(monkeypatch, tmp_path: Path):
    """With one slot already completed, the scrape asks for at most the
    one remaining slot."""
    import asyncio

    import cs2archive.listener.daemon as ml
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    state = State(tmp_path / "listener.json")
    _daily(state)["completed"] = ["backlog/faceit/2026-09-29/high/done.json"]
    scraped: dict = {}

    async def fake_discover(n=0, *, hours=0, count=25, skip_youtube_demand=True):
        scraped["n"] = n
        return ([], [])

    monkeypatch.setattr(ml, "discover_faceit_tracks", fake_discover)
    asyncio.run(ml._maybe_queue_faceit(_guard_args(), state, None))
    assert scraped["n"] == 1


def test_faceit_upload_respawn_dead_console_only(monkeypatch, tmp_path: Path):
    """A live upload console (spawn <30 min old) must not get a second
    spawn — upload_pending.py has no single-instance lock, so racing
    consoles double-upload the same video. A dead one (>=30 min) respawns,
    and the respawn runs alongside (not instead of) the scrape."""
    import asyncio

    import cs2archive.listener.daemon as ml
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    card = _mk_guard_card(tmp_path, "backlog/faceit/2026-09-29/high/p.json")
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["completed"] = [card]
    daily["faceit_queued"] = [card]
    spawns = daily.setdefault("faceit_upload_spawns", {})
    spawns[card] = {"at": datetime.now().isoformat(), "count": 1}
    spawned: list[str] = []
    monkeypatch.setattr(ml, "_start_upload_after_pipeline",
                        lambda c, dry: spawned.append(c))

    async def fake_discover(n=0, *, hours=0, count=25, skip_youtube_demand=True):
        return ([], [])

    monkeypatch.setattr(ml, "discover_faceit_tracks", fake_discover)
    asyncio.run(ml._maybe_queue_faceit(_guard_args(), state, None))
    assert spawned == []
    # the ledger copy is written back to daily — re-read it live
    _daily(state)["faceit_upload_spawns"][card] = {
        "at": (datetime.now() - timedelta(minutes=45)).isoformat(),
        "count": 1,
    }
    asyncio.run(ml._maybe_queue_faceit(_guard_args(), state, None))
    assert spawned == [card]
    assert _daily(state)["faceit_upload_spawns"][card]["count"] == 2


def test_faceit_upload_respawn_respects_spawn_cap(monkeypatch, tmp_path: Path):
    import asyncio

    import cs2archive.listener.daemon as ml
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    card = _mk_guard_card(tmp_path, "backlog/faceit/2026-09-29/high/p.json")
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["completed"] = [card]
    daily["faceit_queued"] = [card]
    spawns = daily.setdefault("faceit_upload_spawns", {})
    spawns[card] = {
        "at": (datetime.now() - timedelta(minutes=45)).isoformat(),
        "count": ml.FACEIT_UPLOAD_SPAWN_CAP,
    }
    spawned: list[str] = []

    async def fake_discover(n=0, *, hours=0, count=25, skip_youtube_demand=True):
        return ([], [])

    monkeypatch.setattr(ml, "discover_faceit_tracks", fake_discover)
    monkeypatch.setattr(ml, "_start_upload_after_pipeline",
                        lambda c, dry: spawned.append(c))
    asyncio.run(ml._maybe_queue_faceit(_guard_args(), state, None))
    assert spawned == []


def test_faceit_dry_run_scrapes_nothing_and_keeps_cooldown(
        monkeypatch, tmp_path: Path):
    import asyncio

    import cs2archive.listener.daemon as ml
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    card = _mk_guard_card(tmp_path, "backlog/faceit/2026-09-29/high/p.json")
    state = State(tmp_path / "listener.json")
    daily = _daily(state)
    daily["faceit_queued"] = [card]
    scraped: dict = {}

    async def fake_discover(n=0, *, hours=0, count=25, skip_youtube_demand=True):
        scraped["n"] = n
        return ([], [])

    monkeypatch.setattr(ml, "discover_faceit_tracks", fake_discover)
    asyncio.run(ml._maybe_queue_faceit(_guard_args(dry_run=True), state, None))
    assert "n" not in scraped
    # a dry run must not advance the 15-min FACEIT cooldown
    assert _daily(state).get("faceit_last_scrape") is None


def test_teams_stale_covers_missing_and_expired(tmp_path: Path):
    from cs2archive.listener.daemon import TEAMS_MAX_AGE, teams_stale

    now = datetime(2026, 9, 30, 12, 0)
    fresh = (now - timedelta(hours=1)).isoformat()
    expired = (now - timedelta(hours=TEAMS_MAX_AGE.total_seconds() / 3600)).isoformat()

    assert teams_stale(State(tmp_path / "a.json"), now=now) is True
    for bad_ts in (None, "not-a-date"):
        s = State(tmp_path / "b.json")
        s.data["teams"] = ["Spirit"]
        s.data["teams_updated_at"] = bad_ts
        assert teams_stale(s, now=now) is True

    s = State(tmp_path / "c.json")
    s.data["teams"] = ["Spirit"]
    s.data["teams_updated_at"] = fresh
    assert teams_stale(s, now=now) is False

    # exactly at the boundary counts as stale, so a 24h-old list does refresh
    s.data["teams_updated_at"] = expired
    assert teams_stale(s, now=now) is True


def test_refresh_teams_defaults_on_with_opt_out():
    from cs2archive.listener.daemon import build_parser

    parser = build_parser()
    assert parser.parse_args([]).refresh_teams is True
    assert parser.parse_args(["--refresh-teams"]).refresh_teams is True
    assert parser.parse_args(["--no-refresh-teams"]).refresh_teams is False
