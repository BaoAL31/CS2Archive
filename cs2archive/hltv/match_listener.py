"""DEPRECATED: this module moved to ``cs2archive.listener.daemon``.

The listener polls HLTV event results *and* FACEIT notables, so ``hltv/`` was the
wrong bucket. Kept as a one-release-window compatibility shim so stale command
lines, docs and muscle memory keep working. The launcher wrappers
(``run_listener.ps1`` / ``stop_listener.ps1`` / ``install_listener_task.ps1``)
moved to ``cs2archive/listener/`` with it; the old names forward.
"""
from __future__ import annotations

import sys

sys.stderr.write(
    "DEPRECATED cs2archive/hltv/match_listener.py -> "
    "python -m cs2archive.listener.daemon\n"
)

_TARGET = "cs2archive.listener.daemon"

if __name__ == "__main__":
    import runpy

    runpy.run_module(_TARGET, run_name="__main__", alter_sys=True)
else:
    import importlib

    _module = importlib.import_module(_TARGET)
    for _name, _value in vars(_module).items():
        if not _name.startswith("_"):
            globals()[_name] = _value
