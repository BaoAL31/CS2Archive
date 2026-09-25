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
    from pov.hook_plan import plan_hook
    plan = plan_hook(timeline, max_seconds=30.0)
"""

from __future__ import annotations

PRE_KILL = 1.2          # lead-in before each kill (fast pace)
PRE_FIRST_KILL = 2.0    # a little more context on the opening window
POST_KILL = 1.5         # hold after each kill
PAYOFF = 2.5            # hold after the final kill (the satisfying beat)
MAX_PAYOFF = 4.0        # hard ceiling on the final hold
MIN_WINDOW_TICKS = 64   # CSDM refuses sub-1s windows


def _windows_for_moment(moment: dict, tickrate: int) -> list[dict]:
    """Kill-anchored sub-clip windows for one moment, in ticks."""
    start_tick = int(moment["start_tick"])
    end_tick = int(moment["end_tick"])
    kills = sorted(int(k) for k in (moment.get("kill_ticks") or []))
    if not kills:
        return [{"start_tick": start_tick, "end_tick": end_tick}]

    pre = PRE_FIRST_KILL * tickrate
    post = POST_KILL * tickrate
    spans: list[list[int]] = []
    for i, k in enumerate(kills):
        if i == 0:
            a = k - pre
        else:
            a = k - PRE_KILL * tickrate
        b = k + post
        if spans and a <= spans[-1][1]:
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


def plan_hook(moments: list[dict], tickrate: int = 64,
              max_seconds: float = 30.0) -> list[dict]:
    """Attach ``windows`` to each moment, dropping the weakest until it fits.

    ``moments`` is in edit order (climax last), so the weakest are at the front.
    Always keeps at least one moment.
    """
    plan = []
    for m in moments:
        plan.append({**m, "windows": _windows_for_moment(m, tickrate)})

    while len(plan) > 1:
        total = sum((w["end_tick"] - w["start_tick"]) / tickrate
                    for m in plan for w in m["windows"])
        if total <= max_seconds:
            break
        plan.pop(0)
    return plan
