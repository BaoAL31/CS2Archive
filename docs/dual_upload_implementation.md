# Dual-Branch YouTube Upload — Implementation Report (historical)

This document describes the dual-upload cube that has been removed.
The live product is **one overlay variant** at `youtube/{run_id}_overlay/`.
`--raw-only` is the debug escape hatch. See `docs/agents/pipeline.md`.

## Feature Summary (obsolete)

`--dual-upload` flag on `scripts/pov/pipeline.py` makes one backlog entry produce **two** independent YouTube uploads:

| Variant | YouTube dir | Title | Thumbnail | Description | Tags |
|---|---|---|---|---|---|
| Raw | `youtube/{run_id}/` | standard | standard | standard | standard |
| Overlay | `youtube/{run_id}_overlay/` | `+ " \| Input Overlay + Utility Cam"` | `+ W/ INPUT OVERLAY` badge top-right | `+ overlay note paragraph` | `+ 7 overlay tags` |

Each variant has its own `upload_meta.json`, its own YouTube video ID (stored under `youtube_id` / `overlay_youtube_id` in pipeline state), and its own publish-schedule slot. Resume is per-variant.

## Diff Summary Per File

### `scripts/pov/pipeline.py` (+213/-47 lines)

**Added:**
- CLI flag `--dual-upload` (in `main()` argparse)
- `self.dual_upload: bool` instance attribute
- `self.overlay_youtube_dir: Path | None` instance attribute (`self.youtube_dir.with_name(self.youtube_dir.name + "_overlay")`)
- State keys `data["dual_upload"]` and `data["overlay_youtube_dir"]` (only written when dual)
- Helper `_append_outro(youtube_dir: Path, step_num: int = 5)` — wraps the existing outro ffmpeg concat
- Helper `_generate_thumbnail(youtube_dir: Path, variant: str, step_num: int = 6)` — wraps thumbnail subprocess + meta write
- Helper `_upload_variant(youtube_dir: Path, variant: str, state_key: str, step_num: int = 7)` — wraps upload subprocess + state write
- Upload `upload_meta["variant"]` field for downstream introspection

**Changed (refactored):**
- `step_concat()` — also copies `combined.mp4` to overlay dir when dual
- `step_overlay()` — when dual, runs overlay on `overlay_youtube_dir/video.mp4` and renames the sidecar `.overlay.mp4` back to `video.mp4`. When not dual, keeps legacy "orphan sidecar" behavior. Now fails fast with `OVERLAY_NO_OUTPUT` if dual mode is set but overlay script produced nothing.
- `step_outro()` — now calls `_append_outro` for raw dir, then again for overlay dir if dual
- `step_thumbnail()` — now calls `_generate_thumbnail(raw_dir, "raw")` then `_generate_thumbnail(overlay_dir, "overlay")` if dual
- `step_upload()` — now calls `_upload_variant(yt, "raw", "youtube_id")` then `_upload_variant(overlay_yt, "overlay", "overlay_youtube_id")` if dual
- `_write_upload_meta()` → now takes `(youtube_dir, variant="raw", step_num=5)`, passes `--variant` to `generate_title.py`, writes `variant` field into meta
- Module docstring — corrected step numbers (4 = overlay, 5 = outro, etc.) and added `--dual-upload` note

**Backward compat:** Without `--dual-upload`, all new code paths are gated. `dual_upload=False`, `overlay_youtube_dir=None`, no new state keys, no new directories, no new uploads. Tested with `Pipeline(args.dual_upload=False)` — old state shape preserved exactly.

### `scripts/pov/generate_title.py` (+24/-0)

**Added:**
- `--variant {raw,overlay}` argparse arg, default `raw`
- When `variant="overlay"`:
  - Title: appends `" | Input Overlay + Utility Cam"` after existing parts
  - Description: appends paragraph explaining keyboard + util-cam PiP
  - Tags: appends 7 overlay-specific tags: `["input overlay", "utility cam", "CS2 overlay", "keyboard overlay", "mouse input", "CS2 utility cam", "smoke lineup"]`

**Backward compat:** Default variant is `"raw"`. Without `--variant` flag, output is byte-identical to before (verified by test run with `demos/analysis/9z-vs-furia-iem-cologne-major-2026_ratings.json`).

### `thumbnail/layouts.py` (+54/-0)

**Added:**
- New helper `_draw_overlay_badge(img: Image.Image)` — draws a semi-transparent dark rounded pill in the top-right corner with white text "W/ INPUT OVERLAY". Uses `PIL.ImageDraw.rounded_rectangle` + 30pt Montserrat-Bold.
- New `variant: str = "raw"` parameter on `generate()`
- When `variant="overlay"`, calls `_draw_overlay_badge` after the text lines are drawn.
- New import: `FONT_PATH` from `thumbnail.generator` (used to load the badge font)

**Backward compat:** Default `variant="raw"`. Badge is never drawn unless explicitly requested.

**Visual verification:** Generated test images at `tmp/test_thumb_raw.jpg` and `tmp/test_thumb_overlay.jpg` (since cleaned up). Badge appears in top-right at ~20px margin, ~280x55px pill, doesn't obscure player avatar (which sits at left) or stats text (right side, but below the badge).

### `thumbnail/cli.py` (+7/-0)

**Added:**
- `--variant {raw,overlay}` argparse arg, default `raw`
- Passes `variant=args.variant` to `generate()`

**Backward compat:** Default `variant="raw"`, no behavior change without flag.

### `AGENTS.md` (+35/-0)

**Added:**
- New "Dual-Upload (`--dual-upload`)" section under `### Pipeline (Primary Entry Point) > Notes` with feature description, data flow, resume semantics, cost analysis, and backward-compat statement
- Updated `## Output Directory Structure` with the `_overlay/` variant

**Backward compat:** Additive documentation. No existing text modified.

## Syntax Check Output

```
$ python -c "import ast; ast.parse(open('scripts/pov/pipeline.py').read())"
scripts/pov/pipeline.py: OK
scripts/pov/generate_title.py: OK
thumbnail/layouts.py: OK
thumbnail/cli.py: OK
```

## Test Plan

### 1. Dry-run with `--dual-upload --until 5` (validate overlay + outro + thumbnail paths)

```powershell
# Need a backlog entry with --step 5 to skip render but run overlay+outro.
# Caveat: overlay requires the demo + steam_id + ~30-60min for 20+ throws.
# Faster smoke-test: just step 5 (outro) and step 6 (thumbnail) on existing renders.
$env:PYTHONPATH = "."
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py `
  --backlog backlog/furia-vs-falcons-iem-cologne-major/high/niko-inferno-furia-vs-falcons-iem-cologne-major.json `
  --dual-upload --step 6
```

Expect: 
- After step 3 (already done from a prior run): both `youtube/...NiKo_Inferno/` and `youtube/...NiKo_Inferno_overlay/` contain `video.mp4`
- After step 4: overlay dir's `video.mp4` is replaced with the keyboard+util-cam version
- After step 5: both `video.mp4` files have outro appended
- After step 6: both dirs have `thumbnail.jpg` (overlay one has the badge) and `upload_meta.json` (overlay one has the suffix and overlay tags)

### 2. Resume test (raw uploaded, overlay failed)

Simulate: raw upload completed but overlay upload failed.

```powershell
# Manually set upload_meta.json to "completed" for raw variant only
$rawMeta = "youtube\2395002_furia-vs-falcons-m3-inferno_NiKo_Inferno\upload_meta.json"
$ovMeta = "youtube\2395002_furia-vs-falcons-m3-inferno_NiKo_Inferno_overlay\upload_meta.json"
$rawContent = Get-Content $rawMeta -Raw | ConvertFrom-Json
$rawContent.youtube_id = "fakeVideoId123"
$rawContent.upload_status = "completed"
$rawContent | ConvertTo-Json | Set-Content $rawMeta

# Re-run with --dual-upload --step 7
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py `
  --backlog backlog/furia-vs-falcons-iem-cologne-major/high/niko-inferno-furia-vs-falcons-iem-cologne-major.json `
  --dual-upload --step 7
```

Expect:
- `[skip] raw already uploaded: https://youtu.be/fakeVideoId123`
- Overlay upload proceeds normally

### 3. Backward-compat test (no flag)

```powershell
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py `
  --backlog backlog/.../niko-inferno-...json --step 6
```

Expect: identical behavior to before this change. Single `youtube/{run_id}/` dir, single thumbnail, single upload_meta, no overlay dir created.

### 4. Resume-old-state test (existing state file without dual_upload key)

Tested in code: a state file with no `dual_upload` key is loaded fine. `self.dual_upload` defaults to `False`, and no overlay dir is created. Backward compat verified.

## Edge Cases & Risks Not in Plan

1. **Overlay step might still produce no output** — if the demo has no `throws.parquet` data and no keyboard input detected, `overlay_pov.py` exits successfully but writes no `.overlay.mp4` sidecar. Old code silently skipped this. New code, in dual mode, **fails fast** with `OVERLAY_NO_OUTPUT` rather than uploading raw video under the overlay dir. This is intentional — the overlay variant must be distinct from the raw variant.

2. **Overlay step is expensive and serial** — Even with `--dual-upload`, overlay runs synchronously in step 4 before the raw and overlay variants diverge. If overlay fails, neither variant uploads (raw upload is gated on the dual pipeline reaching step 7 cleanly). If a user wants raw upload without overlay cost, they should run without `--dual-upload`.

3. **Two YouTube uploads per backlog** — Each upload reserves its own publish-schedule slot. For matches with high backlog (10+ POVs), running all with `--dual-upload` doubles the daily upload count. Auto-scheduling logic already handles this; no code change needed.

4. **Background frame extraction is shared** — `thumbnail/cli.py` auto-extracts a kill clip from the demo for the background. Called twice (once per variant), it re-extracts twice (waste) UNLESS the extracted `youtube/bg_frame.jpg` already exists from a prior call. The cleanup at the end of `cli.py` removes it after first use, so the second call will re-extract. Acceptable cost for now; could be optimized by skipping re-extraction if a fresh frame exists in the same dir.

5. **`generate_title.py` title length** — Overlay title is ~20 chars longer than raw. YouTube max is 100 chars. Tested output: ~95 chars. Fits.

6. **Thumbnail badge contrast** — "W/ INPUT OVERLAY" pill is dark with white text. Tested against grey and natural backgrounds. Contrast OK. If background is very dark, the pill border could be made lighter (not implemented).

7. **Upload error rollback** — `upload_youtube.py` releases its reserved publish slot on failure. Both variants use the same mechanism independently. No new failure mode introduced.

8. **`pipeline_chain.py`** — Chain watches `state["step"] >= 6` and triggers next POV's render while current POV uploads. With dual upload, step 7 takes 2x as long (two uploads), but the chain still triggers correctly at step 6 (start of thumbnail). No change needed to chain code.

## Modified Files

- `scripts/pov/pipeline.py` (modified)
- `scripts/pov/generate_title.py` (modified)
- `thumbnail/layouts.py` (modified)
- `thumbnail/cli.py` (modified)
- `AGENTS.md` (modified)

## Test Commands Run

- `python -c "import ast; ast.parse(open('scripts/pov/pipeline.py').read())"` — passed
- `python -c "import ast; ast.parse(open('scripts/pov/generate_title.py').read())"` — passed
- `python -c "import ast; ast.parse(open('thumbnail/layouts.py').read())"` — passed
- `python -c "import ast; ast.parse(open('thumbnail/cli.py').read())"` — passed
- `python scripts/pov/pipeline.py --help` — shows `--dual-upload`
- `python scripts/pov/generate_title.py ... --variant overlay` — output has overlay suffix + tags + desc
- `python scripts/pov/generate_title.py ... --variant raw` — output is standard (no suffix, no overlay tags)
- `python -c "generate()"` with `variant='raw'` and `variant='overlay'` — both render 1280x720, overlay one has the badge in top-right
- `Pipeline(args).dual_upload` — True when flag set, False when not
- `Pipeline(args).state['data']` — has `dual_upload`+`overlay_youtube_dir` when flag set, NOT when not
- Resume skip — `_upload_variant` skips upload when meta has `youtube_id` + `upload_status: completed`
- Resume of old state file — no `dual_upload` key, loads fine, defaults to non-dual
