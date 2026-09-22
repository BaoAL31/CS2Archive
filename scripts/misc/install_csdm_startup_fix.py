"""Install/undo the tested CSDM 3.20.1 startup fix without replacing its stock DLL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = "cs2archive_startup_3_20_1"
COMMAND = "+csdm_initialize"
STOCK_SHA256 = "43f15c8c689efb4b1edad6b0b7b537bc4cf36223b870b8356a430f65a800a4d7"
PATCHED_SHA256 = "9042b9abeb1efee881440c1c96aad74858791f69c63ec8118611c6ae7b3c9f79"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Replace atomically, including settings: never leave partially written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(settings_path: Path, binary_dir: Path, binary: Path, journal: Path) -> None:
    if digest(binary_dir / "server.dll") != STOCK_SHA256:
        raise RuntimeError("CSDM stock plugin changed; this fix only supports the tested 3.20.1 binary")
    if digest(binary) != PATCHED_SHA256:
        raise RuntimeError("Candidate binary does not match the tested clean build")
    data = json.loads(settings_path.read_bytes())
    playback = data.setdefault("playback", {})
    target = binary_dir / f"server_{VERSION}.dll"
    if journal.exists():
        saved = json.loads(journal.read_bytes())
        if saved["settings_path"] != str(settings_path.resolve()):
            raise RuntimeError("Existing installation journal belongs to other settings")
        if all(playback.get(k) == v for k, v in saved["after"].items()) and target.exists() and digest(target) == PATCHED_SHA256:
            return
        raise RuntimeError("Existing installation or interrupted install; undo it before installing again")
    if playback.get("cs2PluginVersion") not in (None, "latest"):
        raise RuntimeError("Another custom plugin is selected; refusing to replace it")
    params = str(playback.get("launchParameters") or "")
    if COMMAND in params.split():
        raise RuntimeError("Initialization command already present without an installation journal")
    after = {"cs2PluginVersion": VERSION, "launchParameters": params + " " + COMMAND}
    before = {k: {"present": k in playback, "value": playback.get(k)} for k in after}
    if target.exists() and digest(target) != PATCHED_SHA256:
        raise RuntimeError("Named plugin file already exists with different contents")
    # Persist recovery information before any installed-state mutation.
    write_json(journal, {"settings_path": str(settings_path.resolve()), "before": before, "after": after})
    if not target.exists():
        with target.open("xb") as stream:
            stream.write(binary.read_bytes())
    playback.update(after)
    write_json(settings_path, data)


def undo(settings_path: Path, journal: Path) -> None:
    saved = json.loads(journal.read_bytes())
    if saved["settings_path"] != str(settings_path.resolve()):
        raise RuntimeError("Installation journal belongs to other settings")
    data = json.loads(settings_path.read_bytes())
    playback = data.setdefault("playback", {})
    for key, after in saved["after"].items():
        before = saved["before"][key]
        original_matches = (key in playback) == before["present"] and playback.get(key) == before["value"]
        if playback.get(key) != after and not original_matches:
            raise RuntimeError(f"{key} changed since installation; refusing to overwrite it")
    for key, before in saved["before"].items():
        if before["present"]:
            playback[key] = before["value"]
        else:
            playback.pop(key, None)
    write_json(settings_path, data)
    journal.unlink()
    # Keep the inactive named DLL; never delete or replace the stock plugin.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "undo"])
    parser.add_argument("--binary", type=Path, default=ROOT / "assets/csdm-startup-fix/server.dll")
    args = parser.parse_args()
    import psutil
    busy = [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in {"cs2.exe", "cs-demo-manager.exe"}]
    if busy:
        raise SystemExit(f"Close CS2/CSDM before changing plugin settings (processes {busy})")
    settings_path = Path.home() / ".csdm/settings.json"
    journal = ROOT / ".data/csdm-startup-fix-install.json"
    binary_dir = Path(os.environ["LOCALAPPDATA"]) / "Programs/cs-demo-manager/resources/static/cs2"
    if args.action == "install":
        install(settings_path, binary_dir, args.binary, journal)
    else:
        undo(settings_path, journal)
    print(f"CSDM startup fix: {args.action} complete")


if __name__ == "__main__":
    main()
