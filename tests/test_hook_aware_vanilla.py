"""Vanilla CS2 twin: kill unhooked cs2.exe, reap Steam respawns."""

from __future__ import annotations

import hook_aware


def test_kill_unhooked_skips_hooked_pid(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_image_pids", lambda name: {11, 22} if name == "cs2.exe" else set())
    monkeypatch.setattr(hook_aware, "_afx_pids", lambda: {11})
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: killed.append(pid))
    assert hook_aware.kill_unhooked_cs2() == [22]
    assert killed == [22]


def test_kill_unhooked_waits_until_afx_visible(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_image_pids", lambda name: {11} if name == "cs2.exe" else set())
    monkeypatch.setattr(hook_aware, "_afx_pids", lambda: set())
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: killed.append(pid))
    assert hook_aware.kill_unhooked_cs2() == []
    assert killed == []
    killed: list[int] = []
    monkeypatch.setattr(hook_aware, "_image_pids", lambda name: {11} if name == "cs2.exe" else set())
    monkeypatch.setattr(hook_aware, "_afx_pids", lambda: {11})
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: killed.append(pid))
    assert hook_aware.kill_unhooked_cs2() == []
    assert killed == []


def test_reap_kills_respawn_then_stops(monkeypatch):
    killed: list[int] = []
    waves = [{99}, set()]

    def pids(name: str) -> set[int]:
        assert name == "cs2.exe"
        return waves.pop(0) if waves else set()

    monkeypatch.setattr(hook_aware, "_image_pids", pids)
    monkeypatch.setattr(hook_aware, "_taskkill_pid", lambda pid: killed.append(pid))
    n = hook_aware.reap_steam_respawned_cs2(seconds=2.5)
    assert n == 1
    assert killed == [99]


def test_missing_ffmpeg_after_hook_needs_new_pid():
    assert hook_aware.missing_ffmpeg_after_hook(None, 100.0, new_ffmpeg=False) is False
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 20.0, new_ffmpeg=False) is False
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 55.0, new_ffmpeg=False) is True
    assert hook_aware.missing_ffmpeg_after_hook(10.0, 55.0, new_ffmpeg=True) is False
