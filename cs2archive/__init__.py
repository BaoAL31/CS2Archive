"""cs2archive package.

Windows console-window guard
----------------------------
When the pipeline runs without an inherited console (listener daemon, scheduled
task, agent-spawned shell), every console child (cmd.exe, tasklist, ffmpeg,
csdm.cmd, ...) makes Windows allocate a *fresh, visible* console window. That
shows up as cmd windows flashing all over the desktop during a render.

Default every ``subprocess`` spawn to ``CREATE_NO_WINDOW`` so children never get
their own window. Spawns that intentionally ask for a console keep working:
``CREATE_NEW_CONSOLE`` and ``DETACHED_PROCESS`` are left untouched.

Opt out for interactive debugging with ``CS2ARCHIVE_SHOW_CONSOLE=1``.
"""

from __future__ import annotations

import os
import subprocess

if os.name == "nt" and os.environ.get("CS2ARCHIVE_SHOW_CONSOLE", "") not in {"1", "true", "yes"}:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
    # Any of these means the caller already decided the console story.
    _EXPLICIT_CONSOLE = _NO_WINDOW | subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS
    _popen_init = subprocess.Popen.__init__

    def _windowless_popen_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        flags = kwargs.get("creationflags", 0)
        if not (flags & _EXPLICIT_CONSOLE):
            kwargs["creationflags"] = flags | _NO_WINDOW
        _popen_init(self, *args, **kwargs)

    if not getattr(subprocess.Popen, "_cs2archive_windowless", False):
        subprocess.Popen.__init__ = _windowless_popen_init  # type: ignore[method-assign]
        subprocess.Popen._cs2archive_windowless = True  # type: ignore[attr-defined]
