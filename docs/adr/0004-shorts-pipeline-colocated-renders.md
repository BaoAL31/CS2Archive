# Shorts Pipeline: Colocated Render Directories

The Shorts pipeline produces 9:16 vertical clips from the same demos as POV Archive and Highlight Reel, but as a standalone extract-extract-render pipeline (not embedded in either parent pipeline). Where and how the outputs are structured matters because the upload pipeline (`upload_pending.py`) needs to discover them later, and because the render paths overlap with the parent pipeline's CSDM config-file rendering.

**Decision: Shorts are colocated inside the parent pipeline's render directory under a `shorts/` subfolder.** For HLTV demos, output goes to `renders/pov-{demo_stem}_{player}/shorts/`. For FACEIT demos, output goes to `renders/hl-{demo_stem}/shorts/`. There is no separate top-level `renders/shorts-{demo_stem}/` directory.

## Why

- **Discoverability:** A viewer can look at one render directory and see all artifacts for that match/player (full POV video + shorts). No separate directory to track.
- **Upload alignment:** `upload_pending.py` already scans subdirectories under `youtube/`; `shorts/` is just a sibling concept under `renders/`. Uploaders can later scan `renders/*/shorts/` for Short output files.
- **No confusion with sibling pipelines:** `renders/shorts-*` would clash with the convention of `renders/pov-*` and `renders/hl-*` — directories that represent renders of a specific match/producer pipeline.

## Considered Options

**Option A: Dedicated `renders/shorts-{demo_stem}/`** — Clean, isolated, but loses coupling to the parent pipeline. You would need to reconstruct which demo came from which pipeline manually. Rejected because the parent run directory already carries the context (player identity for HLTV, match identity for FACEIT).

**Option B: Separate `renders/shorts/` subdir with no parent coupling** — Same problems as Option A, plus harder for upload scripts to discover.

## Consequences

- `build_short_timeline.py` must dynamically determine the correct parent directory based on whether the demo lives under `demos/hltv/` or `demos/faceit/` and whether a corresponding `pov-*` or `hl-*` render directory exists.
- If the parent render directory is not found (pipeline not run yet), the script creates it and places the `shorts/` folder inside — even if the POV paint itself hasn't been done yet.
- No collision risk with `pov-*/shorts/` and `hl-*/shorts/` because a demo belongs to exactly one pipeline.