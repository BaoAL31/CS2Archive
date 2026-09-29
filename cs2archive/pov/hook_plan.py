"""Kill-anchored edit plan shared by render_hook.py and assemble_hook.py.

A moment's detection window (``start_tick`` -> ``end_tick``) is contiguous, but
for a fast-paced hook we only want the seconds around each kill. Planning the
windows *before* rendering means every CSDM sequence is already a tight
sub-clip: no re-trim at assembly, no risk of a silent segment, and the cut
points are frame-exact.

Plan shape (seconds are ticks / tickrate):

    [setup] kill1 [post] [pre] kill2 [post] ... [payoff]

Gaps between kills shorter than the merge threshold collapse into one window, so
a burst of kills plays uninterrupted.

Usage:
    from cs2archive.pov.hook_plan import plan_hook
    plan = plan_hook(timeline, max_seconds=30.0)
"""

from __future__ import annotations

PRE_KILL = 1.0          # lead-in before each kill (fast pace)
PRE_FIRST_KILL = 1.0    # same context on the opening window
POST_KILL = 1.0         # hold after each kill
PAYOFF = 1.0            # hold after the final kill (the satisfying beat)
MAX_PAYOFF = 1.0        # hard ceiling on the final hold
MIN_WINDOW_TICKS = 64   # CSDM refuses sub-1s windows
NEXT_KILL_CAP_BUFFER = 0.4   # windows end this far before the POV's next kill
MIN_POST_KILL_HOLD = 0.75    # ... but the kill + immediate beat always play
CHAIN_MERGE_SECONDS = 5.0    # chained moments keep one continuous window across gaps below this


def _windows_for_moment(moment: dict, tickrate: int,
                        all_kill_ticks: list[int] | None = None) -> list[dict]:
    """Kill-anchored sub-clip windows for one moment, in ticks."""
    start_tick = int(moment["start_tick"])
    end_tick = int(moment["end_tick"])
    kills = sorted(int(k) for k in (moment.get("kill_ticks") or []))
    if not kills:
        return [{"start_tick": start_tick, "end_tick": end_tick}]

    pre = PRE_FIRST_KILL * tickrate
    post = POST_KILL * tickrate
    # Chained moments (several candidates merged in the builder) play as one
    # continuous clip: spans merge across gaps below the chain threshold
    # instead of splitting into jump-cut windows.
    merge_gap = (CHAIN_MERGE_SECONDS * tickrate
                 if moment.get("chained") else 0.0)
    spans: list[list[int]] = []
    for i, k in enumerate(kills):
        if i == 0:
            a = k - pre
        else:
            a = k - PRE_KILL * tickrate
        b = k + post
        if spans and a <= spans[-1][1] + merge_gap:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])

    # Payoff: hold past the last kill, up to the round win when we know it.
    last = spans[-1]
    payoff_end = kills[-1] + PAYOFF * tickrate
    win_tick = moment.get("round_win_tick")
    if win_tick:
        payoff_end = max(payoff_end, min(int(win_tick) + tickrate, kills[-1] + MAX_PAYOFF * tickrate))
    last[1] = max(last[1], min(payoff_end, end_tick))

    # The payoff must not swallow the POV's NEXT kill: a single-kill window
    # would read as an ordinary 2k, a multikill payoff as a bigger multikill
    # than the moment. End the tail just before the next kill lands (the kill
    # + immediate beat always play). Chained moments own their interior kills,
    # so only their tail is capped.
    if all_kill_ticks and kills:
        ordered = sorted(int(k) for k in all_kill_ticks)
        nxt = next((k for k in ordered if k > kills[-1]), None)
        if nxt is not None:
            cap = nxt - NEXT_KILL_CAP_BUFFER * tickrate
            floor = kills[-1] + MIN_POST_KILL_HOLD * tickrate
            last[1] = min(last[1], max(cap, floor))

    out = []
    for a, b in spans:
        a = max(a, start_tick)
        b = min(b, end_tick)
        if b - a < MIN_WINDOW_TICKS:
            b = a + MIN_WINDOW_TICKS
        out.append({"start_tick": int(a), "end_tick": int(b)})
    return out


def moment_seconds(moment: dict, tickrate: int) -> float:
    return sum((w["end_tick"] - w["start_tick"]) / tickrate
               for w in _windows_for_moment(moment, tickrate))


def planned_seconds(plan: list[dict], tickrate: int) -> float:
    """Assembled footage seconds for a planned hook (what the viewer sees)."""
    return sum((w["end_tick"] - w["start_tick"]) / tickrate
               for m in plan for w in m["windows"])


def plan_hook(moments: list[dict], tickrate: int = 64,
              max_seconds: float = 30.0,
              all_kill_ticks: list[int] | None = None) -> list[dict]:
    """Attach ``windows`` to each moment, dropping the weakest until it fits.

    ``moments`` is in edit order (climax last), so the weakest are at the front.
    Always keeps at least one moment. ``all_kill_ticks`` (every POV kill tick
    in the demo) caps each tail before the next kill lands; moments flagged
    ``chained`` keep one continuous window across gaps below 5s.
    """
    plan = []
    for m in moments:
        plan.append({**m, "windows": _windows_for_moment(m, tickrate, all_kill_ticks)})

    while len(plan) > 1:
        total = sum((w["end_tick"] - w["start_tick"]) / tickrate
                    for m in plan for w in m["windows"])
        if total <= max_seconds:
            break
        plan.pop(0)
    return plan
