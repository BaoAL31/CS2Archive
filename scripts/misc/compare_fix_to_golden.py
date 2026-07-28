"""Compare _fix_edit_timeline output to golden kill_indices (Cache match)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _extract_players_from_action_timeline,
    _fix_edit_timeline,
)

RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"


def main() -> None:
    at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
    golden = json.loads((FIXTURES / "cache_full_goal_segments.json").read_text(encoding="utf-8"))
    rendered = json.loads((RUN / "edit_timeline.rendered_73.json").read_text(encoding="utf-8"))
    players = _extract_players_from_action_timeline(at)

    # Feed fragmented LLM-like input (rendered segs) through fix pass
    llm = {"segments": rendered["segments"]}
    fixed = _fix_edit_timeline(llm, at, players)["segments"]
    want = golden["segments"]

    print(f"fixed={len(fixed)} golden={len(want)}")

    def key(s):
        return (tuple(s["kill_indices"]), s["pov_steam_id"])

    got_keys = [key(s) for s in fixed]
    want_keys = [key(s) for s in want]

    # Align by kill set
    want_by = {frozenset(s["kill_indices"]): s for s in want}
    got_by = {frozenset(s["kill_indices"]): s for s in fixed}

    missing = sorted(want_by.keys() - got_by.keys(), key=lambda fs: min(fs))
    extra = sorted(got_by.keys() - want_by.keys(), key=lambda fs: min(fs))
    print(f"missing kill-sets ({len(missing)}):")
    for fs in missing:
        s = want_by[fs]
        print(f"  WANT kis={s['kill_indices']} pov=...{s['pov_steam_id'][-6:]}")
    print(f"extra kill-sets ({len(extra)}):")
    for fs in extra:
        s = got_by[fs]
        rn = at["kills"][s["kill_indices"][0]]["round"]
        print(f"  GOT  r{rn} kis={s['kill_indices']} pov=...{s['pov_steam_id'][-6:]}")

    shared = want_by.keys() & got_by.keys()
    tick_mismatches = 0
    for fs in sorted(shared, key=lambda x: min(x)):
        g, w = got_by[fs], want_by[fs]
        if g["start_tick"] != w["start_tick"] or g["end_tick"] != w["end_tick"]:
            tick_mismatches += 1
            if tick_mismatches <= 15:
                print(
                    f"  ticks kis={w['kill_indices']}: "
                    f"got {g['start_tick']}-{g['end_tick']} "
                    f"want {w['start_tick']}-{w['end_tick']}"
                )
    print(f"shared={len(shared)} tick_mismatches={tick_mismatches}")


if __name__ == "__main__":
    main()
