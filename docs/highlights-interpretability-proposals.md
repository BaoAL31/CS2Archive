# Multi-pro / multi-POV cut: interpretability proposals

Council deliverable (single member, worked alone). Scope: increase how well the
downstream edit — and eventually the viewer — understands **story, tactics and
stakes** from `renders/hl-{stem}/action_timeline.json` →
`renders/hl-{stem}/multipov/edit_timeline.json`.

Constraints honoured: everything is computable from demoparser2 events/ticks or
from cheap offline post-processing of the action timeline; deterministic
detectors preferred; render budget (each moment = a CSDM segment render)
respected; every Recognised Pro stays legible.

---

## Proposal 1 — `round_story[]`: a story card per round (HIGHEST LEVERAGE)

**Story question:** "What was the *point* of this round, and why does this
moment inside it matter?"

Today `rounds[]` carries the scoreboard (winner, `win_reason`, running score,
alive progression, match_point/overtime) and `moments[]` carries isolated frags.
Nothing says *what kind of round this was* — a 1v3 retake hold in a 10-2 lead
and a 1v3 retake hold at 11-11 are identical in the timeline. The edit layer
therefore picks on raw frag value and cuts clips with no connective tissue.

**Computation (deterministic, offline over `action_timeline.json`):**
per round derive
- `buy_type` per team: `full_buy | force | eco | semi | save | unknown`
  from **purchased equipment**, not kills. Ladder:
  1. `parse_event("item_equip")` / `item_pickup` joined to entity ids → exact
     per-team spend at freeze-end (`X_EquipmentValue` via `parse_ticks` at the
     round-start tick if demoparser2 exposes it — probe once, cache the answer
     in the timeline header).
  2. Fallback: weapon-class value of the guns the team actually *used and lost*
     that round (`kills_all[].weapon` + `victim_weapon`) → a lossy but
     round-local estimate. Threshold: 4 rifle+ kills ⇒ `full_buy`, else eco.
  3. Else `unknown`. Never fabricate.
- `economy_diff`: |spend_T − spend_CT| (ladder 1/2 only).
- `opening_advantage`: side of the first death, and whether the first death
  happened inside 20s of freeze-end (`fast_blood`).
- `pivot`: the single tick where round control flipped — the kill that took
  alive count from `attacker_team ≥ 3` to `≤ 2` for the eventual winner's
  opponent... concretely: the kill after which the eventual winner's team had
  a majority and never lost it. This is the round's *hinge kill*.
- `outcome_flavor`: derived label string, e.g. `eco_rush_win`,
  `full_buy_comeback`, `retake_hold`, `one_gun_two_players`,
  `bomb_saved`, `fast_blood_eco`.
- `stakes` (ranked): `map_point` > `ot_transition` > `match_point` >
  `half_end` (first half close) > `second_half_start` > `comeback_window`
  (down ≥4 after having led) > `normal`.
- `pivot_pro_sid`: the pro on the winning side of the hinge kill, or `""`.
- `headline`: 1 templated sentence, e.g.
  `"R14 (9-6, T side): eco win — pivot was FalleN's 1v3 hold at 1:12."`
  Template only, no LLM, so it is stable across reruns.

**Schema addition:** new top-level `round_story: [ {round, buy_T, buy_CT,
economy_diff, opening_advantage, fast_blood, pivot_tick, pivot_sid,
outcome_flavor, stakes, stakes_rank, headline} ]`. Bump
`timeline_version` to 3; add `"round_story_version": 1` so caches rebuild
cleanly (same discipline as `INSTA_RULE_VERSION`).

**Also consumed by:** `build_multipov_timeline.py` — a `pivot_sid` moment gets
a value bonus, and a clip whose `stakes_rank >= 3` is never dropped first by
`_fit_budget` (trim lowest value-per-second *among non-stakes* clips first).
That single change makes the cut read as a match rather than a frag reel.

**Cost / failure modes:** one extra `parse_event` pass per demo (seconds),
zero extra render cost — the story rides along in the existing segment window.
Failure modes: missing `item_equip` on PBDEMS2/Source2 (→ ladder 2/3, `unknown`
is honest), bomb-explode-only rounds (no `round_end` winner — already handled by
`_authoritative_winners`), economy estimates skewed when a team never lost a
gun. All fields nullable, downstream must tolerate `null`.

---

## Proposal 2 — `pro_storyline{}`: per-Recognised-Pro arc

**Story question:** "Why is this player in the video, and what did they do?"

Coverage guarantee in the current scheduler forces *a* clip per pro, but picks
the highest-frag-value one, which frequently ends up being a 3K that the viewer
cannot contextualise. A per-pro rollup makes each appearance *earn its place*
and is the cheapest way to satisfy the "every pro stays narratively legible"
constraint.

**Computation (offline, from `kills_all` + `rounds[]` + `round_story`):**
per pro: `kills, deaths, kd, opening_kills, opening_deaths, clutches_won,
clutches_attempted, multi_kills, hs_pct, util_kills, wallbangs, damage_ traded,
opening_impact` (opening kills − opening deaths), `round_wins_with` (rounds
where the pro's team won and the pro had ≥1 kill or ≥1 impact damage) and
`best_moment` (highest-`value` moment id). Then **arc classification** by
comparison against the match mean: `carry`, `punch_up`,
`quiet_anchor` (high KD, low opening impact), `frag_feeder` (opening deaths
dominated), `clutch_anchor`, `support` (util_kill/wallbang carry, low K/D).

**Schema addition:** `pro_storyline: { sid: {nick, kills, deaths, kd,
opening_kill_diff, clutches_won, clutches_attempted, multi_kills, hs_pct,
util_kills, arc, best_moment_id, share_of_pro_kills, headline} }` and a
`headline` per pro, templated ("donk: 31-14, arc=carry, 3 opening kills").

**Cost / failure modes:** pure aggregation, microseconds, no render cost.
Risks: small-sample arcs on a single map (mitigate: only emit `arc` when the
pro has ≥8 kills, else `arc: null`, say so rather than over-claim).

---

## Proposal 3 — `pov_link{}`: causal POV continuity between adjacent clips

**Story question:** "Why are we cutting from this player to that one?"

The scheduler picks non-overlapping moments and the renderer picks a POV per
segment. Nothing ties consecutive segments together, so the cut reads as
arbitrary. A cheap causal link makes the POV switch *itself* the narration.

**Computation:** for each adjacent picked pair (A, B) in
`build_multipov_timeline.py`, inspect A's kills and B's first killer/attacker:
- `direct`: A's primary POV was the *victim* of B's opening kill, or the
  attacker of B's first victim → switch is a **payoff cut** (you watch the
  setup, then the shooter who finished it).
- `trade`: killed A's POV got traded inside B's window.
- `momentum`: same team, same side, consecutive rounds, streak continues.
- `switch`: no causal edge (pro flip / side flip) — flag it so the renderer
  can insert a scoreboard beat rather than pretending continuity.

**Schema addition:** each entry in `picked[]` gains
`link: {kind, from_pov, to_pov, evidence_kill_id, seconds_since_prev}` and
`picked[]` gains a `needs_bridge: bool` when `link.kind == "switch"` — the
render/assemble step can add a 0.5s score-state card there.

**Cost / failure modes:** pure graph lookup over existing fields. Failure mode
is over-claiming: `link.kind` is a *hint*, and the renderer must never reorder
a clip to manufacture a link (order stays chronological).

---

## Proposal 4 — `control_points[]`: coarse map-space geography

**Story question:** "Where is this fight happening, and did they take the map
in?"

Purely positional legibility: "A-site hold" vs "mid duel" vs "rotate through
connector" is the difference between a tactical highlight and a frag reel.
Ties directly into the existing util-cam product (lineup PiPs key on
`map:type:side:landing` clusters) — same clustering machinery, same
`settings.cs2util_root` collision mesh, so this is largely reuse.

**Computation:** sample `parse_ticks(["X","Y","Z","team_num","health"])` at
**1 Hz** (64-tick snapshots for a full map is 10k+ rows/player and costs
minutes; 1 Hz over ~35 min = ~2100 rows × 10 players, cheap). Per round,
per team: k-means (fixed `random_state`, k=4) on decimated positions →
`zones[] = {label, centroid, count}`. Then per round:
`territory`: which zones the T side held >60% of the round's sampled ticks;
`contested`: zone where both teams >25%; `site_take` (zone containing a
bomb_planted site in the last 20s and held by winner);
`late_round_pressure` (T presence in a CT zone during CT eco); `rotate`
(a pro crossing >120u zone-to-zone inside 8s while no kill in that window).

**Schema addition:** `round_story[].territory = {held_zones, contested_zones,
plant_zone, site_take}` and a per-demo `zones: [{id, label, centroid,
n_samples}]`. Zone labels come from a static per-map anchor file
(`assets/zones/{map}.json`) with a deterministic fallback to
`zone_0..zone_k` — **no LLM, ever**, for labels.

**Cost / failure modes:** the only genuinely new parse cost in this document
(1 Hz tick sample, minutes at worst). Failure modes: demos without X/Y/Z,
smoke-heavy clusters collapsing, label file missing for an unusual map
(mirrors the existing `EXTRACT_MAP_NOT_FOUND` code path — reuse it). Also
1 Hz can miss a fast 5-second take; accept it, it's a labelling layer not a
verdict.

---

## Proposal 5 — cheap offline LLM narration pass (optional, strictly additive)

**Story question:** "Say the one sentence the templates can't."

Keep this *last* in the pipeline because the deterministic layers already carry
the value. Its only job is fluent phrasing of an already-computed fact — never
inference. (Proposals 1–4 are what make the LLM safe to run at all: the
verdicts are already fixed, so the model is a formatter, not a judge.)

- **When:** after `build_multipov_timeline.py` picks clips, before render.
- **Input:** ~40-80 lines: match header, final score, `round_story[]`
  one-liners for the rounds containing a picked clip, and the `picked[]` list
  with `pro_storyline` rollups. Truncate to JSON lines; total <8k tokens.
- **Output schema:**
  ```json
  {"version": 1,
   "segments": [{"picked_index": 3,
                 "narration": "<=120 chars",
                 "stakes_tag": "map_point|comeback|clutch|punch_up|eco_win|...",
                 "confidence": 0.0}]}
  ```
  Strict JSON, `picked_index` must exist, no new facts: any `stakes_tag` not in
  the enum, or an index out of range, rejects that segment.
- **Cost control:** one call per cut (not per segment), temperature 0, 8k
  cap, ~$0.01/cut. Local dict lookup for player→nick so the model never
  guesses names.
- **Fallback:** on timeout, 4xx, malformed JSON or schema violation, write
  `narration: null` and fall through to the Proposal-1 templated `headline`.
  The renderer treats `null` as "use the template". Nothing in the product
  path may depend on this pass succeeding.

---

## Ranking (best first)

1. **Proposal 1 — `round_story[]`** (highest leverage; the only one that changes
   *what gets cut* as well as *how it reads*)
2. **Proposal 3 — `pov_link{}`** (tiny, zero render cost, biggest perceived
   "this was edited on purpose" effect)
3. **Proposal 2 — `pro_storyline{}`** (satisfies multi-pro legibility; enables
   prose titles/description for free)
4. **Proposal 4 — `control_points[]`** (highest new compute cost, best tactical
   legibility, best reuse of the util-cam clustering)
5. **Proposal 5 — LLM narration** (smallest real gain, largest surface for
   nondeterminism and cost; ship last, or never)

### Highest-leverage single addition

**`round_story[]` with the hinge-kill pivot + stakes rank.** Everything else in
the current stack reads frags off a timeline that has no notion of narrative
shape. A pivot kill turns a 1v2 moment from "two kills" into "he won the round
here"; a stakes rank turns the final pick from "best frag of the match" into
"the round that decided the map". It costs one event pass and **zero extra
CSDM renders**, and it is the field the scheduler, the title generator, the
description generator and a future on-screen lower-third can all share.

---

## Explicitly NOT building

- **A "big play" LLM re-ranking of moments.** The scorer already encodes
  calibrated editorial taste from SIEZ (rando discount, anti-eco disqualify,
  diversity cap, coverage guarantee). An LLM ranking is less predictable,
  harder to regression-test, and would quietly re-litigate decisions that were
  made deliberately. LLM is for phrasing only.
- **Automatic highlight *titles* per clip from the LLM.** Same reason, plus
  the existing Shorts title conventions (`docs/agents/shorts-titles.md`) are a
  human-reviewed register; generated titles will drift from it.
- **Full CS:GO-grade economy reconstruction from ticks alone.** Buy types
  from `item_equip` are worth it; a purchase-level ledger (dropped weapons,
  loss bonuses, kill rewards) is not — the timeline already has an
  authoritative `rounds[]` and the extra precision would buy nothing a viewer
  can see in a 25-minute cut.
- **64-tick full-map position sampling.** 1 Hz is enough to label zones.
  Full-rate sampling would dominate the pipeline's runtime for a labelling
  layer. The high-rate use of positions already exists where it earns its
  keep: the util-cam overlay's own extractors.
- **A separate "story timeline" file with its own version.** One derived layer
  inside `action_timeline.json` (plus `multipov/edit_timeline.json` for the
  `picked[]` annotations) keeps a single source of truth; a parallel story file
  would drift the moment anyone edits the moment extractor.
- **Anything that reorders clips non-chronologically.** Ordering is the whole
  reason a viewer can follow the match. Link detection annotates; it never
  reorders.
