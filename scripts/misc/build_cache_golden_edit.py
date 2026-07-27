"""Manually reshape rendered 73-seg edit timeline into golden goal."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"

MIN_DUR = 640  # 10s
LEAD = 256
TAIL = 128
MULTI_TAIL = 64
HANDOFF = 160
POST_DEATH = 128  # 2s after POV death
WARMUP = 0


def main() -> None:
    at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
    src_path = RUN / "edit_timeline.rendered_73.json"
    if not src_path.is_file():
        raise SystemExit(f"Missing {src_path}; run _recover_rendered_73.py first")
    et = json.loads(src_path.read_text(encoding="utf-8"))
    kills = at["kills"]
    rs = {r["round"]: int(r["tick"]) for r in at["round_starts"]}
    fe = {r["round"]: int(r["tick"]) for r in at.get("round_freeze_ends", [])}
    rounds = sorted(rs.keys())
    round_end_exclusive: dict[int, int] = {}
    for i, rn in enumerate(rounds):
        if i + 1 < len(rounds):
            round_end_exclusive[rn] = rs[rounds[i + 1]] - 1
        else:
            round_end_exclusive[rn] = max(k["tick"] for k in kills if k["round"] == rn) + 640

    def death_of(sid: str, after_tick: int, rn: int) -> int | None:
        for k in kills:
            if k["round"] != rn:
                continue
            if k["tick"] <= after_tick:
                continue
            if k["victim_steam_id"] == sid:
                return int(k["tick"])
        return None

    def floor(rn: int) -> int:
        if rn == WARMUP:
            return rs[rn]
        return fe.get(rn, rs[rn] + 1536)

    def att(kis: list[int]) -> str:
        return kills[kis[0]]["attacker_steam_id"]

    segs = deepcopy(et["segments"])

    def remove_kis(ki: int) -> None:
        segs[:] = [
            s
            for s in (
                {**s, "kill_indices": [x for x in s["kill_indices"] if x != ki]}
                for s in segs
            )
            if s["kill_indices"]
        ]

    def set_or_add(kis: list[int], pov: str | None = None) -> None:
        kis_set = set(kis)
        leftover = []
        for s in segs:
            overlap = kis_set & set(s["kill_indices"])
            if not overlap:
                leftover.append(s)
                continue
            remain = [x for x in s["kill_indices"] if x not in kis_set]
            if remain:
                ns = deepcopy(s)
                ns["kill_indices"] = remain
                leftover.append(ns)
        segs[:] = leftover
        segs.append({
            "start_tick": kills[min(kis, key=lambda i: kills[i]["tick"])]["tick"],
            "end_tick": kills[max(kis, key=lambda i: kills[i]["tick"])]["tick"] + TAIL,
            "pov_steam_id": pov or att(kis),
            "segment_type": "multi_kill" if len(kis) >= 2 else "default",
            "kill_indices": sorted(kis),
            "rationale": "manual golden",
        })

    # --- Surgical edits (user review of rendered segs) ---

    # r0 warmup: majority electroNic (include all pro knife kills present after fix)
    set_or_add([2, 3, 4, 6])

    # r4: drop early s1mple 1k before Senzu then mzinho multi (keep last 1k before multi)
    remove_kis(24)

    # r11: equal streak interrupt — TeSeS 1k then mzinho 2k; drop TeSeS sandwich [69]
    # and electroNic same-tick solo [71]
    set_or_add([67])
    set_or_add([68, 70])
    remove_kis(69)
    remove_kis(71)

    # r13: drop Senzu [80] interrupting electroNic 3k
    set_or_add([78, 81, 83])
    remove_kis(80)

    # r16: electroNic solo ~6s — prefer expand s1mple, drop electroNic [99]
    # TeSeS 2k through s1mple sandwich [101]
    remove_kis(99)
    set_or_add([98])
    set_or_add([100, 102])
    remove_kis(101)

    # r18: merge Senzu 2k; drop TeSeS interrupt [110]
    set_or_add([109, 112])
    remove_kis(110)

    # r21: TeSeS 2k uninterrupted; drop electroNic [130]
    set_or_add([128, 131])
    remove_kis(130)

    # r22: one electroNic 3k segment (clutch) — merge fragments
    set_or_add([133, 136, 137])

    # r23: keep mzinho / TeSeS / last electroNic (last expands to round end)
    set_or_add([140])
    set_or_add([141])
    set_or_add([143])

    # r24: drop mzinho 1k [146]; Senzu from round start; keep holzt 2k
    remove_kis(146)
    set_or_add([148])
    set_or_add([149, 150])

    # r26: merge electroNic through s1mple interrupt
    set_or_add([158, 162])
    remove_kis(160)

    # r29: TeSeS then mzinho only (drop Senzu + s1mple; 10s min)
    remove_kis(174)
    remove_kis(175)
    set_or_add([177])
    set_or_add([179, 180])

    # r30: mzinho then TeSeS merged to end of match
    set_or_add([182])
    set_or_add([183, 186])

    segs = [s for s in segs if s.get("kill_indices")]
    segs.sort(key=lambda s: min(kills[i]["tick"] for i in s["kill_indices"]))

    # First claim wins for overlapping kis
    seen: set[int] = set()
    cleaned = []
    for s in segs:
        kis = [ki for ki in s["kill_indices"] if ki not in seen]
        if not kis:
            continue
        seen.update(kis)
        s["kill_indices"] = sorted(kis)
        s["pov_steam_id"] = att(kis)
        s["segment_type"] = "multi_kill" if len(kis) >= 2 else "default"
        cleaned.append(s)
    segs = cleaned

    last_idx_by_round: dict[int, int] = {}
    for i, s in enumerate(segs):
        rn = kills[s["kill_indices"][0]]["round"]
        last_idx_by_round[rn] = i

    last_kill_by_round: dict[int, int] = {}
    seen_round: set[int] = set()

    for i, s in enumerate(segs):
        kis = s["kill_indices"]
        rn = kills[kis[0]]["round"]
        kt = [kills[ki]["tick"] for ki in kis]
        first_k, last_k = min(kt), max(kt)
        fl = floor(rn)
        first_in_round = rn not in seen_round
        seen_round.add(rn)
        prior = last_kill_by_round.get(rn)

        if first_in_round:
            start = fl
        else:
            lead = first_k - LEAD
            if prior is not None:
                lead = min(lead, prior + HANDOFF)
            start = max(fl, lead, 0)

        end = last_k + TAIL + MULTI_TAIL * max(0, len(kis) - 1)
        pov = s["pov_steam_id"]
        dth = death_of(pov, last_k, rn)
        rnd_end = round_end_exclusive[rn]
        is_last = last_idx_by_round.get(rn) == i

        if is_last:
            if dth is not None and dth <= rnd_end:
                end = max(end, dth + POST_DEATH)
                end = min(end, rnd_end)
            else:
                end = rnd_end
        else:
            if dth is not None:
                end = max(end, min(dth + POST_DEATH, rnd_end))
            if i + 1 < len(segs):
                next_kis = segs[i + 1]["kill_indices"]
                next_rn = kills[next_kis[0]]["round"]
                if next_rn == rn:
                    next_first = min(kills[ki]["tick"] for ki in next_kis)
                    end = min(end, max(last_k + TAIL, next_first + HANDOFF))
            end = min(end, rnd_end)

        if end - start < MIN_DUR:
            end = min(start + MIN_DUR, rnd_end)
            if end - start < MIN_DUR:
                start = max(fl, end - MIN_DUR)

        end = max(end, last_k + 1)
        end = min(end, rnd_end)

        s["start_tick"] = int(start)
        s["end_tick"] = int(end)
        last_kill_by_round[rn] = max(last_kill_by_round.get(rn, 0), last_k)

    for s in segs:
        s["rationale"] = (
            f"Golden: {kills[s['kill_indices'][0]]['attacker']} "
            f"kills {s['kill_indices']} r{kills[s['kill_indices'][0]]['round']}"
        )

    out_et = {
        "demo_path": et.get("demo_path") or at.get("demo_path"),
        "map": et.get("map") or at.get("map"),
        "segments": segs,
    }
    (RUN / "edit_timeline.json").write_text(json.dumps(out_et, indent=2), encoding="utf-8")
    (RUN / "edit_timeline.golden.json").write_text(json.dumps(out_et, indent=2), encoding="utf-8")

    FIXTURES.mkdir(parents=True, exist_ok=True)
    goal = {
        "description": (
            "Golden edit timeline after manual review (Cache FACEIT). "
            "Equal/higher streak interrupt, 10s min, death+2s, last-seg round-end expand."
        ),
        "segments": [
            {
                "start_tick": s["start_tick"],
                "end_tick": s["end_tick"],
                "pov_steam_id": s["pov_steam_id"],
                "segment_type": s["segment_type"],
                "kill_indices": s["kill_indices"],
            }
            for s in segs
        ],
    }
    (FIXTURES / "cache_full_goal_segments.json").write_text(
        json.dumps(goal, indent=2), encoding="utf-8"
    )

    print(f"Wrote {len(segs)} golden segments")
    for i, s in enumerate(segs, 1):
        rn = kills[s["kill_indices"][0]]["round"]
        print(
            f"  seg-{i:03d} r{rn} t{s['start_tick']}-{s['end_tick']} "
            f"({(s['end_tick'] - s['start_tick']) / 64:.1f}s) "
            f"kis={s['kill_indices']} pov=...{s['pov_steam_id'][-6:]}"
        )


if __name__ == "__main__":
    main()
