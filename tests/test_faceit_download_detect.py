from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scrapers.faceit import (
    _finished_match_archive,
    _inflight_match_archive,
    _is_partial_download,
    _match_download_files,
)


def test_partial_and_finished_detection(tmp_path: Path):
    mid = "1-58e04f44-fc5a-4b36-8ccd-2a97f2d674f0"
    done = tmp_path / f"{mid}-1-1.dem.zst"
    part = tmp_path / f"{mid}-1-1.dem.zst.crdownload"
    other = tmp_path / "1-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb-1-1.dem.zst"
    done.write_bytes(b"x" * 2_000_000)
    part.write_bytes(b"y" * 500_000)
    other.write_bytes(b"z" * 2_000_000)

    files = _match_download_files(mid, [tmp_path])
    assert done in files and part in files
    assert other not in files
    assert _is_partial_download(part)
    assert not _is_partial_download(done)
    assert _finished_match_archive(mid, [tmp_path]) == done
    assert _inflight_match_archive(mid, [tmp_path]) == part


def test_inflight_only(tmp_path: Path):
    mid = "1-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    part = tmp_path / f"{mid}-1-1.dem.zst.crdownload"
    part.write_bytes(b"y" * 1000)
    assert _finished_match_archive(mid, [tmp_path]) is None
    assert _inflight_match_archive(mid, [tmp_path]) == part
