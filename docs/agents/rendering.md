# Demo Video Rendering

> Reference doc extracted from `AGENTS.md`. Read this when rendering POV videos, debugging HLAE capture failures, or touching CSDM/ffmpeg commands.

Uses **CS2 Demo Manager (csdm)** CLI to render POV videos. Installed at:
```
C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd
```

## Recording Mode

Uses `--recording-system HLAE` — csdm drives HLAE `mirv_streams` to encode directly to video via FFmpeg (no TGA/PNG sequence on disk).

**Critical:** `--output` must be an **absolute** path. Relative paths (e.g. `renders/...`) resolve from the CS2 install directory and cause `AFXERROR: Failed writing image for screen recording` → csdm **Raw files not found**. `render_pov.py` and the pipeline always pass `Path.resolve()` output dirs.

HLAE **2.190.1+** required (`C:\Program Files (x86)\HLAE\HLAE.exe`). Disable RTSS/MSI OSD and Steam/Xbox overlays if capture fails. After CS2 updates, if HLAE breaks again, test one round with absolute output before full pipeline runs.

## VP9 Trick (sharper YouTube uploads)

Render at **2560×1440** even for 1080p-targeted uploads. YouTube allocates VP9 codec (higher bitrate) to 1440p+ uploads, while 1080p gets H.264. Video looks sharper even when watched at 1080p because YouTube uses better encoding.

All scripts default to 2560×1440; per-round render and concat upscale use **h264_nvenc CQ 15** (match quality end-to-end).

## Rounds-Only POV (full HUD, no x-ray, batch rendering)

For rendering a player's POV with full HUD (radar, health, ammo) and no x-ray, in configurable batch sizes (default 3 rounds per csdm call):

```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" --mode player --steamids <steam64_id> --event rounds --rounds <N> --perspective player --no-show-x-ray --output "C:\full\path\to\renders\pov-folder" --framerate 60 --width 2560 --height 1440 --recording-system HLAE --close-game-after-recording --no-show-only-death-notices --show-assists --record-audio --concatenate-sequences --ffmpeg-video-codec h264_nvenc --ffmpeg-crf 15 --ffmpeg-output-parameters "-cq 15 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1" --cfg "C:\full\path\to\assets\cs2_pov.cfg"
```

Use `python scripts/pov/render_pov.py <demo_path> <steam_id>` instead — it wraps the above command with auto round-detection, p1/p2 split handling, and batch output naming.

All scripts pass `--cfg assets/cs2_pov.cfg` which configures HUD and restores keybinds via `exec autoexec`. The crosshair comes from CS2's `autoexec.cfg` in the game's `csgo/cfg/` directory — `render_pov.py` swaps `autoexec_render.cfg` (pro's crosshair, extracted from demo) and `autoexec_personal.cfg` (your crosshair) before/after rendering.

## Split demos (p1, p2)

HLTV sometimes splits match demos into parts. The render script auto-detects companion parts and renders them sequentially.

To manually concatenate split renders:
```powershell
ffmpeg -f concat -safe 0 -i <filelist.txt> -c copy "renders\combined.mp4"
```

## Concatenating rendered rounds

After rendering all rounds with `python scripts/pov/render_pov.py`, join them into one video:
```powershell
python scripts/pov/concat_rounds.py <renders_folder>
```
Output is `combined.mp4` in the same folder. Concat is incremental (one batch at a time with ffmpeg stream copy), then upscaled to 1440p via CUDA Lanczos. Each batch file is deleted after successful append — remaining `batch-*.mp4` files on disk indicate which batches still need to be concat'd on resume.
