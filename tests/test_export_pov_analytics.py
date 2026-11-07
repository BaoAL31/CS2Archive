from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from cs2archive.upload import export_pov_analytics as analytics


def test_window_uses_full_pacific_days_across_dst_change():
    # November 1 UTC is still October 31 Pacific. DST changes inside the window.
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert analytics.analysis_window("2026-11-01T01:00:00Z", now) == ("2026-11-01", "2026-11-07")
    assert analytics.analysis_window("2026-11-07T01:00:00Z", now) is None


def test_empty_analytics_report_is_not_a_zero_view_observation():
    assert analytics.report_values({"columnHeaders": [{"name": "views"}]}) == {}
    assert analytics.report_values({"columnHeaders": [{"name": "views"}], "rows": [[0]]}) == {"views": 0}


def test_wrong_google_channel_is_rejected_before_query(monkeypatch):
    from cs2archive.faceit import update_player_demand
    monkeypatch.setattr(update_player_demand, "recognised_aliases", lambda: {"donk": "donk"})
    youtube, service = Mock(), Mock()
    youtube.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": "other"}]}
    row = dict(channel="CS2 Archive", channel_id="ours", video_id="video", published_at="2026-10-01T00:00:00Z")
    with pytest.raises(RuntimeError, match="does not match"):
        analytics.collect([row], youtube, service, datetime(2026, 10, 28, tzinfo=timezone.utc), 60)
    service.reports.assert_not_called()


def test_collection_uses_fixed_window_and_newest_capture(monkeypatch):
    from cs2archive.faceit import update_player_demand
    monkeypatch.setattr(update_player_demand, "recognised_aliases", lambda: {"donk": "donk"})
    youtube, service = Mock(), Mock()
    youtube.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": "ours"}]}
    headers = [{"name": name} for name in analytics.METRICS.split(",")]
    service.reports.return_value.query.return_value.execute.return_value = {
        "columnHeaders": headers, "rows": [[10, 20, 120, 20, 0]]}
    old = dict(channel="CS2 Archive", channel_id="ours", video_id="video", published_at="2026-10-01T09:00:00Z",
               title="donk vs team | FACEIT", primary_player="donk", duration_seconds="600", captured_at="2026-10-10")
    fresh = dict(old, captured_at="2026-10-28", title="donk vs rival | FACEIT")
    report = analytics.collect([fresh, old], youtube, service, datetime(2026, 10, 28, tzinfo=timezone.utc), 60)
    assert len(report["outcomes"]) == 1
    assert report["outcomes"][0]["title"] == fresh["title"]
    query = service.reports.return_value.query.call_args.kwargs
    assert (query["startDate"], query["endDate"], query["filters"]) == ("2026-10-02", "2026-10-08", "video==video")
    assert report["groups"]["all"]["median_views"] == 10


@pytest.mark.parametrize("status,expected_attempts", [(503, 3), (403, 1)])
def test_request_retries_are_bounded_and_do_not_retry_permissions(monkeypatch, status, expected_attempts):
    from googleapiclient.errors import HttpError
    import httplib2
    request = Mock()
    request.execute.side_effect = HttpError(httplib2.Response({"status": status}), b'{}')
    monkeypatch.setattr(analytics.time, "sleep", lambda delay: None)
    with pytest.raises(HttpError):
        analytics.execute(request)
    assert request.execute.call_count == expected_attempts
