"""
Shared CS2 window minimizer — prevents CS2 from stealing focus during rendering.

Also the render stall guard. Parking CS2 behind the operator's windows is not
free: Windows is under no obligation to keep presenting an occluded/unfocused
window, and on the reporter's machine a parked CS2 stops rendering entirely
("when it's unfocus it just stops entirely") — CSDM keeps "recording" a frozen
game, so the round comes out as a still image. The guard below watches the
render directory and brings CS2 back to the front when the writes stop.
"""

from __future__ import annotations

import ctypes
import enum
import threading
import time
from pathlib import Path

HWND_BOTTOM = 1
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

SW_MINIMIZE = 6
SW_RESTORE = 9

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_pid_for_hwnd(hwnd: int) -> int:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _find_cs2_windows() -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []

    def enum_callback(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            pid = _get_pid_for_hwnd(hwnd)
            if pid:
                try:
                    handle = kernel32.OpenProcess(0x0400, False, pid)
                    if handle:
                        buf = ctypes.create_unicode_buffer(260)
                        kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(ctypes.c_ulong(260)))
                        kernel32.CloseHandle(handle)
                        if buf.value.lower().endswith("cs2.exe"):
                            title = _get_window_title(hwnd)
                            if title:
                                results.append((hwnd, title))
                except Exception:
                    pass
        return True

    user32.EnumWindows(WNDENUMPROC(enum_callback), None)
    return results


def ensure_cs2_closed() -> None:
    """Minimize all CS2 windows (used before render to free GPU)."""
    for hwnd, _ in _find_cs2_windows():
        if user32.IsWindow(hwnd) and not user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_MINIMIZE)


def focus_cs2(restore: bool = True) -> bool:
    """Bring a CS2 window to the foreground (restoring it first if minimized).

    Returns True if a CS2 window was found and focused. Uses a simulated Alt
    keypress to bypass Windows' foreground lock so a background (Python) process
    can reliably steal focus — this is what makes rendering run at full speed
    instead of being throttled while CS2 sits minimized/unfocused.
    """
    windows = _find_cs2_windows()
    if not windows:
        return False
    hwnd, _ = windows[0]
    if user32.IsWindow(hwnd):
        if restore and user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # Unlock the foreground: Windows only lets the *active* process call
        # SetForegroundWindow. A tiny synthetic ALT keypress makes this thread
        # count as foreground-active, letting us focus CS2 without a click.
        user32.keybd_event(0x12, 0, 0, 0)          # VK_MENU down
        user32.keybd_event(0x12, 0, 0x0002, 0)     # VK_MENU up (KEYEVENTF_KEYUP)
        user32.SetForegroundWindow(hwnd)
        return True
    return False


def park_cs2(hwnd: int) -> None:
    """Keep CS2 restored without changing capture geometry or stealing focus.

    Background engine sleep is disabled in the render cfg, not by moving the
    window outside the desktop (which can itself cause presentation occlusion).
    """
    if not user32.IsWindow(hwnd):
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


class CS2Park(threading.Thread):
    """Focus CS2 once to unlock full render speed, then park it behind your windows.

    Windows throttles a window that has never been activated, so CS2 renders slowly
    if it is left minimized/unfocused from launch. This thread briefly brings CS2 to
    the front (unlocking full speed), then drops it restored *behind* your other
    windows (not minimized) so it renders fast without obstructing you.
    """

    def __init__(self, pulse: float = 0.6, verbose: bool = False):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._pulse = pulse
        self._verbose = verbose
        self._done: set[int] = set()

    def run(self):
        while not self._stop.is_set():
            for hwnd, title in _find_cs2_windows():
                if hwnd not in self._done:
                    if focus_cs2():
                        time.sleep(self._pulse)
                        park_cs2(hwnd)
                        self._done.add(hwnd)
                        if self._verbose:
                            print(f"\n  [park] CS2 focused, parked behind: {title}")
            self._stop.wait(0.2)

    def stop(self):
        self._stop.set()


class CS2FocusPulse(threading.Thread):
    """Briefly focus CS2 when it launches, then minimize it.

    CS2 renders slowly until it has been the foreground window at least once
    (Windows throttles a window that has never been activated). This thread
    pulses CS2 to the front for a short beat — unlocking full render speed —
    and then minimizes it so it does not obstruct your work.
    """

    def __init__(self, pulse: float = 0.6, verbose: bool = False):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._pulse = pulse
        self._verbose = verbose
        self._done: set[int] = set()

    def run(self):
        while not self._stop.is_set():
            for hwnd, title in _find_cs2_windows():
                if hwnd not in self._done:
                    if focus_cs2():
                        time.sleep(self._pulse)
                        if user32.IsWindow(hwnd) and not user32.IsIconic(hwnd):
                            user32.ShowWindow(hwnd, SW_MINIMIZE)
                        self._done.add(hwnd)
                        if self._verbose:
                            print(f"\n  [focus-pulse] pulsed to front: {title}")
            self._stop.wait(0.2)

    def stop(self):
        self._stop.set()


class CS2Minimizer(threading.Thread):
    def __init__(self, verbose: bool = False):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._minimized: set[int] = set()
        self._verbose = verbose

    def run(self):
        while not self._stop.is_set():
            for hwnd, title in _find_cs2_windows():
                if hwnd not in self._minimized:
                    if not user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, SW_MINIMIZE)
                        self._minimized.add(hwnd)
                        if self._verbose:
                            print(f"\n  [minimizer] Minimized: {title}")
            self._stop.wait(0.1)

    def stop(self):
        self._stop.set()
        for hwnd in list(self._minimized):
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
        self._minimized.clear()


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class StallAction(enum.Enum):
    """What the guard does when the render stops writing."""

    REFOCUS = "refocus"          # bring CS2 back to the front and keep going
    STOP_PARKING = "stop-parking"  # the park itself is the problem: leave it focused


def stall_action(consecutive: int) -> StallAction:
    """Escalation for the *consecutive*-th stall of one render (pure).

    One or two stalls are the ordinary throttle; by the third, parking CS2 has
    demonstrably stopped it twice in a row and the only reliable option left is
    to leave the window in the foreground for the rest of the render.
    """
    return StallAction.REFOCUS if consecutive < 3 else StallAction.STOP_PARKING


def render_is_stalled(prev_bytes: int | None, cur_bytes: int | None) -> bool:
    """True when the game stopped writing frames between two samples (pure).

    The instrument is cs2.exe's own ``WriteTransferCount``: mirv_streams writes
    the captured stream from inside the game process, so frames in flight mean
    a steadily climbing counter, and a frozen game means a flat one.

    Directory mtimes were tried first and are NOT usable: a healthy render
    leaves gaps of up to 375s between writes (measured 2026-10-25), which is
    longer than any stall threshold worth having. Either side being None means
    "no game / not armed yet" — startup is not a stall.
    """
    if prev_bytes is None or cur_bytes is None:
        return False
    return cur_bytes <= prev_bytes


def cs2_write_total() -> int | None:
    """Sum of cs2.exe's WriteTransferCount, or None when no CS2 is running."""
    total = 0
    found = False
    for pid in _cs2_process_ids():
        written = _process_write_bytes(pid)
        if written is not None:
            total += written
            found = True
    return total if found else None


class RenderStallGuard(threading.Thread):
    """Refocus CS2 when a parked window stops producing frames.

    Samples cs2.exe's write counter every *poll* seconds while a render is in
    flight. Once the counter has been seen growing (armed) it must keep
    growing: ``stall_samples`` consecutive flat samples mean the game has
    stopped rendering — the reported symptom, where CS2 parked behind other
    windows freezes and CSDM records a still image.

    On the first two stalls it brings CS2 back to the foreground; on the third
    it stops the parker entirely, so a render degrades to \"CS2 in the
    foreground\" rather than to a frozen video.
    """

    def __init__(self, render_dir: Path | None = None, *, poll: float = 5.0,
                 stall_samples: int = 6, park: "CS2Park | None" = None,
                 verbose: bool = True):
        super().__init__(daemon=True)
        self._dir = Path(render_dir) if render_dir is not None else None
        self._poll = poll
        self._stall_samples = max(2, int(stall_samples))
        self._park = park
        self._verbose = verbose
        self._stop = threading.Event()
        self._stalls = 0
        self._flat = 0
        self._prev: int | None = None
        self._armed = False

    def run(self) -> None:
        while not self._stop.is_set():
            cur = cs2_write_total()
            if cur is None:
                # No game (between batches / before launch): nothing to judge.
                self._prev, self._armed, self._flat = None, False, 0
            elif not self._armed:
                if self._prev is not None and cur > self._prev:
                    self._armed = True
                self._flat = 0
            elif render_is_stalled(self._prev, cur):
                self._flat += 1
                if self._flat >= self._stall_samples:
                    self._trip()
            else:
                self._flat = 0
            self._prev = cur
            self._stop.wait(self._poll)

    def _trip(self) -> None:
        action = stall_action(self._stalls)
        self._stalls += 1
        self._flat = 0
        if action is StallAction.STOP_PARKING and self._park is not None:
            self._park.stop()
        focused = focus_cs2()
        if self._verbose:
            seconds = self._stall_samples * self._poll
            print(f"\n  [stall] cs2 wrote nothing for {seconds:.0f}s while parked — "
                  f"{action.value}{' (CS2 focused)' if focused else ' (no CS2 window found)'}",
                  flush=True)

    def stop(self) -> None:
        self._stop.set()


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _cs2_process_ids() -> list[int]:
    """PIDs of every running cs2.exe (by image name, not window title)."""
    pids: list[int] = []

    def callback(hwnd: int, _: int) -> bool:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            handle = kernel32.OpenProcess(0x0400, False, pid.value)
            if handle:
                buf = ctypes.create_unicode_buffer(260)
                kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(ctypes.c_ulong(260)))
                kernel32.CloseHandle(handle)
                if buf.value.lower().endswith("cs2.exe"):
                    pids.append(pid.value)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), None)
    return sorted(set(pids))


def _process_write_bytes(pid: int) -> int | None:
    """WriteTransferCount for *pid* (bytes written), or None when unreadable."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = IO_COUNTERS()
        if not kernel32.GetProcessIoCounters(handle, ctypes.byref(counters)):
            return None
        return int(counters.WriteTransferCount)
    finally:
        kernel32.CloseHandle(handle)
