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

## Scoreboard Avatar-Box Calibration

`scripts/overlay/voice_shade.py` dims/reveals the POV team's scoreboard
avatars. Its rectangles live in `scripts/overlay/avatar_boxes.py` and must be
measured in the player's **native render resolution**, not in the final
2560×1440 upload: `concat_rounds.py` may stretch a 4:3 or 16:10 render.

When a box is offset or clips a neighbouring avatar:

1. Render one round only at the POV's native resolution, into an isolated
   calibration directory:

   ```powershell
   $env:PYTHONPATH="."
   & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/render_pov.py `
     "<demo>.dem" <steam64> --rounds 1 --width <native_width> --height <native_height> `
     --output "renders/avatar_calibration_<player>" --batches 1
   ```

2. Extract a buy-phase/match-start frame (the top HUD has all ten colored
   frames), for example:

   ```powershell
   ffmpeg -ss 5 -i "renders/avatar_calibration_<player>\round-*.mp4" `
     -frames:v 1 "renders/avatar_calibration_<player>\hud_calibration.png"
   ```

3. Measure each colored frame directly in that native frame. Use the
   rectangle's outer bounds: `x0` inclusive, `x1` inclusive, `y0` inclusive,
   `y1` inclusive. Put the five left-team ranges in `LEFT`, then the five
   right-team ranges in `RIGHT`.

4. Make a contact sheet of the ten crops before editing the config. Every
   crop must contain exactly one full colored frame—no adjacent frame, empty
   gap, or clipped border. Then repeat the same crop test after stretching the
   source to the final output resolution, using the scale applied by the
   pipeline.

Do not use `detect_avatar_boxes.py` as the authority for final coordinates:
its broad HSV mask can mistake saturated portrait artwork for a colored HUD
border. It is a starting probe only; the rendered contact-sheet check is the
acceptance test.

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
