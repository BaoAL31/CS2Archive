# Shorts Pipeline: Colocated Render Directories

The Shorts pipeline produces 9:16 vertical clips from the same demos as POV Archive and Highlight Reel, but as a standalone extract-extract-render pipeline (not embedded in either parent pipeline). Where and how the outputs are structured matters because the upload pipeline (`upload_pending.py`) needs to discover them later, and because the render paths overlap with the parent pipeline's CSDM config-file rendering.

**Decision: Shorts use a dedicated namespace under `renders/shorts/`.** Each demo
gets `renders/shorts/shorts-{demo_stem}/`, with per-short folders beneath it:
`renders/shorts/shorts-{demo_stem}/shorts-{slug}/`.

## Why

- **Discoverability:** All Shorts live under one dedicated render namespace, keeping the main `renders/` directory from filling with per-short folders.
- **Upload alignment:** `upload_pending.py` already scans subdirectories under `youtube/`; `shorts/` is just a sibling concept under `renders/`. Uploaders can later scan `renders/*/shorts/` for Short output files.
- **No confusion with sibling pipelines:** The `shorts/` namespace clearly separates vertical Shorts artifacts from POV and Highlight Reel renders.

## Considered Options

**Option A: Dedicated `renders/shorts/shorts-{demo_stem}/`** — Clean and isolated, while retaining the demo stem in the path. Chosen.

**Option B: Parent pipeline subdirectories** — Rejected because Shorts polluted unrelated POV/Highlight render directories.

## Consequences

- `build_short_timeline.py` writes to the shared Shorts namespace regardless of HLTV or FACEIT source.
- Existing legacy `renders/hl-*/shorts-*` artifacts are not moved automatically.