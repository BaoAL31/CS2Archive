"""Pre-render version gate: demo ↔ CS2 patch + HLAE↔CS2 pin + CSDM floors.

Local filesystem only — no network. Typical cost is tens of milliseconds
(steam.inf + two PE version resources + demoparser header).

HLAE is version-pinned to CS2 builds (AfxHookSource2 byte signatures). A CS2
update without a matching HLAE fails at hook time with a GUI dialog, not a
useful log line — so this gate hard-fails before launch when:
  - CS2 is newer than the highest pin in ``CS2_MIN_HLAE`` (table not updated), or
  - the HLAE CSDM actually launches is older than the pin for this CS2.
"""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cs2archive.config import settings  # CR-07: module scope (was local-only)

# Absolute floor when CS2 is older than every pin below.
MIN_HLAE = (2, 192, 0)
MIN_CSDM = (3, 20, 0)

# CS2 patch (inclusive lower bound) → minimum HLAE. Highest matching bound wins.
# Bump BOTH the new row AND the docs/bugs/hlae-steam-online-hook.md table when
# installing a new HLAE for a CS2 update. A CS2 newer than the top row hard-fails
# with RENDER_CS2_UNPINNED so a game update never silently burns HLAE retries.
CS2_MIN_HLAE: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = (
    ((1, 41, 9, 5), (2, 192, 10)),  # 2.192.10 still the newest upstream (2026-11-04 check); the .4->.5 gap assumed to move no hooked offsets, same pattern as .3->.4 (which verified live). Pending live verify.
    ((1, 41, 9, 4), (2, 192, 10)),  # 2.192.10 is still the newest upstream HLAE (2026-10-31 check); the .3->.4 gap moved no hooked offsets, same pattern as the .1->.2 pair. Verified live 2026-11-01 (one-round render: VERIFY-OK 95% weapon / 0% frozen).
    ((1, 41, 9, 3), (2, 192, 10)),  # Installed current hook; shared rig pin also recorded in CS2UtilArchive/config/hlae_for_cs2.json.
    ((1, 41, 9, 2), (2, 192, 8)),  # 1.41.9.2 is newer than the newest upstream HLAE: 2.192.8 (AfxHookSource2 0.41.8) is still the latest release, so the .1->.2 gap moved no hooked offsets, same as the 1.41.8.8/.9 pair. Verified live 2026-10-25 (one-round render).
    ((1, 41, 9, 1), (2, 192, 8)),  # HLAE 2.192.8 (AfxHookSource2 0.41.8, released 2026-10-14 for this patch); hooks 1.41.9.1 fine (verified 2026-10-16 one-round render: hook 2s, verify pass 64% weapon / 0% frozen, 518 MB — attempt 1 hit the known no-ffmpeg hook flake, attempt 2 OK)
    ((1, 41, 9, 0), (2, 192, 7)),  # HLAE 2.192.7 (released 2026-10-08 for this patch); hooks 1.41.9.0 fine (verified 2026-10-10 one-round render: hook 4s, verify pass, 504 MB)
    ((1, 41, 8, 9), (2, 192, 6)),  # 2.192.6 is the newest upstream HLAE; hooks .9 fine (verified 2026-10-08 one-round render: hook 3s, verify pass, 244 MB)
    ((1, 41, 8, 8), (2, 192, 6)),  # HLAE 2.192.6 hooks .8 fine (verified 2026-10-02 one-round render)
    ((1, 41, 8, 6), (2, 192, 6)),  # HLAE 2.192.6 / AfxHookSource2 0.41.6
    ((1, 41, 8, 5), (2, 192, 5)),  # HLAE 2.192.5 / AfxHookSource2 0.41.5
    ((1, 41, 8, 3), (2, 192, 4)),  # HLAE 2.192.4 / AfxHookSource2 0.41.4
    ((1, 41, 8, 2), (2, 192, 3)),  # HLAE 2.192.3 / AfxHookSource2 0.41.3
)

# Demo patches HLAE will not record on current CS2 (AfxHook loads, no ffmpeg /
# "Raw files not found"). Confirmed vs CS2 1.41.8.1: 1.41.3.8 and 1.41.4.1
# fail; 1.41.6.4 records. Current CS2 may still play a few older patches at or
# above MIN_RENDERABLE_DEMO_PATCH; it does not have to match steam.inf exactly.
INCOMPATIBLE_DEMO_PATCHES = frozenset({
    "1.41.3.8",
    "1.41.4.1",
})
MIN_RENDERABLE_DEMO_PATCH = (1, 41, 6, 4)

# CR-07: every one of these used to be a hardcoded machine path.
HLAE_EXE = settings.hlae_exe
CSDM_EXE = settings.csdm_exe
CSDM_SETTINGS = Path.home() / ".csdm" / "settings.json"
CS2_STEAM_INF = settings.cs2_steam_inf


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


def patch_tuple(patch: str) -> tuple[int, ...]:
    return tuple(int(x) for x in patch.split("."))


def demo_patch_too_old(demo_patch: str) -> bool:
    """True when this demo patch will not record on current CS2/HLAE."""
    if demo_patch in INCOMPATIBLE_DEMO_PATCHES:
        return True
    try:
        return patch_tuple(demo_patch) < MIN_RENDERABLE_DEMO_PATCH
    except ValueError:
        return False


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


def _width_padded(parts: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Zero-pad to *width* so prefix tuples compare numerically.

    ``patch_tuple("1.41.9")`` is a 3-tuple and ``(1,41,9) >= (1,41,9,0)`` is
    False, so a CS2 build reported without its trailing component skipped its
    OWN pin row and fell back to the previous row's HLAE floor — 2.192.6
    instead of 2.192.7 for the 1.41.9 row. That is the silent
    under-requirement this table exists to prevent (too-old HLAE burns hook
    retries), and it is reachable on any 3-part steam.inf PatchVersion.
    """
    return parts + (0,) * (width - len(parts))


def hlae_bounds_for_cs2(cs2_patch: str) -> tuple[tuple[int, ...], tuple[int, ...] | None] | None:
    """(min HLAE inclusive, max HLAE exclusive) for *cs2_patch*.

    None means CS2 is newer than every pin (table stale → RENDER_CS2_UNPINNED).
    Max is the HLAE built for the next newer CS2 pin: that build's signatures
    are missing on the older game (e.g. ReplayName on 2.192.5 vs CS2 1.41.8.3).
    """
    rows = sorted(CS2_MIN_HLAE, key=lambda row: row[0])
    width = max(len(bound) for bound, _ in rows)
    rows = [(_width_padded(bound, width), hlae) for bound, hlae in rows]
    cs2 = _width_padded(patch_tuple(cs2_patch), width)
    highest_bound = rows[-1][0]
    if cs2 > highest_bound:
        return None
    required = MIN_HLAE
    ceiling: tuple[int, ...] | None = rows[0][1]
    for i, (bound, hlae) in enumerate(rows):
        if cs2 >= bound:
            required = hlae
            ceiling = rows[i + 1][1] if i + 1 < len(rows) else None
    if ceiling == required:
        # Degenerate ceiling: the next row needs the SAME HLAE, so no "too new
        # for this CS2" build exists to exclude. Without this, a CS2 patch
        # whose successor shares its HLAE (1.41.8.6/.8, and now 1.41.9.1/.2)
        # would flag its own required HLAE as too new — RENDER_HLAE_CS2_MISMATCH
        # on the exact build the row mandates.
        ceiling = None
    return required, ceiling


def resolve_hlae_exe(
    *,
    csdm_settings: Path = CSDM_SETTINGS,
    fallback: Path = HLAE_EXE,
) -> Path:
    """HLAE binary CSDM will actually launch (custom path when enabled)."""
    try:
        data = json.loads(csdm_settings.read_text(encoding="utf-8"))
        hlae = ((data.get("video") or {}).get("hlae") or {})
        if hlae.get("customLocationEnabled"):
            loc = (hlae.get("customExecutableLocation") or "").strip()
            if loc:
                return Path(loc)
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return fallback


# ── HLAE auto-selection ─────────────────────────────────────────────────────
#
# CS2 updates (and Steam ROLLBACKS — 1.41.9.5 → 1.41.9.2 mid-hour, 2026-11-04)
# kept invalidating one hard-coded customExecutableLocation. But this machine
# keeps every HLAE ever installed under ~/.csdm/hlae-versions/, and the pin
# table already knows which one fits which CS2. So selection is now automatic:
# read steam.inf, pick the installed HLAE the pin table names (newest installed
# when CS2 is newer than every pin — the only candidate anyway), and repoint
# CSDM's settings.json before the render. The manual pin-row edit stays as the
# KNOWLEDGE base; it no longer gates the machine.

INSTALLED_HLAE_ROOT = Path.home() / ".csdm" / "hlae-versions"


def installed_hlae_versions(
    root: Path = INSTALLED_HLAE_ROOT,
) -> list[tuple[tuple[int, ...], Path]]:
    """(version, HLAE.exe path) for every installed HLAE, oldest first.

    Version comes from the PE header when readable, else the directory name
    (``HLAE-2.192.8``) — a copied tree with a stripped exe must still count.
    """
    found: list[tuple[tuple[int, ...], Path]] = []
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        exe = d / "HLAE.exe"
        if not exe.is_file():
            continue
        ver: tuple[int, ...] | None = None
        try:
            ver = read_pe_version(exe)
        except Exception:
            ver = None
        if ver is None:
            m = re.search(r"HLAE-(\d+)\.(\d+)\.(\d+)", d.name)
            if m:
                ver = tuple(int(x) for x in m.groups())
        if ver:
            found.append((ver, exe))
    return sorted(found, key=lambda item: item[0])


def select_hlae_for_cs2(
    cs2_patch: str,
    root: Path = INSTALLED_HLAE_ROOT,
) -> tuple[tuple[int, ...], Path, str] | None:
    """Best installed HLAE for *cs2_patch*: (version, exe path, how-chosen).

    ``how`` is ``"pin"`` when the pin table names it, ``"newest-installed"``
    when CS2 is newer than every pin (or nothing fits the table's range) and
    the newest installed HLAE is the only possible attempt. None when no HLAE
    is installed at all — that is the only still-fatal case.
    """
    installed = installed_hlae_versions(root)
    if not installed:
        return None
    bounds = hlae_bounds_for_cs2(cs2_patch)
    if bounds is not None:
        lo, hi = bounds
        in_range = [
            (ver, exe) for ver, exe in installed
            if version_at_least(ver, lo)
            and (hi is None or not version_at_least(ver, hi))
        ]
        if in_range:
            ver, exe = max(in_range, key=lambda item: item[0])
            return ver, exe, "pin"
    ver, exe = max(installed, key=lambda item: item[0])
    return ver, exe, "newest-installed"


def ensure_hlae_for_cs2(
    *,
    steam_inf: Path = CS2_STEAM_INF,
    installed_root: Path = INSTALLED_HLAE_ROOT,
    csdm_settings: Path = CSDM_SETTINGS,
) -> tuple[tuple[int, ...], Path, str] | None:
    """Point CSDM at the right installed HLAE for the running CS2. Idempotent.

    Rewrites ``video.hlae.customExecutableLocation`` in CSDM's settings.json
    only when it differs from the selection, backing the file up first
    (``settings.json.bak-hlae-auto-<ts>``). Returns the selection, or None
    when no HLAE is installed (caller must hard-fail; there is nothing to
    select).
    """
    cs2 = read_cs2_patch(steam_inf)
    selection = select_hlae_for_cs2(cs2, installed_root)
    if selection is None:
        return None
    ver, exe, how = selection
    current = resolve_hlae_exe(csdm_settings=csdm_settings)
    if current == exe:
        return selection
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = csdm_settings.with_name(csdm_settings.name + f".bak-hlae-auto-{stamp}")
    try:
        shutil.copy2(csdm_settings, backup)
        data = json.loads(csdm_settings.read_text(encoding="utf-8"))
        hlae = data.setdefault("video", {}).setdefault("hlae", {})
        hlae["customLocationEnabled"] = True
        hlae["customExecutableLocation"] = str(exe)
        csdm_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  [WARN] could not repoint CSDM at {exe} ({exc}) — using "
              f"whatever settings.json already names", flush=True)
        return selection
    print(f"  [HLAE] CS2 {cs2} -> HLAE {format_version(ver)} ({how}; was "
          f"{current.name}); settings backed up to {backup.name}", flush=True)
    return selection


def check_render_versions(
    demo_path: Path | str | None = None,
    *,
    steam_inf: Path = CS2_STEAM_INF,
    hlae_exe: Path | None = None,
    csdm_exe: Path = CSDM_EXE,
    csdm_settings: Path = CSDM_SETTINGS,
    # Explicit floor that bypasses the CS2 pin table (and its ceiling check).
    hlae_override: tuple[int, ...] | None = None,
    min_csdm: tuple[int, ...] = MIN_CSDM,
) -> VersionCheckResult:
    """Local-only preflight. Safe to call before every render."""
    result = VersionCheckResult(ok=True)
    if hlae_exe is None:
        hlae_exe = resolve_hlae_exe(csdm_settings=csdm_settings, fallback=HLAE_EXE)

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
            if demo_patch_too_old(demo_patch):
                result.ok = False
                result.errors.append((
                    "RENDER_DEMO_TOO_OLD",
                    f"demo patch {demo_patch} is too old for current CS2/HLAE "
                    f"(need >={format_version(MIN_RENDERABLE_DEMO_PATCH)})",
                ))
            elif cs2 is not None and demo_patch != cs2:
                # Current CS2 can still record a few older patches at/above
                # MIN_RENDERABLE_DEMO_PATCH. Hard-fail only when the demo is newer.
                try:
                    demo_newer = patch_tuple(demo_patch) > patch_tuple(cs2)
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

    # --- HLAE (the one CSDM launches) ---
    required_hlae = hlae_override
    hlae_ceiling: tuple[int, ...] | None = None
    if required_hlae is None and cs2 is not None:
        bounds = hlae_bounds_for_cs2(cs2)
        if bounds is None:
            # CS2 newer than every pin. Used to hard-fail (RENDER_CS2_UNPINNED),
            # but three patches running the row-add dance proved the gate adds
            # no information: the newest installed HLAE is the only candidate
            # either way. Proceed loudly — ensure_hlae_for_cs2() has already
            # selected it — so a game update no longer stalls the machine.
            top_cs2 = format_version(max(bound for bound, _ in CS2_MIN_HLAE))
            print(
                f"[WARN] CS2 {cs2} is newer than the HLAE compatibility table "
                f"(last pin: CS2 {top_cs2}). Proceeding with the newest "
                f"installed HLAE — if the hook misses its offsets, expect the "
                f"vanilla-viewer failure (Game error / Raw files not found) "
                f"and wait for https://github.com/advancedfx/advancedfx/releases.",
                file=sys.stderr,
            )
            required_hlae = MIN_HLAE
        else:
            required_hlae, hlae_ceiling = bounds
    if required_hlae is None:
        required_hlae = MIN_HLAE

    try:
        if not hlae_exe.is_file():
            raise FileNotFoundError(f"HLAE not found: {hlae_exe}")
        hlae_ver = read_pe_version(hlae_exe)
        result.versions["hlae"] = format_version(hlae_ver)
        result.versions["hlae_path"] = str(hlae_exe)
        too_old = not version_at_least(hlae_ver, required_hlae)
        too_new = (
            hlae_ceiling is not None
            and version_at_least(hlae_ver, hlae_ceiling)
        )
        if too_old or too_new:
            result.ok = False
            # CS2-specific code only when the requirement came from the pin table.
            code = (
                "RENDER_HLAE_CS2_MISMATCH"
                if hlae_override is None and cs2 is not None
                else "RENDER_HLAE_OUTDATED"
            )
            if too_new:
                detail = (
                    f"HLAE {format_version(hlae_ver)} is newer than CS2 {cs2} "
                    f"(need < {format_version(hlae_ceiling)}; "
                    f"a newer hook looks for symbols this build does not have)"
                )
            else:
                detail = (
                    f"HLAE {format_version(hlae_ver)} < required {format_version(required_hlae)}"
                    + (f" for CS2 {cs2}" if cs2 else "")
                )
            result.errors.append((
                code,
                f"{detail} (using {hlae_exe}); "
                f"point ~/.csdm/settings.json video.hlae.customExecutableLocation "
                f"at the matching HLAE from https://github.com/advancedfx/advancedfx/releases",
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
