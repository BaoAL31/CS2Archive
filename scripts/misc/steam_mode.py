"""Toggle Steam online/offline mode via script.

HLAE's CS2 hook fails to latch when Steam is online (CS2 opens the vanilla demo
viewer instead of recording). Running Steam in offline mode fixes it. Steam has
no live online/offline toggle, so this restarts Steam:

    offline:  steam.exe -shutdown, then steam.exe -offline
    online:   steam.exe -shutdown, then steam.exe

Usage:
    python scripts/misc/steam_mode.py --offline
    python scripts/misc/steam_mode.py --online
    python scripts/misc/steam_mode.py --status
"""
from __future__ import annotations

import argparse
import subprocess
import time

STEAM_EXE = r"D:\Steam\steam.exe"


def _steam_running() -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq steam.exe"],
        capture_output=True, text=True,
    ).stdout
    return "steam.exe" in out


def _close_steam(wait: bool = True) -> None:
    # tasklist can miss a just-launched process; keep probing until Steam either
    # shows up or we've waited long enough to conclude it's not starting.
    for _ in range(6):
        if _steam_running():
            break
        time.sleep(0.5)
    if not _steam_running():
        return  # not running / not starting — nothing to close
    subprocess.run([STEAM_EXE, "-shutdown"], timeout=20)
    for _ in range(60):  # wait up to ~30s for a graceful close
        if not _steam_running():
            return
        time.sleep(0.5)
    # Force-close only as a last resort — aggressive kills corrupt Steam's
    # login session and make it exit right after launch (connect -> LogOff).
    subprocess.run(["taskkill", "/F", "/IM", "steam.exe"], capture_output=True)
    time.sleep(3)


def _launch(args: list[str]) -> None:
    # Launch detached so Steam survives after this Python process exits.
    # Without DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP, Steam is a child of
    # the script's process and gets killed when the script exits.
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        [STEAM_EXE] + args,
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        close_fds=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--offline", action="store_true", help="set Steam to offline mode")
    g.add_argument("--online", action="store_true", help="set Steam to online mode")
    g.add_argument("--status", action="store_true", help="print whether Steam is running")
    args = ap.parse_args()

    if args.status:
        print("steam running:", _steam_running())
        return

    target = "offline" if args.offline else "online"
    # Always attempt a graceful close first (this probes for up to ~3s for a
    # just-launched Steam to appear, so we don't spawn a second instance during
    # a restart). If Steam isn't running it just proceeds to launch.
    _close_steam()
    print(f"Launching Steam {target}...")
    _launch(["-offline"] if args.offline else [])
    print(f"Steam set to {target} mode (restarting)")


if __name__ == "__main__":
    main()
