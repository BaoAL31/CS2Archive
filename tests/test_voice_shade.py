"""Voice-shade halftime mapping — last first-half round is not second half."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "overlay"))

from voice_shade import last_first_half_round


# Real sidecar ticks from s1mple vs top1sacer mirage (2026-09-10).
_PRT = {
    11: [71896, 74013],
    12: [76277, 80070],
    13: [84733, 86703],
}


def test_half_announce_in_round_12_freeze_stays_first_half():
    # round_announce_last_round_half @ 75125 sits between r11 end and r12 start.
    assert last_first_half_round(75125, _PRT) == 12


def test_half_announce_inside_round_12_still_first_half():
    assert last_first_half_round(77000, _PRT) == 12


def test_half_announce_after_round_12_clip_is_already_second_half():
    # Event does not fire here; if a tick lands in the r12–r13 gap it is
    # after the last first-half *clip*, so treat r13 as the boundary round.
    assert last_first_half_round(82000, _PRT) == 13


def test_second_half_is_the_round_after_last_first_half():
    last = last_first_half_round(75125, _PRT)
    rounds = sorted(_PRT)
    assert last == 12
    assert rounds[rounds.index(last) + 1] == 13
