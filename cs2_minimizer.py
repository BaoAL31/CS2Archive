"""
Shared CS2 window minimizer — prevents CS2 from stealing focus during rendering.
"""

from __future__ import annotations

import ctypes
import threading

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
