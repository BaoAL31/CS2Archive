"""Pre-render version gate: demo ↔ CS2 patch + HLAE/CSDM floors.

Local filesystem only — no network. Typical cost is tens of milliseconds
(steam.inf + two PE version resources + demoparser header).
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

# Bump these when a CS2 update requires newer tooling (after you install it).
MIN_HLAE = (2, 192, 0)
MIN_CSDM = (3, 20, 0)

HLAE_EXE = Path(r"C:\Program Files (x86)\HLAE\HLAE.exe")
CSDM_EXE = Path(r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\cs-demo-manager.exe")
CS2_STEAM_INF = Path(
    r"D:\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\steam.inf"
)


class RenderVersionError(Exception):
    """Hard fail for pipeline / render_pov."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class VersionCheckResult:
    ok: bool
    versions: dict[str, str] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (code, message)

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        code, message = self.errors[0]
        if len(self.errors) > 1:
            message = "; ".join(f"[{c}] {m}" for c, m in self.errors)
            code = "RENDER_VERSION_CHECK"
        raise RenderVersionError(code, message)


def normalize_patch_version(raw: str) -> str:
    """Normalize CS2 patch strings to dotted form (e.g. 14172 → 1.41.7.2)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty patch version")
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", s):
        return s
    # demoparser2 header uses undotted digits (14172 → 1.41.7.2)
    if re.fullmatch(r"\d{5}", s):
        return f"{s[0]}.{s[1:3]}.{s[3]}.{s[4]}"
    raise ValueError(f"unrecognized patch version: {raw!r}")


def parse_steam_inf_patch(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("PatchVersion="):
            return normalize_patch_version(line.split("=", 1)[1].strip())
    raise ValueError("PatchVersion= not found in steam.inf")


def read_cs2_patch(steam_inf: Path = CS2_STEAM_INF) -> str:
    if not steam_inf.is_file():
        raise FileNotFoundError(f"CS2 steam.inf not found: {steam_inf}")
    return parse_steam_inf_patch(steam_inf.read_text(encoding="utf-8", errors="replace"))


def read_demo_patch(demo_path: Path) -> str:
    from demoparser2 import DemoParser

    header = DemoParser(str(demo_path)).parse_header()
    return normalize_patch_version(str(header["patch_version"]))


def read_pe_version(exe: Path) -> tuple[int, ...]:
    """Return (major, minor, build, revision) from a Windows PE FileVersion."""
    path = str(exe)
    size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
    if not size:
        raise OSError(f"GetFileVersionInfoSizeW failed for {exe}")
    buf = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
        raise OSError(f"GetFileVersionInfoW failed for {exe}")

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
        raise OSError(f"VerQueryValueW failed for {exe}")
    info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return (
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    )


def format_version(parts: tuple[int, ...]) -> str:
    # Drop trailing .0 revision when unused (3.20.0.0 → 3.20.0)
    trimmed = list(parts)
    while len(trimmed) > 3 and trimmed[-1] == 0:
        trimmed.pop()
    return ".".join(str(p) for p in trimmed)


def version_at_least(have: tuple[int, ...], need: tuple[int, ...]) -> bool:
    return tuple(have[: len(need)]) >= need


def check_render_versions(
    demo_path: Path | str | None = None,
    *,
    steam_inf: Path = CS2_STEAM_INF,
    hlae_exe: Path = HLAE_EXE,
    csdm_exe: Path = CSDM_EXE,
    min_hlae: tuple[int, ...] = MIN_HLAE,
    min_csdm: tuple[int, ...] = MIN_CSDM,
) -> VersionCheckResult:
    """Local-only preflight. Safe to call before every render."""
    result = VersionCheckResult(ok=True)

    # --- CS2 ---
    try:
        cs2 = read_cs2_patch(steam_inf)
        result.versions["cs2"] = cs2
    except Exception as e:
        result.ok = False
        result.errors.append(("RENDER_CS2_VERSION_UNKNOWN", str(e)))
        cs2 = None

    # --- Demo vs CS2 ---
    if demo_path is not None:
        demo = Path(demo_path)
        try:
            if not demo.is_file():
                raise FileNotFoundError(f"demo not found: {demo}")
            demo_patch = read_demo_patch(demo)
            result.versions["demo"] = demo_patch
            if cs2 is not None and demo_patch != cs2:
                # Patched: allow CS2 newer than demo (minor drift) with warning.
                # Hard-fail only when demo is newer than installed CS2.
                import sys
                try:
                    d_parts = tuple(int(x) for x in demo_patch.split("."))
                    c_parts = tuple(int(x) for x in cs2.split("."))
                    demo_newer = d_parts > c_parts
                except Exception:
                    demo_newer = demo_patch > cs2
                if demo_newer:
                    result.ok = False
                    result.errors.append((
                        "RENDER_DEMO_GAME_MISMATCH",
                        f"demo patch {demo_patch} != CS2 {cs2}; update/downgrade game or re-acquire demo",
                    ))
                else:
                    print(f"[WARN] demo patch {demo_patch} != CS2 {cs2} (CS2 newer, trying anyway)", file=sys.stderr)
        except Exception as e:
            result.ok = False
            result.errors.append(("RENDER_DEMO_VERSION_UNKNOWN", str(e)))

    # --- HLAE ---
    try:
        if not hlae_exe.is_file():
            raise FileNotFoundError(f"HLAE not found: {hlae_exe}")
        hlae_ver = read_pe_version(hlae_exe)
        result.versions["hlae"] = format_version(hlae_ver)
        if not version_at_least(hlae_ver, min_hlae):
            result.ok = False
            result.errors.append((
                "RENDER_HLAE_OUTDATED",
                f"HLAE {format_version(hlae_ver)} < required {format_version(min_hlae)}; "
                f"update from https://github.com/advancedfx/advancedfx/releases",
            ))
    except Exception as e:
        result.ok = False
        code = "RENDER_HLAE_MISSING" if isinstance(e, FileNotFoundError) else "RENDER_HLAE_VERSION_UNKNOWN"
        result.errors.append((code, str(e)))

    # --- CSDM ---
    try:
        if not csdm_exe.is_file():
            raise FileNotFoundError(f"CSDM not found: {csdm_exe}")
        csdm_ver = read_pe_version(csdm_exe)
        result.versions["csdm"] = format_version(csdm_ver)
        if not version_at_least(csdm_ver, min_csdm):
            result.ok = False
            result.errors.append((
                "RENDER_CSDM_OUTDATED",
                f"CSDM {format_version(csdm_ver)} < required {format_version(min_csdm)}; "
                f"update from https://github.com/akiver/cs-demo-manager/releases",
            ))
    except Exception as e:
        result.ok = False
        code = "RENDER_CSDM_MISSING" if isinstance(e, FileNotFoundError) else "RENDER_CSDM_VERSION_UNKNOWN"
        result.errors.append((code, str(e)))

    return result


def assert_render_versions(demo_path: Path | str | None = None, **kwargs) -> dict[str, str]:
    """Raise RenderVersionError on failure; return versions dict on success."""
    result = check_render_versions(demo_path, **kwargs)
    result.raise_if_failed()
    return result.versions
