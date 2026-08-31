"""Util-cam data lookup must key on HLTV match id, not map-slug substring.

Same fixture names recur across events (Porto vs EWC aurora-vs-m80 inferno,
legacy-vs-falcons mirage). A stem substring match binds the older event's
throws.parquet onto the new POV and overlay then aborts as 'missing clips'.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from overlay._common import _CS2UTIL_ROOT, _CS2UTIL_SCRIPTS, TICKRATE  # noqa: E402

for _p in (str(_CS2UTIL_SCRIPTS), str(_CS2UTIL_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from overlay import overlay_utilcams as ouc  # noqa: E402


def _touch_demo_dir(results: Path, project: str, demo_id: str, *, throws: bool = True) -> Path:
    d = results / project / "data" / f"demo={demo_id}"
    d.mkdir(parents=True)
    if throws:
        (d / "throws.parquet").write_bytes(b"not-a-real-parquet")
    return d


def _porto_inferno() -> Path:
    return Path("demos/hltv/2396939-aurora-vs-m80-blast-open-porto/aurora-vs-m80-m2-inferno.dem")


def _ewc_inferno_p1() -> Path:
    return Path(
        "demos/hltv/2396578-aurora-vs-m80-esports-world-cup/aurora-vs-m80-m2-inferno-p1.dem"
    )


def _porto_mirage() -> Path:
    return Path(
        "demos/hltv/2396938-legacy-vs-falcons-blast-open-porto/legacy-vs-falcons-m1-mirage.dem"
    )


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(ouc, "_cs2util_results_dir", lambda: root)
    return root


def test_does_not_bind_older_same_slug_split_demo(results: Path) -> None:
    _touch_demo_dir(results, "auto_extracted", "2396578-aurora-vs-m80-m2-inferno-p1")
    _touch_demo_dir(results, "auto_extracted", "2396578-aurora-vs-m80-m2-inferno-p2")
    assert ouc._find_demo_data_dir(_porto_inferno()) is None


def test_picks_this_match_when_older_slug_also_present(results: Path) -> None:
    _touch_demo_dir(results, "auto_extracted", "2396578-aurora-vs-m80-m2-inferno-p1")
    want = _touch_demo_dir(results, "auto_extracted", "2396939-aurora-vs-m80-m2-inferno")
    assert ouc._find_demo_data_dir(_porto_inferno()) == want


def test_does_not_bind_older_same_slug_unsplit(results: Path) -> None:
    _touch_demo_dir(results, "auto_extracted", "2396609-legacy-vs-falcons-m1-mirage")
    assert ouc._find_demo_data_dir(_porto_mirage()) is None


def test_p1_file_matches_same_match_p1_only(results: Path) -> None:
    want = _touch_demo_dir(results, "auto_extracted", "2396578-aurora-vs-m80-m2-inferno-p1")
    _touch_demo_dir(results, "auto_extracted", "2396939-aurora-vs-m80-m2-inferno")
    assert ouc._find_demo_data_dir(_ewc_inferno_p1()) == want


def test_unique_map_slug_still_resolves(results: Path) -> None:
    want = _touch_demo_dir(results, "auto_extracted", "2396938-legacy-vs-falcons-m3-dust2")
    _touch_demo_dir(results, "auto_extracted", "2396609-legacy-vs-falcons-m2-dust2")
    demo = Path(
        "demos/hltv/2396938-legacy-vs-falcons-blast-open-porto/legacy-vs-falcons-m3-dust2.dem"
    )
    assert ouc._find_demo_data_dir(demo) == want


def test_play_window_slack_and_gaps() -> None:
    play = {1: (1000, 2000), 2: (4000, 5000)}
    assert ouc._play_window_for_throw(1500, play) == 1
    slack = int(2 * TICKRATE)
    assert ouc._play_window_for_throw(4000 - slack, play) == 2
    assert ouc._play_window_for_throw(4000 - slack - 1, play) is None
    assert ouc._play_window_for_throw(2001, play) is None  # post-death / between rounds


def test_expected_clip_count_excludes_out_of_video_throws() -> None:
    play = {1: (2542, 5779), 2: (7251, 13126)}
    throws = [
        {"util_type": "smoke", "throw_tick": 3000},   # in play
        {"util_type": "he", "throw_tick": 6845},      # freeze of r2, beyond 2s slack
        {"util_type": "decoy", "throw_tick": 3100},   # in play but no clip
        {"util_type": "flash", "throw_tick": 8000},   # in play
    ]
    assert ouc._count_expected_flight_clips(throws, play) == 2
    assert ouc._count_expected_flight_clips(throws, None) == 3
