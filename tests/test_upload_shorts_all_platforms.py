"""A Short is published to all four platforms unless an operator says otherwise.

The per-platform opt-out flags (``--skip-tiktok`` / ``--skip-instagram`` /
``--skip-facebook``) exist for a genuinely broken platform, but they are hidden
from ``--help`` and inert unless ``CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD`` is set, so
an agent cannot narrow a Short to one platform on its own initiative. These
tests lock that gate in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_SHORTS = PROJECT_ROOT / "cs2archive" / "upload" / "upload_youtube_shorts.py"
UPLOAD_PENDING_SHORTS = PROJECT_ROOT / "cs2archive" / "upload" / "upload_pending_shorts.py"

PLATFORM_FLAGS = ("--skip-tiktok", "--skip-instagram", "--skip-facebook")
GATE_ENV = "CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD"


def _run(script: Path, *args: str, authorized: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != GATE_ENV}
    if authorized:
        env[GATE_ENV] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("script", [UPLOAD_SHORTS, UPLOAD_PENDING_SHORTS])
@pytest.mark.parametrize("flag", PLATFORM_FLAGS)
def test_platform_opt_out_is_hidden_from_help(script, flag):
    r = _run(script, "--help")
    assert r.returncode == 0, r.stderr
    assert flag not in r.stdout


@pytest.mark.parametrize("script", [UPLOAD_SHORTS, UPLOAD_PENDING_SHORTS])
@pytest.mark.parametrize("flag", PLATFORM_FLAGS)
def test_platform_opt_out_is_refused_without_authorization(script, flag):
    r = _run(script, flag)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing a partial upload" in r.stdout
    assert GATE_ENV in r.stdout


def test_authorized_partial_upload_passes_the_gate():
    # The gate is the only thing under test: an authorized run gets past it and
    # fails later on the missing video (exit 1), not on authorization (exit 2).
    r = _run(UPLOAD_SHORTS, "nope.mp4", "--skip-tiktok", authorized=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refusing a partial upload" not in r.stdout


def _meta(**overrides) -> dict:
    meta = {
        "upload_status": "completed",
        "youtube_id": "abc123",
        "tiktok_status": "scheduled",
        "instagram_status": "queued",
        "facebook_status": "scheduled",
    }
    meta.update(overrides)
    return meta


def test_fully_uploaded_meta_is_not_pending():
    from cs2archive.upload.upload_pending_shorts import _needs_upload

    assert not _needs_upload(_meta())


@pytest.mark.parametrize(
    "field",
    ["tiktok_status", "instagram_status", "facebook_status"],
)
def test_a_missing_social_platform_keeps_the_meta_pending(field):
    from cs2archive.upload.upload_pending_shorts import _needs_upload

    assert _needs_upload(_meta(**{field: None}))


@pytest.mark.parametrize("platform", ["tiktok", "instagram", "facebook"])
def test_an_authorized_skip_settles_that_platform(platform):
    from cs2archive.upload.upload_pending_shorts import _needs_upload

    meta = _meta(**{f"{platform}_status": None, "skipped_platforms": [platform]})
    assert not _needs_upload(meta)


def test_find_pending_picks_up_a_social_only_meta(tmp_path):
    from cs2archive.upload.upload_pending_shorts import find_pending

    video = tmp_path / "short.mp4"
    video.write_bytes(b"x")
    meta_path = tmp_path / "upload_meta_shorts.json"
    meta_path.write_text(
        json.dumps(_meta(tiktok_status=None, video_path=str(video))),
        encoding="utf-8",
    )

    assert find_pending(tmp_path) == [meta_path]
