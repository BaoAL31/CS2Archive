# CS2Archive — Domain Glossary

## Core Concepts

- **Match**: A CS2 pro match between two teams on HLTV or FACEIT. Has a URL, team names, maps, and a unique slug.
- **POV** (Point of View): A specific (match, player, map) tuple. The unit a thumbnail/video is generated for.
- **Render Folder**: Working directory for POV video renders: `demos/renders/pov-{demo-stem}_{player}/`. Scoped per POV (not per demo) so multiple players on the same `.dem` never share `combined.mp4`.
- **Render Batch**: Consecutive demo rounds rendered in a single csdm invocation. Default size is **10** (`--batches N`). On failure mid-batch, the whole batch is re-rendered on resume.
- **Batch Artifact**: The single MP4 csdm writes for one render batch, named `batch-{start:03d}-{end:03d}.mp4` using **global** round numbers (continuous across split demo parts p1/p2). Render skips a batch only when that **exact** filename exists and is **≥ 1 MB** (no minimum duration; some rounds are very short). Concat requires `batch-*.mp4` ranges to be contiguous, non-overlapping, and cover rounds 1..N; otherwise it fails with a clear error (no silent overlap concat).
- **Concat input**: Step 3 merges **batch artifacts** only (`batch-*.mp4`), in global round order, deleting each after a successful append. No `round-*.mp4` path in pipeline code; `--batches 1` is equivalent to the former one-round-per-file model (`batch-007-007.mp4`).
- **Legacy migration**: Existing `round-NNN.mp4` files are renamed once to `batch-NNN-NNN.mp4` (same global index) before using batch concat; no re-render.
- **Batches flag**: `--batches N` on `render_pov.py` and `pipeline.py` (default 10). Pipeline forwards the flag to render step 2.
- **Ratings File**: JSON file in `demos/analysis/{match-slug}_ratings.json` containing per-map player stats from HLTV Rating 3.0.
- **Avatar**: HLTV full-body player photo stored in `demos/avatars/{nickname}.png`. Background removed with `rembg` during avatar download (not at thumbnail time).
- **Background Frame**: A single frame extracted from a csdm-rendered kill clip, blurred (radius 6) and used as the thumbnail background.

## Thumbnail Generator

- **Thumbnail**: 1280×720 PNG image composited from a game frame (blurred) + player cutout + text overlay.
- **Layout**: Player cutout on the left (90% height), 5-6 line text block middle-aligned on the right (player name, K-D, rating, map, match, tournament).
- **Output Structure**: `youtube/{match-slug}_{player}_{map}/thumbnail.png`

## Backlog & Acquisition

- **Backlog Entry**: A JSON file in `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` describing one POV to produce. Created by `create_backlog.py`.
- **Backlog Priority**: Entries organized into `high/` (rating >=1.5), `medium/` (rating 1.0-1.49), or `low/` (rating <1.0).
- **Pipeline State File**: State at `.pipeline/{run_id}.json` (paths, round count, youtube dir). Pipeline reads/writes this for resume.
- **Acquisition** (create_backlog.py): Downloads match archive from HLTV URL, extracts demos, scrapes HLTV Rating 3.0, fetches player avatars, resolves Steam IDs, and writes backlog entries — all-in-one.
- **HF auto-download**: If local `.dem` is missing and backlog has `hf_root`, pipeline downloads single `.dem` from `cs2povarchive/cs2-demos` dataset (not the full archive). Only the needed map's demo is pulled.
- **Match demo folder**: `demos/hltv/<match-slug>/`. Multiple POVs on different maps from the same match share this folder.
- **HLTV profile**: Persistent `.cloak-hltv-profile/` for CloakBrowser (download archives). CDP temp profile (port 9222) for page scraping (`fetch_hltv_page_html`, `HLTVScraper`).
- **Acquisition force**: `--force` re-downloads the archive even when one exists. Undersized archives (<1MB) are not treated as cache hits.

## Pipeline Steps

Steps 1-7 run in order. Resume uses Pipeline State File (`.pipeline/{run_id}.json`).

| # | Name | Description |
|---|------|-------------|
| 1 | analyze | csdm analyze the demo; export JSON for rounds/kills |
| 2 | render | Render POV in batches via csdm + HLAE; checkpoint = `batch-*.mp4` |
| 3 | concat | Merge batch artifacts into `combined.mp4`, copy to youtube/ |
| 4 | outro | Generate 5s silent outro, concat onto video.mp4 |
| 5 | thumbnail | Generate 1280×720 thumbnail.png |
| 6 | upload | Upload video.mp4 + thumbnail to YouTube |
| 7 | cleanup | Remove renders folder + pipeline state |

Acquisition side handled by `scripts/create_backlog.py` (not part of pipeline steps).

## Output Directory

- `youtube/`: Root folder for all YouTube-related content (thumbnails, video files).

## Wikify

- **Wiki Output Structure**: Multi-file Markdown wiki with `index.md` entry point, per-article `.md` files, and `category/` index pages (Obsidian-vault style, not monolithic single-file).
- **PDF Handling**: Pre-extract `.pdf` → `.md` before dispatching subagents.
- **LaTeX Handling**: Pre-extract `.tex` → `.md` (strip commands, keep structure + math) before dispatching subagents.
- **Extraction Source**: Wikify has its own extraction pipeline (independent of graphify).
- **Category Taxonomy**: 5 categories — `method` (techniques, algorithms, baselines), `concept` (domain terms, findings, math), `paper` (citations, references), `dataset`, `architecture` (system patterns, decisions, APIs).
- **Article ID Strategy**: Path-scoped `id` (deterministic, collision-free) + `canonical` group key (exact string match). Dedup by `id`, then consolidate by `canonical` in post-merge. `related_to` uses `canonical` keys, not `id`s.
- **Article Status**: Every article has `status: "confirmed" | "ambiguous"`. Ambiguous = concept mentioned but not sufficiently explained in source.