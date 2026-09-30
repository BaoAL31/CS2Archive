"""Vanilla CS2 twin: kill unhooked cs2.exe, reap Steam respawns.

CR-10: these tests also cover the three-state process query. The old code returned an empty set for
a FAILED tasklist, so "could not measure" was indistinguishable from "absent" — which aborted
renders that were hooking fine and reported stalled hooks as "no HLAE hook". Every test here that
asserts on an UNKNOWN reading is guarding that distinction.
"""

from __future__ import annotations

import cs2archive.render.hook_aware as hook_aware
from cs2archive.render.hook_aware import ProcessQuery, UNKNOWN


def _q(*pids: int) -> ProcessQuery:
    return ProcessQuery(known=True, pids=frozenset(pids))


def test_kill_unhooked_skips_hooked_pid(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(11, 22))
    monkeypatch.setattr(hook_aware, "_query_afx", lambda **k: _q(11))
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    assert hook_aware.kill_unhooked_cs2() == [22]
    assert killed == [22]


def test_kill_unhooked_waits_until_afx_visible(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(11))
    monkeypatch.setattr(hook_aware, "_query_afx", lambda **k: _q())          # known: nothing hooked
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    assert hook_aware.kill_unhooked_cs2() == []
    assert killed == []


def test_kill_unhooked_leaves_the_hooked_cs2_alone(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(11))
    monkeypatch.setattr(hook_aware, "_query_afx", lambda **k: _q(11))
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    assert hook_aware.kill_unhooked_cs2() == []
    assert killed == []


# --- CR-10: UNKNOWN must never be treated as absent ---------------------------------------------

def test_query_process_unknown_on_tasklist_failure(monkeypatch):
    """A failed tasklist yields known=False — never a confident empty set."""
    def boom(*a, **k):
        raise OSError("tasklist exploded")

    monkeypatch.setattr(hook_aware.subprocess, "run", boom)
    q = hook_aware._query_process("cs2.exe", attempts=1, backoff=0)
    assert q is UNKNOWN
    assert q.known is False
    assert q.pids == frozenset()
    assert q.present is False
    assert q.absent is False          # the whole point: unknown is not absent


def test_query_process_unknown_on_nonzero_exit(monkeypatch):
    class R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(hook_aware.subprocess, "run", lambda *a, **k: R())
    assert hook_aware._query_process("cs2.exe", attempts=1, backoff=0).known is False


def test_query_process_parses_pids(monkeypatch):
    class R:
        returncode = 0
        stdout = '"cs2.exe","4321","Console","1","1,234 K"\n"cs2.exe","4322","Console","1","1,234 K"\n'

    monkeypatch.setattr(hook_aware.subprocess, "run", lambda *a, **k: R())
    q = hook_aware._query_process("cs2.exe", attempts=1, backoff=0)
    assert q.known and q.pids == frozenset({4321, 4322}) and q.present


def test_process_running_is_tri_state(monkeypatch):
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(7))
    assert hook_aware.process_running("cs2.exe") is True
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q())
    assert hook_aware.process_running("cs2.exe") is False
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: UNKNOWN)
    assert hook_aware.process_running("cs2.exe") is None


def test_kill_unhooked_refuses_when_afx_unknown(monkeypatch):
    """Unknown hook state must not license killing: it cannot tell hooked from vanilla."""
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(11, 22))
    monkeypatch.setattr(hook_aware, "_query_afx", lambda **k: UNKNOWN)
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    assert hook_aware.kill_unhooked_cs2() == []
    assert killed == []


def test_clear_all_cs2_clears_nothing_when_unknown(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: UNKNOWN)
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    assert hook_aware._clear_all_cs2(why="test") == 0
    assert killed == []


def test_clear_all_cs2_counts_only_confirmed_kills(monkeypatch):
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q(1, 2))
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: pid == 1)   # pid 2 refuses to die
    assert hook_aware._clear_all_cs2(why="test") == 2


def test_kill_stale_never_reports_clean_when_it_cannot_verify(monkeypatch, capsys):
    """A failed query must produce an explicit UNVERIFIED warning, not silence."""
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: UNKNOWN)
    monkeypatch.setattr(hook_aware, "_taskkill_tree", lambda name: True)
    monkeypatch.setattr(hook_aware, "reap_steam_respawned_cs2", lambda seconds=0: 0)
    hook_aware.kill_stale_processes()
    out = capsys.readouterr().out
    assert "could not VERIFY cleanup for" in out
    assert "state UNKNOWN" in out          # the narrow guard: refuses the blind blanket kill


def test_await_no_cs2_refuses_to_claim_death_when_unknown(monkeypatch, capsys):
    """Its own docstring warns that a lingering corpse poisons the next launch."""
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: UNKNOWN)
    assert hook_aware._await_no_cs2(timeout_s=0.01) is False
    assert "could not confirm cs2.exe death" in capsys.readouterr().out


def test_await_no_cs2_true_only_on_positive_absence(monkeypatch):
    monkeypatch.setattr(hook_aware, "_query_process", lambda name, **k: _q())
    assert hook_aware._await_no_cs2(timeout_s=1.0) is True


def test_reap_kills_respawn_then_stops(monkeypatch):
    killed: list[int] = []
    waves = [_q(99), _q()]

    def query(name: str, **k):
        assert name == "cs2.exe"
        return waves.pop(0) if waves else _q()

    monkeypatch.setattr(hook_aware, "_query_process", query)
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: (killed.append(pid), True)[1])
    n = hook_aware.reap_steam_respawned_cs2(seconds=2.5)
    assert n == 1
    assert killed == [99]


def test_reap_does_not_end_early_on_unknown(monkeypatch):
    """An unknown reading used to look like 'no cs2', ending the reap while Steam's viewer lived."""
    calls = {"n": 0}

    def query(name: str, **k):
        calls["n"] += 1
        return UNKNOWN if calls["n"] < 3 else _q()

    monkeypatch.setattr(hook_aware, "_query_process", query)
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: True)
    hook_aware.reap_steam_respawned_cs2(seconds=2.5)
    assert calls["n"] >= 3           # kept polling through the unknown readings


def test_missing_ffmpeg_after_hook_needs_new_pid():
    assert hook_aware.missing_ffmpeg_after_hook(None, 100.0, new_ffmpeg=False) is False
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 20.0, new_ffmpeg=False) is False
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 55.0, new_ffmpeg=False) is True
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 55.0, new_ffmpeg=True) is False


# --- CR-10: the poll-loop crux, now a pure function so the cap and the reset are testable ---------

def _streak_after(readings, *, start=0, cap=5, hooked_at=None, elapsed=0.0):
    """Drive classify_hook_poll over a sequence of readings; return (final streak, fail_reason)."""
    streak, reason = start, None
    for afx in readings:
        v = hook_aware.classify_hook_poll(
            afx, unknown_streak=streak, hooked_at=hooked_at, elapsed=elapsed,
            inject_grace=20.0, unknown_cap=cap)
        streak = v.unknown_streak
        if v.fail_reason is not None:
            reason = v.fail_reason
    return streak, reason


def test_unknown_streak_aborts_with_a_distinct_reason():
    streak, reason = _streak_after([UNKNOWN] * 5, cap=5)
    assert streak == 5
    assert reason is not None
    assert "could not determine process state" in reason
    assert "no HLAE hook" not in reason          # never claim the hook was absent


def test_unknown_streak_resets_on_a_known_absent_reading():
    """A known-absent reading is conclusive too; only PRESENT used to reset the streak."""
    readings = [UNKNOWN, UNKNOWN, _q(), UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN]
    streak, reason = _streak_after(readings, cap=5)
    assert reason is None, "four unknowns after a known-absent reading must not trip a cap of five"
    assert streak == 4


def test_unknown_streak_resets_on_a_known_present_reading():
    streak, reason = _streak_after([UNKNOWN] * 3 + [_q(11)] + [UNKNOWN] * 3, cap=5)
    assert reason is None
    assert streak == 3


def test_uncapped_unknown_streak_does_not_abort():
    _, reason = _streak_after([UNKNOWN] * 4, cap=5)
    assert reason is None


def test_known_absent_past_the_grace_reports_no_hook():
    v = hook_aware.classify_hook_poll(_q(), unknown_streak=0, hooked_at=None,
                                      elapsed=30.0, inject_grace=20.0)
    assert v.fail_reason == "no HLAE hook in 20s"


def test_known_absent_before_the_grace_keeps_waiting():
    v = hook_aware.classify_hook_poll(_q(), unknown_streak=0, hooked_at=None,
                                      elapsed=5.0, inject_grace=20.0)
    assert v.fail_reason is None


def test_first_sighting_is_reported_once():
    v1 = hook_aware.classify_hook_poll(_q(1), unknown_streak=0, hooked_at=None, elapsed=1.0)
    v2 = hook_aware.classify_hook_poll(_q(1), unknown_streak=0, hooked_at=1.0, elapsed=2.0)
    assert v1.first_sighting is True
    assert v2.first_sighting is False


def _startup_settings(tmp_path, *, plugin, params):
    import json

    from pathlib import Path

    path = Path(tmp_path) / "settings.json"
    path.write_text(
        json.dumps({"playback": {
            "cs2PluginVersion": plugin, "launchParameters": params}}),
        encoding="utf-8")
    return path


def test_startup_fix_check_flags_inactive_plugin(monkeypatch, tmp_path):
    import cs2archive.misc.install_csdm_startup_fix as fix

    monkeypatch.setattr(fix, "VERSION", "x_fix_1")
    reason = hook_aware.check_csdm_startup_fix(
        _startup_settings(tmp_path, plugin="latest",
                          params="-steam +csdm_initialize"),
        tmp_path / "game")
    assert reason is not None and "x_fix_1" in reason


def test_startup_fix_check_flags_missing_token(monkeypatch, tmp_path):
    import cs2archive.misc.install_csdm_startup_fix as fix

    monkeypatch.setattr(fix, "VERSION", "x_fix_1")
    monkeypatch.setattr(fix, "COMMAND", "+x_init")
    reason = hook_aware.check_csdm_startup_fix(
        _startup_settings(tmp_path, plugin="x_fix_1", params="-steam"),
        tmp_path / "game")
    assert reason is not None and "+x_init" in reason


def test_startup_fix_check_flags_unreadable_settings(tmp_path):
    reason = hook_aware.check_csdm_startup_fix(
        tmp_path / "nope.json", tmp_path / "game")
    assert reason is not None


def test_startup_fix_check_active_world(monkeypatch, tmp_path):
    import hashlib

    import cs2archive.misc.install_csdm_startup_fix as fix

    payload = b"patched-plugin-bytes"
    game_dll = b"game-dll-bytes"
    monkeypatch.setattr(fix, "VERSION", "x_fix_1")
    monkeypatch.setattr(fix, "COMMAND", "+x_init")
    monkeypatch.setattr(fix, "PATCHED_SHA256",
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(fix, "STOCK_SHA256",
                        hashlib.sha256(game_dll).hexdigest())
    plugin_dir = tmp_path / "csdm-plugins"
    plugin_dir.mkdir()
    (plugin_dir / "server_x_fix_1.dll").write_bytes(payload)
    monkeypatch.setattr(fix, "plugin_dir", lambda: plugin_dir)
    game = tmp_path / "game"
    mounted = game / "game" / "csgo" / "bin" / "win64"
    mounted.mkdir(parents=True)
    (mounted / "server.dll").write_bytes(game_dll)
    reason = hook_aware.check_csdm_startup_fix(
        _startup_settings(tmp_path, plugin="x_fix_1",
                          params="-steam +x_init"),
        game)
    assert reason is None


def test_startup_fix_check_flags_hand_patched_game_dll(monkeypatch, tmp_path):
    """The installer never replaces the game binary — a mounted server.dll
    that hashes to the patched build means a hand copy, which is
    unsupported. Plain CS2 updates change this hash routinely; that drift
    belongs to the version gate, not this check."""
    import hashlib

    import cs2archive.misc.install_csdm_startup_fix as fix

    payload = b"patched-plugin-bytes"
    monkeypatch.setattr(fix, "VERSION", "x_fix_1")
    monkeypatch.setattr(fix, "COMMAND", "+x_init")
    monkeypatch.setattr(fix, "PATCHED_SHA256",
                        hashlib.sha256(payload).hexdigest())
    plugin_dir = tmp_path / "csdm-plugins"
    plugin_dir.mkdir()
    (plugin_dir / "server_x_fix_1.dll").write_bytes(payload)
    monkeypatch.setattr(fix, "plugin_dir", lambda: plugin_dir)
    game = tmp_path / "game"
    mounted = game / "game" / "csgo" / "bin" / "win64"
    mounted.mkdir(parents=True)
    (mounted / "server.dll").write_bytes(payload)
    reason = hook_aware.check_csdm_startup_fix(
        _startup_settings(tmp_path, plugin="x_fix_1",
                          params="-steam +x_init"),
        game)
    assert reason is not None and "by hand" in reason
