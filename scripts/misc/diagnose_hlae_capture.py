"""Bounded, non-retrying capture probe. Saves PID/module evidence before cleanup.

Does not change Steam mode or rename installed DLLs. Optional console/plugin
flags temporarily change CSDM settings and restore them on normal exit/errors.
Refuses to launch alongside an existing CS2. Terminates only its identified
test CS2 and observed launch descendants. Evidence stays in a fresh directory.
Do not forcibly kill this helper while temporary settings are active.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager

import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from config import settings


@contextmanager
def console_logging(enabled: bool, plugin: Path | None = None, extra: str = ""):
    """Temporarily enable the engine's own startup log; preserve other settings."""
    if not enabled and plugin is None and not extra:
        yield
        return
    path = Path.home() / ".csdm/settings.json"
    original = path.read_bytes()
    data = json.loads(original)
    playback = data.setdefault("playback", {})
    params = str(playback.get("launchParameters") or "")
    playback["launchParameters"] = params + (" -condebug" if enabled else "") + (" " + extra if extra else "")
    custom_binary = None
    if plugin:
        binary = plugin.read_bytes()
        version = "cs2archive_probe_" + hashlib.sha256(binary).hexdigest()[:12]
        custom_binary = Path(settings.csdm_cmd).parent / "resources/static/cs2" / f"server_{version}.dll"
        with custom_binary.open("xb") as stream:
            stream.write(binary)
        playback["cs2PluginVersion"] = version
    changed = (json.dumps(data, indent=2) + "\n").encode()
    path.write_bytes(changed)
    try:
        yield
    finally:
        if path.read_bytes() == changed:
            path.write_bytes(original)
            if custom_binary:
                custom_binary.unlink()
        else:
            raise RuntimeError("CSDM settings changed concurrently; refusing to overwrite them")


def classify_plugin_log(text: str, hlae_loaded: bool) -> str:
    """Describe observed stages, without inventing Steam/overlay causation."""
    if "Executed: mirv_streams record start" in text:
        return "record_command_executed"
    if "Hooked FrameStageNotify" in text:
        return "frame_hook_ready_record_command_not_observed"
    if "ClientFullyConnect: playerSlot=" in text:
        return "client_connected_frame_hook_not_observed"
    if "CreateInterface called with Source2GameClients001" in text:
        return "client_connection_callback_not_observed"
    if hlae_loaded:
        return "hlae_loaded_plugin_initialization_not_observed"
    return "hlae_injection_not_observed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--console", action="store_true", help="Temporarily add -condebug, restore settings afterward")
    parser.add_argument("--plugin", type=Path, help="Temporarily select an isolated diagnostic plugin build")
    parser.add_argument("--launch-extra", default="", help="Temporary diagnostic launch parameters")
    args = parser.parse_args()
    existing = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "cs2.exe"}
    if existing:
        raise SystemExit(f"Existing CS2 processes {sorted(existing)}; refusing concurrent capture")
    out = ROOT / "renders" / ("hlae-diagnostic-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["outputFolderPath"] = str(out)
    path = out / "capture.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env.update(SteamAppId="730", SteamGameId="730", DISABLE_VK_LAYER_VALVE_steam_overlay_1="1")
    launched_at = time.time()
    started = time.monotonic()
    owned = {}
    previous = None
    hlae_loaded = False
    plugin_hash_recorded = False
    print(f"Evidence: {out}", flush=True)
    with console_logging(args.console, args.plugin, args.launch_extra), (out / "csdm.log").open("w", encoding="utf-8") as log, (out / "processes.jsonl").open("w", encoding="utf-8") as evidence:
        proc = subprocess.Popen([settings.csdm_cmd, "video", "--config-file", str(path)], stdout=log, stderr=subprocess.STDOUT, env=env)
        owned[proc.pid] = psutil.Process(proc.pid)
        try:
            while time.monotonic() - started < args.seconds:
                mounted_plugin = Path(settings.cs2_cfg_dir).parent / "csdm/bin/server.dll"
                if args.plugin and not plugin_hash_recorded and mounted_plugin.is_file():
                    actual = hashlib.sha256(mounted_plugin.read_bytes()).hexdigest()
                    expected = hashlib.sha256(args.plugin.read_bytes()).hexdigest()
                    print(f"Plugin expected={expected} mounted={actual}", flush=True)
                    if actual == expected:
                        plugin_hash_recorded = True
                engine_log = Path(settings.cs2_cfg_dir).parent / "csdm/console.log"
                if args.console and engine_log.is_file() and engine_log.stat().st_mtime >= launched_at:
                    try:
                        (out / "engine-console.log").write_bytes(engine_log.read_bytes())
                    except OSError:
                        pass
                rows = []
                for p in psutil.process_iter(["name", "ppid", "create_time"]):
                    try:
                        name = (p.info["name"] or "").lower()
                        if p.info["ppid"] in owned and p.pid not in owned:
                            owned[p.pid] = p
                        # HLAE's loader can exit between polls. The exact demo
                        # argument plus creation time identifies this probe's CS2.
                        if name == "cs2.exe" and p.create_time() >= launched_at and config["demoPath"] in p.cmdline():
                            owned[p.pid] = p
                        if name not in {"cs2.exe", "hlae.exe", "ffmpeg.exe", "rtss.exe", "msiafterburner.exe"}:
                            continue
                        row = {"pid": p.pid, "ppid": p.ppid(), "name": name, "owned": p.pid in owned}
                        if name in {"cs2.exe", "hlae.exe", "ffmpeg.exe"}:
                            row["cmdline"] = p.cmdline()
                        if name == "cs2.exe":
                            try:
                                row["modules"] = sorted({m.path for m in p.memory_maps() if any(s in m.path.lower() for s in ("afxhook", "rtsshooks", "gameoverlay", "steam_api", "server.dll"))})
                                hlae_loaded |= any("afxhooksource2" in m.lower() for m in row["modules"])
                            except psutil.Error as exc:
                                row["module_error"] = type(exc).__name__
                        rows.append(row)
                    except psutil.Error:
                        continue
                if rows != previous:
                    record = {"elapsed": round(time.monotonic() - started, 2), "processes": rows}
                    evidence.write(json.dumps(record) + "\n")
                    evidence.flush()
                    print(json.dumps(record), flush=True)
                    previous = rows
                if proc.poll() is not None:
                    print(f"CSDM exited {proc.returncode} at {time.monotonic()-started:.1f}s", flush=True)
                    break
                time.sleep(1)
        finally:
            plugin_log = Path(settings.cs2_cfg_dir).parent.parent / "bin/win64/csdm.log"
            plugin_text = ""
            if plugin_log.is_file() and plugin_log.stat().st_mtime >= launched_at:
                (out / "plugin.log").write_bytes(plugin_log.read_bytes())
                plugin_text = plugin_log.read_text(encoding="utf-8", errors="replace")
            summary = {"elapsed": round(time.monotonic()-started, 2), "csdm_exit_before_cleanup": proc.poll(),
                       "hlae_loaded": hlae_loaded, "plugin_stage": classify_plugin_log(plugin_text, hlae_loaded)}
            (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            evidence.write(json.dumps({"elapsed": round(time.monotonic()-started, 2), "cleanup": True, "csdm_exit_before_cleanup": proc.poll()}) + "\n")
            for p in reversed(list(owned.values())):
                try:
                    if p.is_running() and p.name().lower() != "steam.exe":
                        p.terminate()
                except psutil.Error:
                    pass
            psutil.wait_procs(list(owned.values()), timeout=5)
    print("Result:", summary, flush=True)
    print("Videos:", [(str(p.relative_to(out)), p.stat().st_size) for p in out.rglob("*.mp4")], flush=True)


if __name__ == "__main__":
    main()
