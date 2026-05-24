# CS2Archive — Domain Glossary

## Core Concepts

- **Match**: A CS2 pro match between two teams on HLTV or FACEIT. Has a URL, team names, maps, and a unique slug.
- **POV** (Point of View): A specific (match, player, map) tuple. The unit a thumbnail/video is generated for.
- **Render Folder**: Working directory for POV video renders: `demos/renders/pov-{demo-stem}_{player}/`. Scoped per POV (not per demo) so multiple players on the same `.dem` never share `combined.mp4`.
- **Ratings File**: JSON file in `demos/analysis/{match-slug}_ratings.json` containing per-map player stats from HLTV Rating 3.0.
- **Avatar**: HLTV full-body player photo stored in `demos/avatars/{nickname}.png`. Background is removed with `rembg` during download (step 4), not at thumbnail time.
- **Background Frame**: A single frame extracted from a csdm-rendered kill clip, blurred (radius 6) and used as the thumbnail background.

## Thumbnail Generator

- **Thumbnail**: 1280×720 PNG image composited from a game frame (blurred) + player cutout + text overlay.
- **Layout**: Player cutout on the left (90% height), 5-6 line text block middle-aligned on the right (player name, K-D, rating, map, match, tournament).
- **Output Structure**: `youtube/{match-slug}_{player}_{map}/thumbnail.png`

## Backlog & Resume

- **Backlog Entry**: A handoff markdown file in `backlog/{priority}/{slug}.md` describing one POV to produce.
- **Backlog Priority**: Entries are organized into `high/` (rating >=1.5), `medium/` (rating 1.0-1.49), or `low/` (rating <1.0).
- **Progress File**: A thin JSON index at `backlog/{slug}.progress.json` pointing at a POV's `run_id`, next pipeline step, render resume round, and human-readable status. Agents read this to resume without retyping CLI flags.
- **Pipeline State File**: Authoritative machine state at `.pipeline/{run_id}.json` (paths, round count, youtube dir). The pipeline reads/writes this; the Progress File does not duplicate it.
- **Acquisition** (pipeline step 1): Browser-based download of the match GOTV archive from the HLTV match URL, then unpack and map selection. Replaces manual "download RAR in browser, drop into `demos/`".
- **Demo path override**: Optional `--demo` on the pipeline CLI. When omitted, step 1 acquires from the HLTV URL and selects the `.dem` for `--map`. When set to an existing `.dem` or `.rar`, step 1 skips download and uses that file (extract + map pick only for `.rar`).
- **HLTV profile**: Persistent browser session directory reused across acquisition runs so HLTV cookies and session stay warm between downloads.
- **Match demo folder**: Directory for one HLTV match's acquired archive and extracted `.dem` files: `demos/hltv/<match-slug>/`. Multiple POVs on different maps from the same match share this folder; acquisition skips re-download when the archive or target map `.dem` is already present.
- **Acquisition browser mode**: Default is a visible browser with human-like input (best chance on HLTV). Optional headless mode for unattended runs once downloads are trusted.
- **HLTV acquisition entry points**: Pipeline step 1 and `hltv match` both use the same acquisition behavior (download, extract, map pick vs download-only).
- **Acquisition force**: `--force` on pipeline or `hltv match` re-downloads the archive even when one already exists. Undersized archives are treated as failed downloads, not as cache hits.

## Pipeline Steps

Steps run in order. Resume uses the Pipeline State File; the Progress File links a Backlog Entry to that state.

| # | Name | Description |
|---|------|-------------|
| 1 | acquire | Download match demo archive from HLTV (if not already local), extract it, select the `.dem` for the target map |
| 2 | ratings | Scrape HLTV Rating 3.0 |
| 3 | steam_id | Save player Steam64 to player list |
| 4 | avatar | Download player cutout PNG from HLTV |
| 5 | analyze | csdm analyze the demo |
| 6 | render | Render all rounds as POV clips |
| 7 | concat | Copy combined.mp4 → youtube/video.mp4 |
| 8 | outro | Generate 5s silent outro (Pillow + Montserrat, top-half text), concat onto video.mp4 |
| 9 | thumbnail | Generate 1280×720 thumbnail.png |
| 10 | upload | Upload video.mp4 + thumbnail.png to YouTube |
| 11 | cleanup | Remove renders folder + pipeline state |

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