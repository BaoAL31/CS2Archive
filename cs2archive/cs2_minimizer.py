"""
Shared CS2 window minimizer — prevents CS2 from stealing focus during rendering.
"""

from __future__ import annotations

import ctypes
import threading
import time

HWND_BOTTOM = 1
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
    """Park a CS2 window restored but behind other windows (not minimized).

    A restored-but-unfocused game window renders at full speed, whereas a
    minimized one gets throttled by Windows. This restores it and drops it to the
    bottom of the z-order so it never steals your keyboard/mouse focus.
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
