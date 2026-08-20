"""One-off: regenerate title/description/tags in an upload_meta.json using the
same generate_title.py args the pipeline would, then patch only those three
fields (leaving video_path/thumbnail_path/status intact)."""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BACKLOG = ROOT / "backlog/2396600-furia-vs-aurora-esports-world-cup/high/molodoy-mirage-2396600-furia-vs-aurora-esports-world-cup.json"
META = ROOT / "youtube/2396600_furia-vs-aurora-m3-mirage_molodoy_Mirage_overlay/upload_meta.json"

b = json.loads(BACKLOG.read_text())
ratings = b["ratings_path"]
steam_id = b["steam_id"]
render_dir = ROOT / f"renders/pov-furia-vs-aurora-m3-mirage_molodoy"

# crosshair from csdm analysis sidecar
code = ""
ap = render_dir / "csdm_analysis.json"
if ap.is_file():
    d = json.loads(ap.read_text())
    for pl in d.get("players", []):
        if str(pl.get("steamId") or "") == steam_id:
            code = str(pl.get("crosshairShareCode") or "").strip()
            break

args = [
    "scripts/pov/generate_title.py", str(ratings),
    "--player", b["player"], "--map", b["map"], "--variant", "overlay",
    "--tournament", b["tournament"],
]
if code:
    args += ["--crosshair-code", code]
for flag, key in (
    ("--viewmodel-fov", "viewmodel_fov"),
    ("--viewmodel-offset-x", "viewmodel_offset_x"),
    ("--viewmodel-offset-y", "viewmodel_offset_y"),
    ("--viewmodel-offset-z", "viewmodel_offset_z"),
    ("--viewmodel-presetpos", "viewmodel_presetpos"),
    ("--resolution", "resolution"),
    ("--aspect-ratio", "aspect_ratio"),
    ("--scaling-mode", "scaling_mode"),
    ("--video-settings-source", "video_settings_source"),
):
    v = b.get(key)
    if v is not None and v != "":
        args += [flag, str(v)]

r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=ROOT)
if r.returncode != 0:
    print("ERR", r.stderr[-2000:]); sys.exit(1)
gen = json.loads(r.stdout.strip())

meta = json.loads(META.read_text())
meta["title"] = gen["title"]
meta["description"] = gen["description"]
meta["tags"] = gen["tags"]
META.write_text(json.dumps(meta, indent=2))

print("title  :", meta["title"])
print("tags(#):", len(meta["tags"]), "| total chars:", sum(len(t) for t in meta["tags"]))
print("desc has <div>:", "<div" in meta["description"])
print("tags have <div>:", any("<div" in t for t in meta["tags"]))
