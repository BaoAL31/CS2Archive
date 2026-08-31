from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "misc"))

import scrape_pov_channels as scraper


def test_channel_reference_accepts_video_handle_and_channel_url():
    assert scraper._channel_reference(
        "https://www.youtube.com/watch?v=DGN4BfQjXK0"
    ) == ("video", "DGN4BfQjXK0")
    assert scraper._channel_reference("@limcspov") == ("forHandle", "limcspov")
    assert scraper._channel_reference(
        "https://www.youtube.com/channel/UC123"
    ) == ("id", "UC123")


def test_duration_seconds_parses_youtube_duration():
    assert scraper._duration_seconds("PT18M12S") == 1092
    assert scraper._duration_seconds("PT1H2M3S") == 3723
    assert scraper._duration_seconds("") == 0


def test_title_features_extract_market_signals():
    features = scraper._title_features(
        "donk (34-11) 5513 ELO FACEIT Mirage | POV + VOICECOMMS + Input Overlay",
    )

    assert features["primary_player"] == "donk"
    assert features["kills"] == 34
    assert features["deaths"] == 11
    assert features["elo"] == 5513
    assert features["map"] == "mirage"
    assert features["has_faceit"] is True
    assert features["has_voicecomms"] is True
    assert features["has_input_overlay"] is True


def test_player_features_separate_teammates_from_opponents():
    aliases = {
        "niko": "NiKo",
        "teses": "TeSeS",
        "nertz": "NertZ",
        "jl": "jL",
    }

    features = scraper._player_features(
        "NiKo (24-12) w/TeSeS vs jL/NertZ | Anubis POV",
        aliases,
    )

    assert features["primary_player"] == "NiKo"
    assert json.loads(features["secondary_players"]) == ["TeSeS"]
    assert features["secondary_player_count"] == 1
    assert features["party_type"] == "duo"
    assert json.loads(features["opponent_pro_players"]) == ["jL", "NertZ"]
    assert json.loads(features["all_pro_players"]) == ["NiKo", "TeSeS", "jL", "NertZ"]


def test_player_features_handle_duo_with_title():
    aliases = {"donk": "donk", "magixx": "magixx"}
    features = scraper._player_features(
        "donk (23-14) DUO with MAGIXX - FACEIT TOP #1",
        aliases,
    )

    assert features["primary_player"] == "donk"
    assert json.loads(features["secondary_players"]) == ["magixx"]
    assert features["party_type"] == "duo"


def test_player_features_keep_unlisted_primary_ahead_of_known_pros():
    aliases = {"s1mple": "s1mple", "senzu": "Senzu"}
    features = scraper._player_features(
        "mzinho 31 KILLS WITH S1MPLE & SENZU ON STREAM",
        aliases,
    )

    assert features["primary_player"] == "mzinho"
    assert json.loads(features["secondary_players"]) == ["s1mple", "Senzu"]
    assert features["party_type"] == "trio"


def test_real_channel_title_samples_use_recognised_pro_roster():
    samples = [
        (
            "donk (23-14) DUO with MAGIXX - FACEIT TOP #1 4400 ELO",
            "donk",
            ["magixx"],
            [],
        ),
        (
            "mzinho 31 KILLS WITH S1MPLE & SENZU ON STREAM | CS2",
            "mzinho",
            ["s1mple", "Senzu"],
            [],
        ),
        (
            "NiKo w/TeSeS vs NertZ | avg 3.3K ELO | Mirage POV",
            "NiKo",
            ["TeSeS"],
            ["NertZ"],
        ),
        (
            "NiKo (24-12) w/TeSeS vs jL/Ex3rcice | avg 3.2K ELO",
            "NiKo",
            ["TeSeS"],
            ["jL", "Ex3rcice"],
        ),
    ]

    for title, primary, secondary, opponents in samples:
        features = scraper._player_features(title)
        assert features["primary_player"] == primary
        assert json.loads(features["secondary_players"]) == secondary
        assert json.loads(features["opponent_pro_players"]) == opponents


def test_metric_row_calculates_normalized_performance():
    channel = {
        "channel_id": "UC1",
        "channel": "POV",
        "subscribers": 1000,
    }
    item = {
        "id": "video1",
        "snippet": {
            "title": "donk POV",
            "description": "Match details #cs2",
            "tags": ["donk", "cs2"],
            "categoryId": "20",
            "publishedAt": "2026-08-26T00:00:00Z",
            "thumbnails": {"high": {"url": "https://img.example/high.jpg"}},
        },
        "statistics": {
            "viewCount": "2000",
            "likeCount": "100",
            "commentCount": "20",
        },
        "contentDetails": {"duration": "PT20M"},
    }

    row = scraper._metric_row(
        item,
        channel,
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert row["age_days"] == 2
    assert row["views_per_day"] == 1000
    assert row["views_per_subscriber"] == 2
    assert row["like_rate"] == 0.05
    assert row["duration_seconds"] == 1200
    assert row["description"] == "Match details #cs2"
    assert row["tags"] == '["donk", "cs2"]'
    assert row["hashtags"] == "#cs2"
    assert row["tag_count"] == 2
    assert row["publish_weekday"] == "Wednesday"
    assert row["thumbnail_url"] == "https://img.example/high.jpg"


def test_write_outputs_creates_snapshot_summary_and_history(tmp_path):
    channels = [
        {
            "channel_id": "UC1",
            "channel": "POV",
            "subscribers": 1000,
            "channel_views": 50000,
            "channel_videos": 20,
            "uploads_playlist": "UU1",
        }
    ]
    row = {field: None for field in scraper.VIDEO_FIELDS}
    row.update(
        {
            "captured_at": "2026-08-28T00:00:00+00:00",
            "channel_id": "UC1",
            "channel": "POV",
            "video_id": "video1",
            "title": "donk POV",
            "published_at": "2026-08-26T00:00:00+00:00",
            "views": 2000,
            "views_per_day": 1000,
            "url": "https://www.youtube.com/watch?v=video1",
        }
    )

    paths = scraper.write_outputs(tmp_path, channels, [row])

    assert len(paths) == 3
    assert all(path.is_file() for path in paths)
    summary = json.loads(paths[1].read_text(encoding="utf-8"))
    assert summary[0]["median_views"] == 2000
    with paths[2].open(encoding="utf-8-sig", newline="") as handle:
        history = list(csv.DictReader(handle))
    assert history[0]["video_id"] == "video1"


def test_write_outputs_upserts_history_by_video_id(tmp_path):
    channels = [
        {
            "channel_id": "UC1",
            "channel": "POV",
            "subscribers": 1000,
            "channel_views": 50000,
            "channel_videos": 20,
            "uploads_playlist": "UU1",
        }
    ]
    row = {field: None for field in scraper.VIDEO_FIELDS}
    row.update(
        {
            "captured_at": "2026-08-28T00:00:00+00:00",
            "channel_id": "UC1",
            "channel": "POV",
            "video_id": "video1",
            "title": "donk POV",
            "published_at": "2026-08-26T00:00:00+00:00",
            "views": 2000,
            "views_per_day": 1000,
            "url": "https://www.youtube.com/watch?v=video1",
        }
    )
    scraper.write_outputs(tmp_path, channels, [row])
    updated = dict(row)
    updated["captured_at"] = "2026-08-29T00:00:00+00:00"
    updated["views"] = 4000
    scraper.write_outputs(tmp_path, channels, [updated])

    history_path = tmp_path / "video_history.csv"
    with history_path.open(encoding="utf-8-sig", newline="") as handle:
        history = list(csv.DictReader(handle))
    assert len(history) == 1
    assert history[0]["views"] == "4000"


def test_stale_video_ids_skip_old_history():
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    rows = [
        {"video_id": "old", "published_at": "2026-01-01T00:00:00+00:00"},
        {"video_id": "fresh", "published_at": "2026-08-27T00:00:00+00:00"},
    ]
    assert scraper.stale_video_ids(rows, now, 7) == {"old"}
