"""Tests for render resume: skip-set, stale-sequence clearing, rename mapping."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

import render_pov


def _big(folder: Path, name: str, size: int = 2_000_000) -> Path:
    p = folder / name
    p.write_bytes(b"\0" * size)
    return p


def test_rendered_rounds_skips_usable_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _big(folder, "round-001-tick-100-to-200.mp4")  # usable -> skip
        _big(folder, "round-002-tick-300-to-400.mp4", size=100)  # crashed -> redo
        _big(folder, "round-abc-tick-1-to-2.mp4")  # bad name -> ignored
        (folder / "other.txt").write_bytes(b"x")
        assert render_pov.rendered_rounds(folder) == {1}


def test_rendered_rounds_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert render_pov.rendered_rounds(Path(tmp)) == set()


def test_clear_stale_sequences() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        stale_dir = folder / "5-sequence"
        stale_dir.mkdir()
        (stale_dir / "video.mp4").write_bytes(b"\0")
        (folder / "sequence-9-tick-1-to-2.mp4").write_bytes(b"\0")
        keep_clip = _big(folder, "round-001-tick-100-to-200.mp4")
        keep_txt = folder / "notes.txt"
        keep_txt.write_bytes(b"x")
        removed = render_pov._clear_stale_sequences(folder)
        assert removed == 2
        assert not stale_dir.exists()
        assert not (folder / "sequence-9-tick-1-to-2.mp4").exists()
        assert keep_clip.exists()  # round clips untouched
        assert keep_txt.exists()  # unrelated files untouched


def test_clear_stale_sequences_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert render_pov._clear_stale_sequences(Path(tmp)) == 0


def test_rename_new_format_positional() -> None:
    """Fresh seq dirs map seq_num -> global_rounds[seq_num-1] with analysis ticks."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "csdm_analysis.json").write_text(json.dumps({"rounds": [
            {"number": 13, "startTick": 142971, "endTick": 148348},
            {"number": 14, "startTick": 148668, "endTick": 154215},
        ]}))
        for i in (1, 2):
            d = folder / f"{i}-sequence"
            d.mkdir()
            (d / "video.mp4").write_bytes(b"vid%d" % i)
        salvaged = render_pov._rename_sequence_files(folder, [13, 14])
        assert salvaged == {13, 14}
        assert (folder / "round-013-tick-142971-to-148348.mp4").is_file()
        assert (folder / "round-014-tick-148668-to-154215.mp4").is_file()
        # source videos consumed (round clips hold the bytes now)
        assert not (folder / "1-sequence" / "video.mp4").exists()
        assert not (folder / "2-sequence" / "video.mp4").exists()


if __name__ == "__main__":
    test_rendered_rounds_skips_usable_only()
    test_rendered_rounds_empty()
    test_clear_stale_sequences()
    test_clear_stale_sequences_nothing()
    test_rename_new_format_positional()
    print("PASS")
