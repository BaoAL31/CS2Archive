"""Swift DemoUI Pro's real Panorama speaker HUD for CSDM/HLAE renders.

Install the pinned upstream package locally with ``--install``. The game mount
is temporary and journaled; ``--restore`` recovers an interrupted render.
The adapted HUD is display-only: existing CS2Archive code owns voice audio.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.1.6"
PACKAGE = ROOT / ".cache/swift-demoui/package" / f"SwiftDemoUIPro-v{VERSION}"
ZIP_SHA256 = "24889bf370953176bd1eb3d02701a3395fab9d9ff12b47828f7116c72dc203a2"
JS_SHA256 = "51eaef443a5c485d0dc37fc123e9a7030eced8d8dd105ae7a26883c0f98d6071"
INDEXER_SHA256 = "59b1f8146c0995174ab83911dd4761c98cc3de09b3f92ec68fa569e89a3167a2"
MENU_SHA256 = "e0146b922e7a6d9a96452d74ad12b09b94100a7e5bc414d7424de48947a59d3b"
JOURNAL = ".cs2archive-swift-session.json"
MOUNT = "overrides/cs2archive_swift"
PROFILE = "swift-demoui-0.1.6-archive-2"
HUD_MARKER = "voice_indicators.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install() -> None:
    """Download a pinned official release, verify it, retain upstream licenses."""
    cache = PACKAGE.parent.parent
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"SwiftDemoUIPro-v{VERSION}-win64.zip"
    base = f"https://github.com/nicedayzhu/SwiftDemoUIPro/releases/download/v{VERSION}"
    if not archive.is_file() or _sha(archive.read_bytes()) != ZIP_SHA256:
        urllib.request.urlretrieve(f"{base}/{archive.name}", archive)
    if _sha(archive.read_bytes()) != ZIP_SHA256:
        raise RuntimeError("Swift release checksum mismatch")
    with zipfile.ZipFile(archive) as bundle:
        destination = PACKAGE.parent.resolve()
        for name in bundle.namelist():
            if not (destination / name).resolve().is_relative_to(destination):
                raise RuntimeError("Unsafe path in Swift release")
        bundle.extractall(destination)
    js = PACKAGE / "swift_demo_voice.js"
    urllib.request.urlretrieve(
        f"https://raw.githubusercontent.com/nicedayzhu/SwiftDemoUIPro/v{VERSION}/"
        "addon/panorama/scripts/hud/swift_demo_voice.js", js,
    )
    if _sha(js.read_bytes()) != JS_SHA256:
        raise RuntimeError("Swift JavaScript checksum mismatch")
    print(f"Installed verified Swift DemoUI Pro {VERSION}: {PACKAGE}")


def patch_runtime(source: str, allowed_steamids: list[str], names: dict[str, str],
                  *, native: bool = True) -> str:
    """Adapt pinned upstream code, refusing to guess if upstream changes.

    ``native=True`` (canonical) restyles speaker rows to the in-game HUD.
    ``native=False`` keeps Swift's original dark-bar chrome (legacy).
    """
    if not allowed_steamids or any(not str(sid).isdigit() for sid in allowed_steamids):
        raise ValueError("A nonempty POV-team SteamID list is required")
    config = json.dumps({
        "allowed": {str(s): True for s in allowed_steamids},
        "names": names,
        "native": bool(native),
    }, ensure_ascii=True)
    source = "var CS2ArchiveVoice = " + config + ";\n" + source
    replacements = {
        "function _RunMaskCommands(low, high, status) {":
            "function _RunMaskCommands(low, high, status) { return; // CS2Archive owns audio.\n",
        "function _UnmuteNativePlayer(player) {":
            "function _UnmuteNativePlayer(player) { return false; // Display-only integration.\n",
        "function _FilterSpeakingSlotsForSelection(slots) {": """function _FilterSpeakingSlotsForSelection(slots) {
        return (slots || []).filter(function (slot) {
            var player = _PlayerBySlot(slot);
            return !!(player && CS2ArchiveVoice.allowed[String(player.xuid)]);
        }); // Stable team identities survive halftime and slot remapping.
""",
        "function _SetMenuVisible(visible) {": """function _SetMenuVisible(visible) {
        visible = false;
        var archiveDock = _Panel("SwiftDemoVoiceDock");
        var archiveToggle = _Panel("SwiftDemoVoiceMenuToggle");
        if (archiveDock) archiveDock.visible = false;
        if (archiveToggle) archiveToggle.visible = false;
""",
        "if (name) name.text = player.name;":
            "if (name) name.text = CS2ArchiveVoice.names[String(player.xuid)] || player.name;\n"
            "			if (CS2ArchiveVoice.native) _StyleNativeSpeaking(notice, player, name);",
        "	function _RenderSpeakingPlayers(slots) {":
            """	function _StyleNativeSpeaking(notice, player, name) {
		var overlay = _Panel("SwiftDemoVoiceStatusOverlay");
		if (overlay) {
			overlay.style.margin = "0px 0px 168px 10px";
			overlay.style.width = "480px";
		}
		notice.style.backgroundColor = "#00000000";
		notice.style.border = "0px solid #00000000";
		notice.style.height = "26px";
		notice.style.width = "480px";
		notice.style.marginTop = "2px";
		if (name) {
			name.style.fontSize = "18px";
			name.style.fontWeight = "bold";
			name.style.color = player.team === "TERRORIST" ? "#E4AE39" : "#B8D4E8";
			name.style.textShadow = "1px 1px 0px 2.0 #000000ff";
		}
		var avatar = notice.FindChildTraverse("SwiftDemoSpeakingAvatar");
		if (avatar) {
			avatar.style.width = "22px";
			avatar.style.height = "22px";
			avatar.style.borderRadius = "3px";
		}
	}
	function _RenderSpeakingPlayers(slots) {""",
    }
    for before, after in replacements.items():
        if source.count(before) != 1:
            raise RuntimeError(f"Swift runtime does not match pinned version: {before}")
        source = source.replace(before, after)
    return source


def write_vpk(resources: dict[str, bytes]) -> bytes:
    """Write a small inline VPK v1 with CRCs (compiled resources, no archives)."""
    groups: dict[str, dict[str, list[tuple[str, bytes]]]] = {}
    for name, data in sorted(resources.items()):
        path = Path(name)
        groups.setdefault(path.suffix[1:], {}).setdefault(path.parent.as_posix(), []).append((path.stem, data))
    tree, payload = bytearray(), bytearray()
    def string(value):
        tree.extend(value.encode("utf-8") + b"\0")
    for ext, paths in groups.items():
        string(ext)
        for parent, files in paths.items():
            string(parent)
            for stem, data in files:
                string(stem)
                tree.extend(struct.pack("<IHHIIH", zlib.crc32(data), 0, 0x7fff, len(payload), len(data), 0xffff))
                payload.extend(data)
            string("")
        string("")
    string("")
    return struct.pack("<III", 0x55aa1234, 1, len(tree)) + tree + payload


def read_vpk(data: bytes) -> dict[str, bytes]:
    """Validate/extract inline resources; supports upstream's VPK v1/v2."""
    signature, version, size = struct.unpack_from("<III", data)
    if signature != 0x55aa1234 or version not in (1, 2):
        raise ValueError("Unsupported VPK header")
    header = 12 if version == 1 else 28
    pos, end = header, header + size
    if end > len(data):
        raise ValueError("Truncated VPK tree")
    def string():
        nonlocal pos
        stop = data.index(b"\0", pos, end)
        result = data[pos:stop].decode("utf-8")
        pos = stop + 1
        return result
    resources = {}
    while ext := string():
        while parent := string():
            while stem := string():
                crc, preload, archive, offset, length, terminator = struct.unpack_from("<IHHIIH", data, pos)
                pos += 18
                if archive != 0x7fff or terminator != 0xffff:
                    raise ValueError("Expected inline VPK resource")
                if pos + preload > end or end + offset + length > len(data):
                    raise ValueError("Truncated VPK resource")
                body = data[pos:pos + preload] + data[end + offset:end + offset + length]
                pos += preload
                if zlib.crc32(body) != crc:
                    raise ValueError("VPK resource CRC mismatch")
                resources[f"{parent}/{stem}.{ext}"] = body
    return resources


def capture_profile(native: bool) -> str:
    return PROFILE if native else PROFILE + "-chrome"


def prepare(demo: Path, steam_id: str, output: Path, names: dict[str, str] | None = None,
            *, native: bool = True) -> tuple[Path, Path]:
    """Build per-demo data plus an adapted runtime, without touching CS2."""
    source = PACKAGE / "swift_demo_voice.js"
    indexer = PACKAGE / "swift-demo-voice-indexer.exe"
    menu = PACKAGE / "swift_demo_menu_override.vpk"
    if not all(p.is_file() for p in (source, indexer, menu)):
        raise RuntimeError("Swift DemoUI Pro missing. Run python scripts/overlay/swift_demoui.py --install")
    if _sha(source.read_bytes()) != JS_SHA256:
        raise RuntimeError("Swift runtime checksum mismatch; reinstall the pinned package")
    if _sha(indexer.read_bytes()) != INDEXER_SHA256 or _sha(menu.read_bytes()) != MENU_SHA256:
        raise RuntimeError("Swift binary checksum mismatch; reinstall the pinned package")
    # Import only when building: archive inspection and recovery need no demo parser.
    sys.path.insert(0, str(ROOT / "scripts/faceit"))
    from mix_team_voice import load_team_map
    allowed = sorted(load_team_map(demo, steam_id))
    if not allowed or steam_id not in allowed:
        raise RuntimeError("Cannot identify the POV team for Swift voice indicators")
    stat = demo.stat()
    identity = {"profile": capture_profile(native), "demo": str(demo.resolve()), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "steam_id": steam_id, "allowed": allowed,
                "names": names or {}, "native": bool(native)}
    key = _sha(json.dumps(identity, sort_keys=True).encode())[:20]
    work = output / ".swift-demoui" / key
    work.mkdir(parents=True, exist_ok=True)
    session = work / "session.vpk"
    manifest = work / "manifest.json"
    if session.is_file() and manifest.is_file():
        saved = json.loads(manifest.read_text())
        if saved.get("identity") == identity and saved.get("sha256") == _sha(session.read_bytes()):
            return menu, session
    def run(*args):
        result = subprocess.run([str(indexer), *map(str, args)], capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(f"Swift indexer failed: {result.stderr[-2000:]}")
        print(result.stdout.strip())
    data_vpk = work / "voice-data.vpk"
    run("build-session-vpk", demo.resolve(), data_vpk.resolve())
    resources = read_vpk(data_vpk.read_bytes())
    runtime = work / "swift_demo_voice.js"
    runtime.write_text(patch_runtime(source.read_text(encoding="utf-8"), allowed, names or {},
                                    native=native), encoding="utf-8")
    compiled = runtime.with_suffix(".vjs_c")
    run("compile-vjs", runtime.resolve(), compiled.resolve())
    resources["panorama/scripts/hud/swift_demo_voice.vjs_c"] = compiled.read_bytes()
    packed = write_vpk(resources)
    if read_vpk(packed) != resources:
        raise RuntimeError("Swift session VPK verification failed")
    session.write_bytes(packed)
    manifest.write_text(json.dumps({"identity": identity, "sha256": _sha(packed)}, indent=2), encoding="utf-8")
    return menu, session


def _mounted_gameinfo(original: bytes) -> bytes:
    # Preserve all existing bytes, comments, line endings and other SearchPaths.
    pattern = rb"(?m)^([ \t]*)Game[ \t]+csgo[ \t]*(?://[^\r\n]*)?\r?$"
    match = re.search(pattern, original)
    if not match or len(re.findall(pattern, original)) != 1:
        raise RuntimeError("Cannot identify the base Game csgo SearchPath; refusing to modify gameinfo.gi")
    if b"cs2archive_swift" in original or b"swift_demo_menu_override" in original:
        raise RuntimeError("A Swift mount is already active; restore that session first")
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    indent = match.group(1)
    added = b"".join(indent + b"Game\tcsgo/" + MOUNT.encode() + b"/" + f + newline
                     for f in (b"session.vpk", b"menu.vpk"))
    return original[:match.start()] + added + original[match.start():]


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cs2archive-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(csgo: Path) -> None:
    """Restore only our recorded session; refuse to overwrite concurrent edits."""
    journal = csgo / JOURNAL
    if not journal.exists():
        return
    state = json.loads(journal.read_text(encoding="utf-8"))
    gameinfo = csgo / "gameinfo.gi"
    original = base64.b64decode(state["original"])
    current = gameinfo.read_bytes()
    if current != original and _sha(current) != state["mounted_sha256"]:
        raise RuntimeError(f"gameinfo.gi changed during render. Preserve {journal} and restore manually")
    _atomic_write(gameinfo, original)
    for name in ("session.vpk", "menu.vpk"):
        (csgo / MOUNT / name).unlink(missing_ok=True)
    try:
        (csgo / MOUNT).rmdir()
    except OSError:
        pass
    journal.unlink()


@contextmanager
def mounted_hud(csgo: Path, menu: Path, session: Path):
    """Mount for one CSDM invocation (including retries), restore on any exit."""
    csgo = csgo.resolve()
    gameinfo = csgo / "gameinfo.gi"
    original = gameinfo.read_bytes()
    mounted = _mounted_gameinfo(original)
    journal = csgo / JOURNAL
    if journal.exists() or (csgo / MOUNT).exists():
        raise RuntimeError("Swift render session already active; use --restore after its CS2 session exits")
    state = {"original": base64.b64encode(original).decode(), "mounted_sha256": _sha(mounted)}
    try:
        with journal.open("x", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RuntimeError("Swift render session already active; restore before continuing") from exc
    try:
        (csgo / MOUNT).mkdir(parents=True)
        shutil.copy2(menu, csgo / MOUNT / "menu.vpk")
        shutil.copy2(session, csgo / MOUNT / "session.vpk")
        _atomic_write(gameinfo, mounted)
        yield
    finally:
        restore(csgo)


def validate_render_profile(output: Path, style: str, steam_id: str) -> None:
    """Do not resume old footage as if it contained the new baked-in HUD."""
    native = style == "swift"
    enabled = style in ("swift", "legacy")
    marker = output / HUD_MARKER
    expected = {"profile": capture_profile(native) if enabled else "off", "steam_id": steam_id}
    recorded = json.loads(marker.read_text()) if marker.is_file() else None
    has_video = any(output.glob("round-*.mp4")) or any(output.glob("batch-*.mp4")) or (output / "combined.mp4").exists()
    if has_video and recorded != expected and (enabled or recorded is not None):
        raise RuntimeError("Existing render uses a different voice indicator style. Keep its saved progress; "
                           "use a new --output folder for Swift or resume the original style.")
    output.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(expected, indent=2), encoding="utf-8")


def require_swift_capture(output: Path, steam_id: str, *, native: bool = True) -> None:
    marker = output / HUD_MARKER
    expected = {"profile": capture_profile(native), "steam_id": steam_id}
    if not marker.is_file() or json.loads(marker.read_text()) != expected:
        raise RuntimeError("Swift speaker indicators must be captured in step 2. This saved render "
                           "does not have them. Resume it with --voice-indicators shade, or render "
                           "Swift into a new output folder; do not delete saved pipeline progress.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--demo", type=Path)
    parser.add_argument("--steam-id")
    parser.add_argument("--output", type=Path, default=ROOT / "renders/swift-preview")
    parser.add_argument("--legacy-hud", action="store_true",
                        help="Keep Swift's original dark-bar speaker chrome instead of the native HUD.")
    args = parser.parse_args()
    if args.install:
        install()
    elif args.restore:
        sys.path.insert(0, str(ROOT / "scripts"))
        from config import settings
        restore(Path(settings.cs2_cfg_dir).parent)
    elif args.demo and args.steam_id:
        menu, session = prepare(args.demo, args.steam_id, args.output, native=not args.legacy_hud)
        print(f"Prepared (game unchanged): {menu}\n{session}")
    else:
        parser.error("Use --install, --restore, or --demo with --steam-id")


if __name__ == "__main__":
    main()
