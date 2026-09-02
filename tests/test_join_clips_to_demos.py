"""Clip Observation → local HLTV demo join (match id + map in the title)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.join_clips_to_demos import (
    build_join,
    demos_for_map,
    index_hltv_demo_dirs,
    map_slug_from_title,
)


def test_map_slug_from_allstar_title():
    assert map_slug_from_title("Dust 2 1V3 Ace Clutch") == "dust2"
    assert map_slug_from_title("latto Dust2 4K") == "dust2"
    assert map_slug_from_title("Nuke Wallbang 1V3 4K Clutch") == "nuke"
    assert map_slug_from_title("Ancient AK-47 3K") == "ancient"
    assert map_slug_from_title("no map here") is None


def test_index_prefers_match_id_folder(tmp_path: Path):
    demos = tmp_path / "demos" / "hltv"
    numbered = demos / "2396941-vitality-vs-legacy-blast-open-porto"
    numbered.mkdir(parents=True)
    (demos / "vitality-vs-legacy").mkdir()
    dirs = index_hltv_demo_dirs(demos, history=[], root=tmp_path)
    assert dirs["2396941"] == numbered


def test_index_falls_back_to_download_history(tmp_path: Path):
    demos = tmp_path / "demos" / "hltv"
    folder = demos / "falcons-vs-legacy"
    folder.mkdir(parents=True)
    demo = folder / "falcons-vs-legacy-m1-nuke.dem"
    demo.write_bytes(b"x")
    hist = [{"match_id": "2394228", "demo_path": str(demo.relative_to(tmp_path))}]
    dirs = index_hltv_demo_dirs(demos, history=hist, root=tmp_path)
    assert dirs["2394228"] == folder


def test_demos_for_map_picks_map_and_split_parts(tmp_path: Path):
    folder = tmp_path / "match"
    folder.mkdir()
    (folder / "a-vs-b-m1-mirage.dem").write_bytes(b"x")
    (folder / "a-vs-b-m2-nuke-p1.dem").write_bytes(b"x")
    (folder / "a-vs-b-m2-nuke-p2.dem").write_bytes(b"x")
    (folder / "a-vs-b-m3-dust2.dem").write_bytes(b"x")
    nuke = [p.name for p in demos_for_map(folder, "nuke", min_bytes=0)]
    assert nuke == ["a-vs-b-m2-nuke-p1.dem", "a-vs-b-m2-nuke-p2.dem"]
    assert demos_for_map(folder, "mirage", min_bytes=0)[0].name.endswith("mirage.dem")


def test_build_join_assigns_clip_to_map_demo(tmp_path: Path):
    demos = tmp_path / "demos" / "hltv"
    folder = demos / "2396941-vitality-vs-legacy"
    folder.mkdir(parents=True)
    demo = folder / "vitality-vs-legacy-m2-dust2.dem"
    demo.write_bytes(b"x")
    jsonl = tmp_path / "allstar.jsonl"
    jsonl.write_text(
        json.dumps({
            "match_id": "2396941",
            "clips": [{
                "clip_id": "c1",
                "steamid": "76561198850020186",
                "player": "latto",
                "match_id": "2396941",
                "title": "Dust 2 1V3 Ace Clutch",
                "label": "latto Dust 2 1V3 Ace Clutch",
                "round": 4,
            }],
        })
        + "\n",
        encoding="utf-8",
    )
    payload = build_join(
        allstar_path=jsonl,
        to_path=tmp_path / "missing.jsonl",
        demos_root=demos,
        history=[],
        min_bytes=0,
        root=tmp_path,
    )
    clip = payload["clips"][0]
    assert clip["status"] == "joined"
    assert clip["map"] == "dust2"
    assert clip["demo_path"] == "demos/hltv/2396941-vitality-vs-legacy/vitality-vs-legacy-m2-dust2.dem"
    assert payload["summary"]["joined_clips"] == 1


def test_build_join_missing_match_and_missing_map(tmp_path: Path):
    demos = tmp_path / "demos" / "hltv"
    folder = demos / "2390001-a-vs-b"
    folder.mkdir(parents=True)
    (folder / "a-vs-b-m1-nuke.dem").write_bytes(b"x")
    jsonl = tmp_path / "allstar.jsonl"
    jsonl.write_text(
        json.dumps({
            "match_id": "2390001",
            "clips": [{
                "clip_id": "ok-map-missing-file",
                "title": "Mirage 4K",
                "round": 2,
            }],
        })
        + "\n"
        + json.dumps({
            "match_id": "2399999",
            "clips": [{
                "clip_id": "no-folder",
                "title": "Nuke 4K",
                "round": 1,
            }],
        })
        + "\n",
        encoding="utf-8",
    )
    payload = build_join(
        allstar_path=jsonl,
        to_path=tmp_path / "missing.jsonl",
        demos_root=demos,
        history=[],
        min_bytes=0,
        root=tmp_path,
    )
    by_id = {c["clip_id"]: c["status"] for c in payload["clips"]}
    assert by_id["ok-map-missing-file"] == "no_map_demo"
    assert by_id["no-folder"] == "no_match_demo"


def test_clip_match_id_mismatch_does_not_use_page_demo(tmp_path: Path):
    demos = tmp_path / "demos" / "hltv"
    page = demos / "2397311-big-vs-nemiga"
    page.mkdir(parents=True)
    (page / "big-vs-nemiga-m1-nuke.dem").write_bytes(b"x")
    jsonl = tmp_path / "allstar.jsonl"
    jsonl.write_text(
        json.dumps({
            "match_id": "2397311",
            "clips": [{
                "clip_id": "other-match",
                "match_id": "2393204",
                "title": "Nuke 4K",
                "round": 1,
            }],
        })
        + "\n",
        encoding="utf-8",
    )
    payload = build_join(
        allstar_path=jsonl,
        to_path=tmp_path / "missing.jsonl",
        demos_root=demos,
        history=[],
        min_bytes=0,
        root=tmp_path,
    )
    clip = payload["clips"][0]
    assert clip["status"] == "match_id_mismatch"
    assert clip["demo_path"] is None
    assert clip["match_id"] == "2393204"
    assert clip["page_match_id"] == "2397311"
