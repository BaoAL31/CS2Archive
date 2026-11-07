"""Re-running step 6 must not erase the record that a POV was published.

2026-10-25: rerunning steps 4-6 for the focus-loss fix overwrote
``upload_meta.json`` with ``youtube_id: null`` / ``upload_status: "pending"``,
which is what let the already-published sh1ro Anubis be uploaded a second time.
"""
from cs2archive.pov.pipeline import merge_upload_meta


def test_fresh_meta_is_returned_when_nothing_existed():
    fresh = {"title": "t", "youtube_id": None, "upload_status": "pending"}
    assert merge_upload_meta(None, fresh) == fresh


def test_published_upload_survives_a_step6_rewrite():
    existing = {
        "title": "old", "video_path": "/old/video.mp4",
        "youtube_id": "abc123", "upload_status": "completed",
        "bilibili_aid": "777", "bilibili_upload_status": "completed",
    }
    fresh = {"title": "new", "video_path": "/new/video.mp4",
             "youtube_id": None, "upload_status": "pending"}
    merged = merge_upload_meta(existing, fresh)
    # the rewrite's own fields still win …
    assert merged["title"] == "new"
    assert merged["video_path"] == "/new/video.mp4"
    # … but a published id is not forgotten, so the uploader cannot re-upload it
    assert merged["youtube_id"] == "abc123"
    assert merged["upload_status"] == "completed"
    assert merged["bilibili_aid"] == "777"
    assert merged["bilibili_upload_status"] == "completed"
    assert merged["rebuilt_after_upload"] is True


def test_pending_existing_meta_stays_pending():
    existing = {"youtube_id": None, "upload_status": "pending"}
    fresh = {"title": "new", "youtube_id": None, "upload_status": "pending"}
    merged = merge_upload_meta(existing, fresh)
    assert merged["youtube_id"] is None
    assert merged["upload_status"] == "pending"
    assert "rebuilt_after_upload" not in merged


def test_unparsable_existing_meta_falls_back_to_fresh():
    fresh = {"title": "new", "youtube_id": None, "upload_status": "pending"}
    assert merge_upload_meta("not a dict", fresh) == fresh
