# Silent fallbacks are banned on the render path

**Policy (2026-11-07):** on the path that produces delivered pixels, an
unresolved value must **raise**, not fall back to a plausible default.

Why this exists: dem0n (FUT) plays **1280x960 4:3 Stretched**, but prosettings
has no *list* entry for him, so `resolve_video_settings` returned its
1920x1080 / 16:9 Native default, the card was written with those values, and a
full POV render started at the **wrong aspect**. Nothing errored. It was caught
only because someone asked "does dem0n actually play 16:9?".

A wrong-aspect POV costs a multi-hour render *and* ships a video that looks
wrong — strictly worse than failing.

## Converted to hard failures

| Site | Old behaviour | Now |
|---|---|---|
| `scrapers/prosettings.resolve_video_settings` | 1920x1080 Native default for unknown players | `VideoSettingsUnresolved`; tries the player's own prosettings page first |
| `scrapers/prosettings.scrape_player_video_settings` | page-wide label map (last value wins → monitor's 1920x1080) | Video Settings section wins, **first** value per label |
| `pov/render_pov` viewmodel lookup | `[WARN]` + empty settings → game-default viewmodel/crosshair | `RENDER_VIDEO_SETTINGS_UNRESOLVED`, exit 1 |
| `pov/render_pov` capture aspect | `capture_size_for_player(...) or capture_size_for_aspect("")` → 16:9 | `RENDER_CAPTURE_ASPECT_UNKNOWN`, exit 1 (pass `--width/--height` to override) |
| `pov/render_pov` demo-name map | `[WARN]`, renders with raw/absent player name | `RENDER_DEMO_NAMES_UNAVAILABLE`, exit 1 |
| `pov/render_pov` spec_lock.cfg write | `[WARN]`, director may cut away from the POV | `RENDER_SPEC_LOCK_WRITE_FAILED`, exit 1 |
| `pov/pipeline._ensure_video_settings` | `[WARN] video settings lookup failed` | `VIDEO_SETTINGS_UNRESOLVED` / `VIDEO_SETTINGS_LOOKUP_FAILED`, exit 1 |
| `pov/pipeline.step_render` HLAE gate | gate ran before `ensure_hlae_for_cs2` → stale-settings hard fail | adapts HLAE first, then gates (see `test_pipeline_hlae_preflight_order.py`) |
| `overlay/overlay_pov` tick→frame mapping | `offset_sec * TICKRATE` and linear-tick guesses → key presses on wrong frames | `[ERROR]` + exit 1; ask for `--no-keyboard` if the mapping is genuinely unavailable |
| `pov/create_backlog` card write | card written with default aspect | `[ERR]` + `sys.exit(1)`, no card |

## Deliberate non-failures (kept, for the record)

These degrade a *nicety*, not the delivered pixels, and failing would make the
pipeline brittle for no gain:

- `concat_rounds._gpu_free_for_scale`: unknown `nvidia-smi` answer → optimistic.
  A GPU problem still fails loudly at encode; there is no CPU fallback any more.
- Thumbnail kill-frame extraction (`_extract_kill_frame`, overlay frame grab):
  falls back to another frame source, and the thumbnail still renders.
- Avatar fetch, playlist naming, Bilibili mirroring, post-upload render purge:
  cosmetic/operational, never changes the video.
- Hook selection: no qualifying moment is a documented *normal skip* (the video
  ships without a cold open) — that is a product decision, not a fallback.

## How to add a fallback (checklist)

1. Does it change the pixels, the timing, or the identity of the player? → it
   must raise, with a `*_UNRESOLVED` / `*_UNAVAILABLE`-style code printed as
   `[PIPELINE_ERROR] {...}` and `sys.exit(1)`.
2. Is it cosmetic/operational? → allowed, but say why in a comment.
3. Always add a test that asserts the failure (see
   `tests/test_no_silent_fallbacks.py`).
