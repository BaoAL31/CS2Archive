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

## Highlights v1 — Edit Timeline

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
