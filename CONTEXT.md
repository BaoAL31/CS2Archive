# CS2Archive Highlights

Domain language for the edit-heavy highlight reel product (FACEIT-first), distinct from the existing full-match single-player POV archive.

## Language

**Kill**:
An elimination during live rounds where at least one side is a Recognised Pro (attacker and/or victim). Includes pro frags, unknown→pro picks, and bomb kills that meet that rule; excludes assists, warmup, suicides/world-only deaths, team kills, and kills with no Recognised Pro involved.
_Avoid_: Frag (synonym OK in casual speech; prefer Kill in the model), elimination, assist

**Action Timeline**:
The ordered sequence of every significant action from map start to map end — kills, bomb events (plant/defuse/explode), utility usage (flash/he/smoke detonations), and flash assists. v1 deliverable is this sequence as data (not video). Not filtered to one player. Written to `{Highlights Run Dir}/action_timeline.json`.
_Avoid_: kill timeline (superseded), highlight reel (the edited video product), POV (the full-match single-player product), timeline visual / scrubber (later), frag leader

**Kill**:
An entry in the Action Timeline's `kills` array. At least one of attacker or victim is a Recognised Pro. Indexed 0-based across the full match for cross-referencing with Edit Segments.
_Avoid_: frag, elimination

**Multi-Kill Streak**:
A contiguous run of Kills by the same Recognised Pro as attacker. One streak is the atomic section that gets a single POV assignment for rendering. Boundary rules (what breaks contiguity, minimum kill count, padding) are decided separately.
_Avoid_: highlight, clip, segment (vague), ace (special case of a streak), multi-kill (game UI label; prefer this term)

**Recognised Pro**:
A player recorded in `.data/player_accounts.json`. Matched in demos by Steam ID. Single identity store — no separate `faceit_pros.json`.
_Avoid_: frag leader, POV player, featured player, watchlist, track_faceit

**Player Account**:
One Recognised Pro record: canonical nickname, Steam/FACEIT identity, and POV capture settings.
_Avoid_: faceit_pros entry

**Highlight Reel**:
The edited video assembled from selected moments (future Kinocut output). Out of scope for v1; branch `feat/kinocut-highlights` holds the highlights product work. Own pipeline, separate from POV Archive.
_Avoid_: POV, highlights (ambiguous — use Kill Timeline or Highlight Reel)

**Highlights Run Dir**:
`renders/hl-{demo_stem}/` — working directory for one Highlight Reel pipeline run. Sibling pattern to `renders/pov-*`. v1 writes `action_timeline.json` here.
_Avoid_: youtube/, demos/analysis/, backlog/, nested `renders/highlights/`

**Action Timeline Builder**:
The FACEIT-only entry that builds an Action Timeline from a demo as data via demoparser2 events (kills, bomb events, utility, round lifecycle). Standalone — `scripts/highlights/build_action_timeline.py`. Hard-refuses demos outside `demos/faceit/`. Feeds the future highlights pipeline; does not run POV Archive.
_Avoid_: backlog entry (POV packaging), POV pipeline step, csdm analyze as the action source

**POV Archive**:
The existing product: one player's full-match rounds rendered end-to-end. Product behavior unchanged; HLTV stays on this path. Scripts live under `scripts/pov/` (and FACEIT POV helpers under `scripts/faceit/`).
_Avoid_: calling the Highlight Reel a POV

---

## Shorts (9:16 Vertical Clips)

**Short**:
A 9:16 vertical video clip suitable for YouTube Shorts. One of two types: 4K/5K (4+ kills by same attacker in a single round) or Clutch (round win from a 2v4 or worse man-disadvantage). Rendered from a single POV with a blurred-mirror header/footer composited above/below the game footage. Output is 1080×1920.
_Avoid_: highlight reel clip, POV clip, highlight

**Short Type**:
Exactly `4k` (4+ kills by same attacker, inclusive of aces) or `clutch` (team wins the round from 2v4, 2v5, 1v3, 1v4, or 1v5 — i.e. outnumbered by 2+). No other types exist.
_Avoid_: multikill (ambiguous — 4K is the canonical label), 1v2 clutch, 2v3 clutch

**Short Timeline**:
The ordered list of Short-worthy moments from a demo, produced by `build_short_timeline.py`. Written to `shorts/short_timeline.json`. Each entry records the Short Type, POV steam_id, start_tick, end_tick, and supporting data (kill tick list for 4K, round win event for Clutch). Uses demoparser2 — same parsing engine as highlights.
_Avoid_: action timeline, kill timeline, highlight list

**Short Span**The tick range rendered for a Short:
- **4K/5K**: first kill tick → last kill tick (tight, no padding)
- **Clutch**: the moment the team's active alive count drops to 2v4 or worse → round win event (bomb defuse/explosion or team win end, not dependent on kills — a zero-kill defuse clutch is valid)
_Avoid_: clip window, render range

**Footage Ratio**The proportion of the 1080×1920 vertical frame occupied by actual game footage, expressed as N/16. Example: `--footage-ratio 10` produces 10/16 height footage, 3/16 header blur, 3/16 footer blur. Remaining space is always split evenly between header and footer.
_Avoid_: crop ratio, aspect split

**Edge Mirror Blur**The visual effect for Short header/footer: the top N/16 slice of the game frame is copied, scaled, blurred heavily, and placed above the footage. Same for the bottom slice below. Header and footer are mirror images of the nearest edge, but composited outside the game area (they extend the frame visually, not tap into reserved video pixels).
_Avoid_: letterbox, pillarbox, blurred border

**Short Render Pipeline**Two scripts, always run sequentially:
1. `build_short_timeline.py` - parses the demo with demoparser2, finds 4K/5K + Clutch moments, writes `short_timeline.json`.
2. `render_shorts.py` - reads `short_timeline.json`, renders each Short via CSDM config-file mode (same tick-range approach as `render_edit_timeline.py`), composites to 9:16 with edge mirror blur and output as `shorts/short_NNN.mp4`.

**Short Run Dir**:
Shorts are co-located with their parent pipeline's render directory:
- **HLTV demos** -> `renders/pov-{demo_stem}_{player}/shorts/` (because the player is known from the POV backlog)
- **FACEIT demos** -> `renders/hl-{demo_stem}/shorts/` (sibling to the highlights run dir)

The `shorts/` subfolder contains `short_timeline.json` and all rendered `short_NNN.mp4` files.
_Avoid_: `renders/shorts-{demo_stem}/` (no separate top-level `shorts-` dir)

**Short Timeline Builder**The script `scripts/shorts/build_short_timeline.py` that uses demoparser2 to extract kill events, bomb events, and round lifetime data, then identifies 4K/5K and Clutch moments. Accepts any demo path (not FACEIT-gated like the highlights Action Timeline Builder). By default keeps **only shorts whose POV player is a Recognised Pro** (`.data/player_accounts.json`); randos are dropped (`--include-all-players` opts out). Surviving shorts get their `pov_nick` rewritten to the canonical nickname. Writes `short_timeline.json` to the correct colocated output dir based on demo location.
_Avoid_: build_short_timeline.py (different name), extract shorts (this IS the extract)

**Short Renderer**:
The script `scripts/shorts/render_shorts.py` that reads `short_timeline.json`, renders each Short via CSVD config-file mode (tick-range sequences, same encoding: 2560x1440 source / h264_nvenc / CQ 14), then post-processes to 9:16 vertical with the edge mirror blur composite. Accepts `--footage-ratio N` for the footage proportion and `--batches N` for segment grouping. Outputs `short_NNN.mp4` files.
_Avoid_: render_shorts (typo), compiles (not a compile step)

---

## Invariants

- A Short is always exactly one POV per render — no mid-sequence POV switching within a single short.
- 4K/5K type includes 5-kill aces. 5K is a special case of 4K.
- Clutch round win is determined by pre-round-life update events (start of round live count) versus in-round death tracking + bomb state change—not by post-round replay ghosting.
- Blurred mask for header/footer is dynamic: per-frame, the top and bottom edge of the frame are sampled, mirrored, and blurred in the composite.
- Short Timeline is a Write-clause output: rendering never re-computes the timeline. If you want different criteria, re-run `build_short_timeline.py`.

---

## High Shorts v1 — Edit Timeline

**Edit Timeline**:
The structured plan assigning a POV (player perspective) to each editorial segment of a match. Output of v1 Highlights pipeline. Written to `{Highlights Run Dir}/edit_timeline.json`. Contains ordered segments with start_tick, end_tick, pov_steam_id, segment_type, rationale, and kill_indices referencing the Action Timeline. Not a video — a machine-readable edit decision list for downstream rendering (Kinocut).
_Avoid_: edit decision list (EDL - legacy term), cut list, timeline (ambiguous with Action Timeline), script (implies narration)

**Edit Segment**:
One tick range assigned a single POV (typically one attacker's Multi-Kill Streak). Typed as `multi_kill`, `entry`, `clutch`, `trade`, `utility`, or `default`. Multiple segments may share a round and their tick ranges MAY overlap (POV handoff). Must not cross into the next round. Starts at or after round_start + 24s buy on live rounds (round >= 1). Warmup/knife round 0 starts at round_start (no buy skip). Minimum one kill per segment (referenced by kill_indices).
_Avoid_: clip, scene, cut, chunk (ambiguous with render output)

**Segment Type**:
Editorial classification of a segment:
- `multi_kill` — 2+ kills by same attacker in quick succession
- `entry` — first kill of round (entry frag)
- `clutch` — 1vX situation won by solo player
- `trade` — immediate trade kill by teammate
- `utility` — segment defined by utility usage (smoke, flash, molly play)
- `default` — fallback when no other type fits
_Avoid_: highlight type, clip type

**POV Assignment**:
The decision of which player's perspective renders a segment. In v1: attacker POV for their kills; victim POV for clutches/trades; utility player for utility segments. POV steam_id must exist in demo (any player, not just Recognised Pros).
_Avoid_: camera angle, perspective (vague), featured player

**Edit Timeline Builder**:
LLM-driven script (`scripts/highlights/build_edit_timeline.py`) that reads Action Timeline + player list, prompts an LLM for segment assignments, validates output, writes `edit_timeline.json`. FACEIT-only (reads Action Timeline from `demos/faceit/`). Single-shot JSON mode with schema validation and retry. A deterministic **fix pass** always normalizes LLM output (round/attacker splits, buy-time floors, overlap handoffs, multi-kill windows, optional kill omissions) — see ADR 0003.
_Avoid_: EDL generator, cut planner, AI editor

**Kill Index Reference**:
Each segment's `kill_indices` array indexes into Action Timeline's `kills` array (0-based). Allows cross-referencing segments to source kills for validation and rendering.
_Avoid_: kill refs, kill pointers

**Highlights Run Dir** (extended):
`renders/hl-{demo_stem}/` now also contains `edit_timeline.json` alongside `action_timeline.json`. Sibling to `renders/pov-*`.
