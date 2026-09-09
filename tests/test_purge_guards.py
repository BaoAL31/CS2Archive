"""Tests for purge guards: post-upload purge + pipeline end-of-run gate."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

import upload_pending


def _meta(adir: Path, status: str, vid: str | None = "v") -> Path:
    adir.mkdir(parents=True, exist_ok=True)
    mp = adir / "upload_meta.json"
    mp.write_text(json.dumps({
        "upload_status": status,
        "youtube_id": "yt123" if status == "completed" else None,
        "video_path": vid,
    }))
    return mp


def test_purge_waits_for_all_variants() -> None:
    """Overlay done but raw pending -> render dir must survive."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        import unittest.mock as mock
        with mock.patch.object(upload_pending, "PROJECT_ROOT", root):
            (root / ".pipeline").mkdir()
            render = root / "renders" / "pov-x"
            render.mkdir(parents=True)
            (render / "combined.mp4").write_bytes(b"\0")
            (root / ".pipeline" / "run1.json").write_text(json.dumps(
                {"data": {"render_dir": str(render)}}))
            yt = root / "youtube"
            _meta(yt / "run1", "pending")
            _meta(yt / "run1_overlay", "completed")
            overlay_meta = yt / "run1_overlay" / "upload_meta.json"
            upload_pending.maybe_purge_renders(overlay_meta, False)
            assert render.exists(), "raw still pending — must keep renders"


def test_purge_after_all_done() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        import unittest.mock as mock
        with mock.patch.object(upload_pending, "PROJECT_ROOT", root):
            (root / ".pipeline").mkdir()
            render = root / "renders" / "pov-x"
            render.mkdir(parents=True)
            (render / "combined.mp4").write_bytes(b"\0" * 2048)
            (root / ".pipeline" / "run1.json").write_text(json.dumps(
                {"data": {"render_dir": str(render)}}))
            yt = root / "youtube"
            _meta(yt / "run1", "completed")
            _meta(yt / "run1_overlay", "completed")
            upload_pending.maybe_purge_renders(
                yt / "run1_overlay" / "upload_meta.json", False)
            assert not render.exists(), "all uploaded — renders purged"


def test_purge_missing_state_no_crash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        import unittest.mock as mock
        with mock.patch.object(upload_pending, "PROJECT_ROOT", root):
            yt = root / "youtube"
            mp = _meta(yt / "run9_overlay", "completed")
            upload_pending.maybe_purge_renders(mp, False)  # must not raise


def test_purge_dry_run_keeps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        import unittest.mock as mock
        with mock.patch.object(upload_pending, "PROJECT_ROOT", root):
            (root / ".pipeline").mkdir()
            render = root / "renders" / "pov-x"
            render.mkdir(parents=True)
            (render / "f.mp4").write_bytes(b"\0")
            (root / ".pipeline" / "run1.json").write_text(json.dumps(
                {"data": {"render_dir": str(render)}}))
            yt = root / "youtube"
            _meta(yt / "run1_overlay", "completed")
            upload_pending.maybe_purge_renders(
                yt / "run1_overlay" / "upload_meta.json", False, dry_run=True)
            assert render.exists(), "dry-run must not delete"


def _should_purge(end_step: int, no_cleanup: bool) -> bool:
    from pipeline import Pipeline
    fake = types.SimpleNamespace(
        end_step=end_step,
        args=types.SimpleNamespace(no_cleanup=no_cleanup),
    )
    return Pipeline.should_purge_after_run(fake)


def test_purge_gate() -> None:
    assert _should_purge(6, True) is False  # default full run keeps
    assert _should_purge(1, True) is False  # partial --until keeps
    assert _should_purge(6, False) is True  # --cleanup purges
    assert _should_purge(7, False) is True  # step 7 purges


if __name__ == "__main__":
    test_purge_waits_for_all_variants()
    test_purge_after_all_done()
    test_purge_missing_state_no_crash()
    test_purge_dry_run_keeps()
    test_purge_gate()
    print("PASS")
