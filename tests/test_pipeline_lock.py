"""Global pipeline lock: two concurrent runs must never share CS2/HLAE.

Overlapping pipelines racing one render dir produced a 57 GB duplicated
concat (2026-10-07). The lock is an OS byte-range hold: the second acquirer
in a DIFFERENT process fails, and a crash/kill releases automatically.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _holder_script(lock_path: str) -> str:
    return (
        "import time, sys; sys.path.insert(0, '.');"
        "from cs2archive.pov.pipeline import acquire_global_lock,"
        " GLOBAL_LOCK_PATH;"
        f"import cs2archive.pov.pipeline as p; p.GLOBAL_LOCK_PATH = __import__('pathlib').Path({lock_path!r});"
        "fd = acquire_global_lock();"
        "print('HOLDING', flush=True); time.sleep(30)"
    )


def test_second_process_is_refused_while_first_holds(tmp_path):
    import cs2archive.pov.pipeline as pipe

    orig = pipe.GLOBAL_LOCK_PATH
    pipe.GLOBAL_LOCK_PATH = tmp_path / "pipeline.lock"
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", _holder_script(str(pipe.GLOBAL_LOCK_PATH))],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            assert holder.stdout is not None
            assert "HOLDING" in holder.stdout.readline()
            with __import__("pytest").raises(SystemExit):
                pipe.acquire_global_lock()
        finally:
            holder.kill()
            holder.wait(timeout=10)
    finally:
        pipe.GLOBAL_LOCK_PATH = orig


def test_lock_reacquirable_after_release(tmp_path, monkeypatch):
    import cs2archive.pov.pipeline as pipe

    monkeypatch.setattr(pipe, "GLOBAL_LOCK_PATH", tmp_path / "pipeline.lock")
    fd = pipe.acquire_global_lock()
    pipe.release_global_lock(fd)
    fd2 = pipe.acquire_global_lock()
    pipe.release_global_lock(fd2)
    assert Path(tmp_path / "pipeline.lock").is_file()
