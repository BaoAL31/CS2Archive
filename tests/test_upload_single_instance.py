"""An upload must never start a second time for the same video.

2026-10-25: sh1ro Anubis went up twice (18eoCNB7Ig8, then piGhqtaxV5c). Step 6
of the pipeline rewrote ``upload_meta.json`` from scratch — hard-coding
``youtube_id: None`` / ``upload_status: "pending"`` — so a POV that was already
published looked brand new to the uploader, and ``upload_pending.py`` (which has
no single-instance lock; the listener works around that with a FACEIT-only spawn
ledger) happily uploaded it again.
"""
import json
from pathlib import Path

import pytest


def _meta_dir(tmp_path: Path, *, video_name: str = "video.mp4") -> Path:
    d = tmp_path / "youtube" / "run_overlay"
    d.mkdir(parents=True)
    (d / video_name).write_bytes(b"x" * 32)
    (d / "upload_meta.json").write_text(
        json.dumps({"title": "t", "video_path": str(d / video_name),
                    "upload_status": "pending", "youtube_id": None}),
        encoding="utf-8")
    return d


def test_pending_upload_is_found(tmp_path):
    from cs2archive.upload.upload_pending import find_pending

    d = _meta_dir(tmp_path)
    assert find_pending(d.parent) == [d / "upload_meta.json"]


def test_in_flight_upload_is_not_pending(tmp_path):
    """A live upload session means someone is already uploading this video."""
    from cs2archive.upload.upload_pending import find_pending

    d = _meta_dir(tmp_path)
    (d / "video.mp4.upload_session.json").write_text("{}", encoding="utf-8")
    assert find_pending(d.parent) == [], (
        "a second uploader must not pick up a video whose upload session is open")


def test_upload_lock_is_exclusive(tmp_path):
    from cs2archive.upload.upload_pending import UploadLock, UploadLocked

    lock_path = tmp_path / "upload.lock"
    with UploadLock(lock_path):
        with pytest.raises(UploadLocked):
            with UploadLock(lock_path):
                pass
    # released: the next run may take it again
    with UploadLock(lock_path):
        pass


def test_upload_lock_reports_the_holder(tmp_path):
    from cs2archive.upload.upload_pending import UploadLock, UploadLocked

    lock_path = tmp_path / "upload.lock"
    with UploadLock(lock_path):
        with pytest.raises(UploadLocked) as excinfo:
            with UploadLock(lock_path):
                pass
    assert "upload" in str(excinfo.value).lower()


# ── in-flight session guard (2026-11-01 YEKINDAR Dust2 duplicate) ──────────
#
# The old guard looked for ``<video>.upload_session.json``, which nothing has
# written since session state moved into upload_meta.json — so it could never
# fire. A killed uploader therefore looked identical to "never uploaded", and
# the next run created a second YouTube video.

def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_live_resumable_session_is_not_pending(tmp_path):
    from cs2archive.upload.upload_pending import find_pending

    d = _meta_dir(tmp_path)
    video = d / "video.mp4"
    import time as _time
    (d / "upload_meta.json").write_text(json.dumps({
        "title": "t", "video_path": str(video),
        "upload_status": "pending", "youtube_id": None,
        "resumable_uri": "https://upload.youtube.test/session?upload_id=abc",
        "video_size": 32,
        "upload_progress_at": _iso(_time.time() - 60),
    }), encoding="utf-8")
    assert find_pending(d.parent) == [], (
        "an uploader is mid-session; a second run must not start over")


def test_stale_resumable_session_is_pending_again(tmp_path):
    """A dead uploader's session must not park the video forever."""
    from cs2archive.upload.upload_pending import find_pending

    d = _meta_dir(tmp_path)
    video = d / "video.mp4"
    import time as _time
    (d / "upload_meta.json").write_text(json.dumps({
        "title": "t", "video_path": str(video),
        "upload_status": "pending", "youtube_id": None,
        "resumable_uri": "https://upload.youtube.test/session?upload_id=abc",
        "video_size": 32,
        "upload_progress_at": _iso(_time.time() - 6 * 3600),
    }), encoding="utf-8")
    assert find_pending(d.parent) == [d / "upload_meta.json"]


def test_untimestamped_legacy_session_is_pending(tmp_path):
    """Old metas with only resumable_uri cannot prove liveness — allow retry."""
    from cs2archive.upload.upload_pending import session_in_flight

    assert not session_in_flight({"resumable_uri": "u", "video_size": 1})
    assert session_in_flight({"resumable_uri": "u",
                              "upload_started_at": "2999-01-01T00:00:00+00:00"})


def test_session_claim_does_not_hijack_a_receipt(tmp_path):
    """A finished upload is never pending, whatever session fields remain."""
    from cs2archive.upload.upload_pending import find_pending

    d = _meta_dir(tmp_path)
    video = d / "video.mp4"
    (d / "upload_meta.json").write_text(json.dumps({
        "title": "t", "video_path": str(video),
        "upload_status": "completed", "youtube_id": "abc",
        "resumable_uri": "u", "upload_progress_at": "2999-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    assert find_pending(d.parent) == []


# ── channel-side duplicate guard ──────────────────────────────────────────

def _item(video_id, title, published="2026-11-01T10:00:00Z"):
    return {"snippet": {"title": title, "publishedAt": published,
                        "resourceId": {"videoId": video_id}}}


def test_duplicate_title_is_detected_case_and_space_insensitively():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    dup = duplicate_of_existing(
        [_item("abc", "YEKINDAR  (17-12) 1.97 Rating POV | Nemiga vs FURIA")],
        "yekindar (17-12) 1.97 rating pov | nemiga vs furia")
    assert dup and dup["video_id"] == "abc"


def test_different_title_is_not_a_duplicate():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    assert duplicate_of_existing(
        [_item("abc", "YEKINDAR POV | Nemiga vs FURIA | Dust2")],
        "YEKINDAR POV | Nemiga vs FURIA | Mirage") is None


def test_old_same_title_video_is_outside_the_lookback():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    assert duplicate_of_existing(
        [_item("old", "same title", "2026-01-01T00:00:00Z")],
        "same title") is None


def test_duplicate_without_video_id_is_ignored():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    item = {"snippet": {"title": "same title", "publishedAt": "2026-11-01T10:00:00Z"}}
    assert duplicate_of_existing([item], "same title") is None


# ── resumable session reuse (resume = same video, refusal = new video) ─────

def test_session_is_reused_only_for_the_same_bytes():
    from cs2archive.upload.upload_youtube import reusable_session_uri

    meta = {"video_path": "youtube/run/video.mp4", "video_size": 100,
            "resumable_uri": "https://upload.youtube.test/s?upload_id=x"}
    assert reusable_session_uri(meta, "youtube/run/video.mp4", 100)[0]
    uri, reason = reusable_session_uri(meta, "youtube/run/video.mp4", 101)
    assert uri is None
    assert reason and "video changed" in reason


def test_session_belonging_to_another_file_is_refused():
    from cs2archive.upload.upload_youtube import reusable_session_uri

    meta = {"video_path": "youtube/a/video.mp4", "video_size": 100,
            "resumable_uri": "https://upload.youtube.test/s?upload_id=x"}
    uri, reason = reusable_session_uri(meta, "youtube/b/video.mp4", 100)
    assert uri is None and reason and "another file" in reason


def test_no_stored_session_means_a_fresh_insert():
    from cs2archive.upload.upload_youtube import reusable_session_uri

    assert reusable_session_uri({}, "video.mp4", 10) == (None, None)


def test_rerendered_pov_is_still_a_duplicate_when_stats_changed():
    """A re-render changes (k-d)/rating/ELO digits but not the POV identity."""
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    dup = duplicate_of_existing(
        [_item("older", "YEKINDAR (17-12) 1.97 Rating POV | Nemiga vs FURIA "
                        "| Dust2 | PGL Masters Bucharest 2026")],
        "YEKINDAR (19-14) 1.62 Rating POV | Nemiga vs FURIA "
        "| Dust2 | PGL Masters Bucharest 2026")
    assert dup and dup["video_id"] == "older" and dup["exact_title"] is False


def test_faceit_elo_change_is_still_a_duplicate():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    dup = duplicate_of_existing(
        [_item("f1", "PR (25-12) 4236 ELO vs ~3441 ELOs | Ancient "
                     "| FACEIT CS2 POV + VOICE COMMS")],
        "PR (25-12) 4260 ELO vs ~3502 ELOs | Ancient "
        "| FACEIT CS2 POV + VOICE COMMS")
    assert dup and dup["video_id"] == "f1"


def test_exact_title_match_is_reported_as_exact():
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    dup = duplicate_of_existing([_item("abc", "Same Title")], "same  title")
    assert dup and dup["exact_title"] is True


def test_another_match_is_not_a_duplicate():
    """Digit collapse must not swallow a genuinely different fixture."""
    from cs2archive.upload.upload_youtube import duplicate_of_existing

    assert duplicate_of_existing(
        [_item("x", "YEKINDAR (17-12) 1.97 Rating POV | Nemiga vs FURIA "
                    "| Dust2 | PGL Masters Bucharest 2026")],
        "YEKINDAR (17-12) 1.97 Rating POV | FURIA vs Spirit "
        "| Dust2 | PGL Masters Bucharest 2026") is None
