"""Compare edit_timeline segments covering global kills 0-11 to golden batch-1."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
GOAL = json.loads(
    (ROOT / "scripts/highlights/fixtures/cache_batch1_goal_segments.json").read_text(encoding="utf-8")
)["segments"]
KEYS = ("start_tick", "end_tick", "pov_steam_id", "segment_type", "kill_indices")


def batch1_segs(data: dict) -> list[dict]:
    out = []
    for s in data["segments"]:
        kis = s.get("kill_indices", [])
        if not kis or max(kis) > 11 or min(kis) < 0:
            continue
        out.append({k: s[k] for k in KEYS})
    out.sort(key=lambda x: min(x["kill_indices"]))
    return out


def diff_label(name: str, path: Path) -> int:
    if not path.is_file():
        print(f"\n=== {name}: missing {path.name} ===")
        return 0
    segs = batch1_segs(json.loads(path.read_text(encoding="utf-8")))
    print(f"\n=== {name} ({len(segs)} segs covering kills 0-11) ===")
    matches = 0
    for i, want in enumerate(GOAL):
        got = segs[i] if i < len(segs) else None
        if got == want:
            matches += 1
            print(f"  seg{i + 1}: MATCH")
        else:
            print(f"  seg{i + 1}: DIFF")
            if got:
                for k in KEYS:
                    if got.get(k) != want.get(k):
                        print(f"    {k}: got {got.get(k)!r} want {want.get(k)!r}")
            else:
                print("    (missing segment)")
    print(f"  -> {matches}/{len(GOAL)} exact matches")
    return matches


def main() -> None:
    paths = [
        ("previous LLM verify", RUN / "edit_timeline_llm_verify.json"),
        ("new LLM rerun", RUN / "edit_timeline_llm_rerun.json"),
        ("manual edit_timeline", RUN / "edit_timeline.json"),
    ]
    scores = {name: diff_label(name, p) for name, p in paths}

    rerun = json.loads((RUN / "edit_timeline_llm_rerun.json").read_text(encoding="utf-8"))
    print("\n=== Rerun: first 5 segments (global order) ===")
    for i, s in enumerate(rerun["segments"][:5]):
        print(
            f"  {i + 1}: ticks {s['start_tick']}-{s['end_tick']} "
            f"kills {s['kill_indices']} pov ...{s['pov_steam_id'][-6:]}"
        )
    print("\n=== Golden batch 1 ===")
    for i, s in enumerate(GOAL):
        print(
            f"  {i + 1}: ticks {s['start_tick']}-{s['end_tick']} "
            f"kills {s['kill_indices']} pov ...{s['pov_steam_id'][-6:]}"
        )
    print("\nSummary:", scores)


if __name__ == "__main__":
    main()
