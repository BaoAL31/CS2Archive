# CS2Archive — Domain Glossary

## Core Concepts

- **Match**: A CS2 pro match between two teams on HLTV or FACEIT. Has a URL, team names, maps, and a unique slug.
- **POV** (Point of View): A specific (match, player, map) tuple. The unit a thumbnail/video is generated for.
- **Ratings File**: JSON file in `demos/analysis/{match-slug}_ratings.json` containing per-map player stats from HLTV Rating 3.0.
- **Avatar**: HLTV full-body player photo stored in `demos/avatars/{nickname}.png`. Background is removed with `rembg` during download (step 4), not at thumbnail time.
- **Background Frame**: A single frame extracted from a csdm-rendered kill clip, blurred (radius 6) and used as the thumbnail background.

## Thumbnail Generator

- **Thumbnail**: 1280×720 PNG image composited from a game frame (blurred) + player cutout + text overlay.
- **Layout**: Player cutout on the left (90% height), 5-6 line text block middle-aligned on the right (player name, K-D, rating, map, match, tournament).
- **Output Structure**: `youtube/{match-slug}_{player}_{map}/thumbnail.png`

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
