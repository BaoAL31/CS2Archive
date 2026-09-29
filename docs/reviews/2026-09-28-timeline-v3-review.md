# Timeline v3 (stakes + zones + pro ledger) — review

- **Date:** 2026-09-28
- **Commit reviewed:** none — `cs2archive/` is untracked (CR-01 refactor in flight). Reviewed as a working tree snapshot.
- **Scope:** diff-less review of
  `cs2archive/highlights/build_action_timeline.py` (`TIMELINE_VERSION=3`, `STAKES_VERSION=1`,
  `_classify_buy`, `_derive_stakes`, `_derive_pro_ledger`, `_economy_by_round`, zone post-pass),
  `cs2archive/highlights/build_multipov_timeline.py` (v2/v3 gate), `tests/test_timeline_v3.py`,
  against the approved proposal set in `docs/highlights-interpretability-proposals.md`.
- **Method:** read the implementation end to end (1588 + 259 lines); re-ran the suite and the lint gate;
  re-parsed `demos/faceit/BCG vs Soldados de Franco - inferno.dem` to re-derive the v3 facts
  independently (equip/side/team/balance/place props, per-round) rather than trusting the supplied
  verification JSON; probed a second FACEIT demo and **three** HLTV demos (props + round numbering);
  ran the builder on an HLTV demo into `.tmp/hltv_anubis_timeline.json` to test the production path.
- **Verdict:** approvable after the must-fix list. The data layer is directionally right and the
  live FACEIT verification reproduces exactly — but three of the shipped fields carry an
  unverifiable-or-wrong value in the general case (bomb `site`, per-pro `deaths`, unresolved places),
  and the first round is silently absent on every HLTV demo I probed, which the v3 verification
  claims do not disclose.

Evidence legend: `[verified]` re-checked by me against the source/data · `[lead]` unverified hint ·
`[refuted]` claim I tried and could not reproduce.

---

## 1. Verdict on decisions 1-7

### D1 — economy sampled at `freeze_end`/`+64` as its own snapshot, not folded into the round-start side snapshot — **KEEP**

`[verified]` Round-start truly is pre-buy: at R1's `round_start` (tick 3186, FACEIT demo) everyone
holds nothing, and at `round_freeze_end` (4882) the sample reads vest+pistol (800/850, `balance` 0-150
= the 800 start money is spent). Folding economy into `_side_map_by_round`'s round-start snapshot
would have read a full buy as an eco, exactly as `dsv` warned.

Two evidence-backed riders:

- `[verified]` `round_freeze_end` fires ~20 s (1270-1274 ticks) after `round_start` on the HLTV demos
  and 26.5 s on the FACEIT one — i.e. it lands at the **end of buy time** (`mp_buytime 20`), which is
  the ideal sampling point. The docstring's "post-buy" claim is therefore stronger than stated; the
  `+64` second sample is surplus rather than load-bearing (no need to widen it to +320: buys are done
  by the freeze tick by definition).
- `[verified]` The offset is *not* uniform across sources, so any future "round-start + N" sampling
  would break; keeping the sample anchored to the freeze event tick is the right call.

### D2 — `balance` deliberately not read — **KEEP the omission, OVERTURN the reason**

`[refuted]` "freeze-tick values looked wrong: flat 16000s" does not reproduce anywhere.
`[verified]` at freeze ticks `balance` is sane spendable money: FACEIT R1 0/100/150, R2 eco side
1600-2050 vs full-buy side 0-600, round starts 800 then 1900-4550; HLTV R1 0/100/150, R4 50-450.
Warmup ticks read 0 here and the flat 16000 belongs to a warmup-money sample (tick ~0), not to the
prop. Omitting `balance` is still correct — equipment value is what an anti-eco verdict needs, not
banked cash — but the *stated* reason is false and is repeated in two docstrings
(`build_action_timeline.py:36-40` and `:850-854`). Left as-is it permanently declares a working prop
broken and blocks the one legitimate future use (distinguishing `save` from `eco`, which needs money
in hand). See CR-24.

### D3 — no region taxonomy, `zone` = raw `place` + `site` — **KEEP raw place; `site` is not a site**

`[verified]` `place` is the engine callout and is already site-level (`Banana`, `TopofMid`,
`BombsiteB`, `CTSpawn`): the k-means/anchor-file taxonomy (proposal 4) is correctly deferred.

`[verified]` **`bomb_*.site` from demoparser2 is a C4 entity handle, not a bombsite.** Live producer
run: `bomb_actions[0] = {"tick": 5069, "round": 0, "type": "plant", "site": "199"}` and the same
handle recurs for every event on the same bomb (`397`, `210`, `199`; negative handles seen in the raw
event frames). Consequence: every `bomb_plant`/`bomb_defuse`/`bomb_explode` moment ships
`zone = {"place": "", "site": "210"}` — a new field carrying a value that is not what it says — and
`bomb_actions[].site`, unchanged since v2, has been feeding the same handle into the LLM prompt at
`build_edit_timeline.py:268` (`site=397`). So "raw place + site" is, today, "place + garbage".
See CR-19 for the fix (it is cheap: the planter's `last_place_name` at the plant tick *is* the site —
10/10 plants on a cache demo returned `BombsiteA`/`BombsiteB`).

### D4 — no impact score on the ledger; raw counts + factual tags — **KEEP**

Right call: value belongs in exactly one place (the value function that already exists, tuned against
SIEZ), and a second scorer in the data layer would be untunable and un-regressable. Two honesty notes:
`tags` never record bomb roles (`planter`/`defuser`) even though `bomb_plant`/`bomb_defuse` moments
exist per-pro, and the approved proposal-2 field list (`util_kills`, `wallbangs`, `best_moment_id`,
`hs_pct`, per-pro `share_of_pro_kills`) is absent. That is a thin-but-honest v1 — say so in the
docstring rather than letting the ledger read as the whole proposal.

### D5 — `best_round = None` for kill-less pros; uninvolved pros omitted — **KEEP both, with one caveat**

`best_round: None` is the honest encoding, and omitting "no kills + no deaths" pros is the right
definition of no-story. Caveat `[verified]`: the death test is run against `kills_all`, which excludes
teamkills, suicides and world deaths, while `rounds[].alive` uses `round_deaths` (which includes
them) — so a pro who died only to a teamkill/fall is *omitted from the ledger entirely* instead of
appearing with `best_round: None`, and every other pro's `deaths` total disagrees with the round
progression. `round_deaths` is already passed into `_derive_pro_ledger` and ignored — the strongest
tell that this was meant to be the source. See CR-21.

### D6 — v2 caches not force-rebuilt (multipov accepts `timeline_version >= 2`) — **KEEP additivity, REFUSE silent staleness**

Additivity is right: nothing reads `stakes` yet, and a forced rebuild would invalidate the inputs the
golden-edit fixtures are built from. Two holes to close in the same commit:

- `[verified]` `STAKES_VERSION` is written to the JSON and read by **nothing**. The comment
  (`build_action_timeline.py:115-118`) claims `INSTA_RULE_VERSION` discipline, but that discipline is
  *enforced* (`pov/pipeline.py:1657` compares `rule_version` via `timeline_matches`); `STAKES_VERSION`
  has no such check and no rebuild path exists for `action_timeline.json` at all. A threshold change
  + bump today rebuilds nothing and leaves stale-but-plausible stakes in place.
- `[verified]` `multipov/edit_timeline.json` records only its own `timeline_version: 2`
  (`build_multipov_timeline.py:199`), so a consumer cannot tell whether the source, and hence
  `picked[].zone`/stakes, exist. Also the CLI gate is `< 2` (`:235`), which accepts *any* future
  version including one that renames fields — an "accept everything ≥2" gate with no provenance is how
  a v4 silently misreads v3.

Minimum fix: have multipov echo `source_timeline_version` + `stakes_version` into its output, and make
the stakes-aware scorer hard-require `timeline_version >= 3` + `buy_source == "ticks"` while behaving
*exactly* as v2 otherwise (that requirement is also what makes the integration test in §3 possible).

### D7 — thresholds (`FULL >= 3500` avg, `ECO <= 1000` avg or `<= 0.2x` opponent, pistols `(1, 13)`) — **KEEP as v1, with three caveats**

- `[verified]` **Pistols `(1, 13)` is data-backed and necessary.** FACEIT R1 = 840/850 per player
  (pistol + vest, start money spent), R13 = 810-890, and the live `m_iTeamNum` snapshot flips exactly
  at R13's freeze tick, confirming R13 is the post-halftime pistol. Without the rule both rounds read
  `eco` (avg ≤ 1000) — so the rule is load-bearing, not cosmetic. Caveat:
  `[verified]` on the HLTV demos the *first* pistol round does not exist in the timeline at all
  (CR-20), so `arc.pistol_rounds` reads `[13]` there.
- `FULL 3500` `[verified]` sits in an empty band in the observed data (full buys 4200-6800 per player,
  force/semi 2400-3900, ecos 240-880) — but the band is narrow by construction: 4 rifles + 1 SMG ≈
  3240 (force) vs 5 rifles ≈ 3600 (full), so 3500 is a real knife-edge. Acceptable as v1; do not
  treat it as tuned until it has been checked on a demo with a genuine semi-buy economy.
- `ECO_RATIO 0.2` `[lead]` never fires independently in any round of the three demos I inspected
  (every relative hit coincided with the absolute cutoff). An unreachable rule is an untested rule:
  either find a case that needs it (a 5-rifle team vs a full-buy team is the only shape where the
  absolute rule fails) or delete it and keep the ladder smaller.

---

## 2. Must-fix findings (ranked, best first)

### CR-19 — bomb moment `zone.site` is a C4 entity handle (`build_action_timeline.py:1382`, `:696`, `:715-716`) — major
`[verified]` Producer evidence: `zone = {"place": "", "site": "210"}` for `bomb_plant`, `bomb_defuse`,
`bomb_explode`; `bomb_actions[].site` holds `199`/`210`/`397`. The v3 zone post-pass propagates it
because `site` is truthy, so a bomb moment can never degrade to `None` — it always ships a wrong
string. Pre-existing wart in `bomb_actions` (and in the LLM prompt at `build_edit_timeline.py:268`)
now promoted into a new field.
**Fix:** derive the site from the planter's/defuser's `last_place_name` at the action tick and drop the
handle. Verified viable: at 10/10 plant ticks on a cache demo the planter's callout was exactly
`BombsiteA`/`BombsiteB`. Concretely: add the bomb-action ticks to the place snapshot tick set (or a
small planter-sid map alongside `bomb_actions`) and set `"site": place or ""`, leaving
`place`/`site` mutually exclusive; then the existing post-pass needs no special case. Also fix the
prompt line so the LLM stops seeing `site=397`.

### CR-20 — the first round is absent from every HLTV timeline (`build_action_timeline.py:1053`) — major
`[verified]` Three HLTV/PBDEMS2 demos (falcons-vs-aurora m1 anubis, m3 inferno, 3dmax-vs-vitality m1
inferno) emit `round_start` for round 1 at tick **1**; the `if t <= 1: continue` phantom filter drops
it, while FACEIT emits round 1 three times (ticks 0/2939/3094/3186) and survives only because a later
duplicate exists. Real build on the anubis demo: 19 rounds starting at round 2, `score_after` ending
`{2: 12, 3: 7}` for a 13-7 match, `arc.pistol_rounds == [13]`, round 1's kills/bombs absent from
`kills`, `kills_all`, `moments`, `pros[].rounds` and `stakes`, and a round-0 bomb plant leaking into
`bomb_actions`/`winner_by_round["0"]` (feeds the LLM batch round set at
`build_edit_timeline.py:107`). Pre-existing, not introduced by v3 — but it silently guts v3 on any
non-FACEIT input, and the brief's "R1 pistol / streaks / conversion" evidence is FACEIT-only for this
reason.
**Fix:** stop dropping tick-0/1 rows wholesale; rely on the existing "one per round number, last seen"
dedup and only discard a row that is a genuine phantom (`round <= 0` *and* superseded). Same for the
`t > 1` filter in `_side_map_by_round` (`:159`), otherwise round 1 has no side snapshot and winner
attribution falls back to the heuristic there. `[verified]` no FACEIT behaviour changes (round 1 still
resolves to the 3186 row; `(first_freeze, 0)` insertion still precedes round 1 in tick order, and
kills are impossible during freeze, so nothing that currently maps to a round starts mapping to 0).
Recommend extracting the round-start construction into a pure `_round_starts(…, first_freeze)` helper
so it is unit-testable — it is the one piece of v3's inputs with no test at all.

### CR-21 — `_derive_pro_ledger` ignores its `round_deaths` parameter; `pros[].deaths` is undercounted (`:887`, `:935-947`) — major
`[verified]` Deaths are counted only from `kills_all`, which by construction excludes teamkills,
suicides and world deaths (`:1283-1292`), while `rounds[].alive` (`:1462`) uses `round_deaths`. The
signature takes `round_deaths` and never reads it; `tests/test_timeline_v3.py` passes a `deaths` dict
that changes nothing (the test would still pass with `{}`), which is the tell. Failure mode: a pro who
falls off the map or is teamkilled has a lower `deaths` than the timeline shows, can be omitted from
the ledger entirely under D5's rule, and can show a "silent" round they actually died in.
**Fix:** count deaths from `round_deaths` (already `{round: [(tick, sid)]}`), or drop the parameter and
document the exclusion — do not leave a passed-and-ignored parameter.

### CR-22 — unresolved places are silent, and the `tick - 2` place fallback is dead (`:1110-1112`, `:1145-1150`, `:1129`) — major
`[verified]` Buys got a provenance guard (`buy_source`) but places did not: if `last_place_name` does
not resolve (or the whole snapshot degrades), every `zone` becomes `None` with no signal anywhere,
which is the exact silent-failure class CR-09 and the `insta_kill` mesh guard exist to stop (the
`if "last_place_name" in columns` check at `:1129` proves the author expected absence, then did
nothing loud about it). Also `[verified]` `_place_at` tries `tick, tick-1, tick-2` while
`_weapon_query_ticks` only ever contains `t` and `t-1` (`:1109-1112`), so the third probe can never
hit — the intended 2-tick lookback was never implemented (the same dead probe is inherited by
`_victim_weapon`).
**Fix:** either add `t-2` to the query or delete the offset, and emit `place_source: "ticks"|"unknown"`
next to `buy_source` (plus a warning when unknown) so a zone-less timeline is distinguishable from a
timeline whose place prop failed.

### CR-23 — `STAKES_VERSION` is inert and multipov does not record its source version (`:115-118`, `:1529`; `build_multipov_timeline.py:199`, `:235`) — major
`[verified]` Nothing reads `STAKES_VERSION`; the CLI gate is `< 2` and the output echoes only
multipov's own version. See D6 for the fix and why it is a prerequisite for the integration test.

### CR-24 — two docstrings assert facts that are false (`:36-40`, `:850-854`, and the `site` claim at `:23`) — minor
`[refuted]`/`[verified]` `balance` is not "flat 16000s" at freeze ticks (D2); `site` is not a
bombsite (CR-19). Comments that declare a working prop broken, and a data field mislabelled, will
mislead the next maintainer more than no comment at all — fix the wording in the same commit as CR-19.

### CR-25 — `stakes.equip_value` cannot be turned back into the quantity it was classified on (`:855-880`, `:800-812`) — minor
`[verified]` The classifier thresholds per-player averages (`total / players`) but only the team total
is serialized; `players` is dropped, and `buy_source` is per-round rather than per-team. A consumer
that wants a graded anti-eco signal (rather than the 4-class verdict) or needs to know the sample size
cannot recover it. One-line addition: `"equip_players": {team: n}` (or serialize the average).

---

## 3. Test gaps before scoring integration

1. **`_economy_by_round` has zero tests** `[verified]` — the only parser-dependent new function, and
   the one carrying tick bookkeeping (freeze and `+64` → round, max-per-sid, the `resolved=False`
   paths for missing column / empty frame / non-frame). A stub parser exposing `parse_ticks` returning
   a DataFrame makes it fully testable with the existing `_derive_moments`-style pattern. Nothing else
   exercises `tick_to_round` mapping at all.
2. **The bomb-zone test encodes a value the producer never emits** `[verified]`
   (`test_timeline_v3.py:181` uses `site: "A"`). It asserts a fixture the real code cannot produce, so
   it *hides* CR-19 while looking like coverage. Rewrite it against the real shape (handle today,
   callout after the fix).
3. **Missing regression test for "v2/unknown buys change nothing"** — the acceptance criterion for the
   integration: with `buy_source != "ticks"` (and `stakes` absent entirely), `build_multipov_timeline`
   output must be byte-identical to today's. Without it the integration cannot be merged safely.
4. **Missing: a clutch whose `kill_ticks` include the round-end/defuse win tick** — the case where the
   zone post-pass's "first tick with a known place" rule has to walk past a non-kill tick.
5. **Missing: pro deaths from teamkill/suicide/world** — would fail today, pinning CR-21.
6. **Missing: any assertion pinning `TIMELINE_VERSION`/`STAKES_VERSION` in the returned dict**
   (`test_multipov_accepts_v3_timeline` passes `stakes_version: 1` and multipov ignores it).
7. **Missing: a real 4-streak `swing` case** — `_rounds5` only produces a 3-streak by the 5th round;
   the "≥3 streak broken by the trailing team" boundary (`streak[o] >= 3`) is untested at the edge
   (`>= 3` vs `> 3`).
8. **Missing: the round-start construction** (CR-20) — see the suggested pure helper.

---

## 4. Downstream risk

| Consumer | Verdict | Evidence |
|---|---|---|
| `build_edit_timeline.py` (LLM path, the shipping product) | **Safe** | `[verified]` `_split_into_round_batches` builds an explicit key set (`kills`, `bomb_actions`, `round_starts/ends`), so the prompt is unchanged and no golden fixture needs regeneration; `_load_action_timeline` has no version check. One live wart: line 268 still prints the C4 handle as `site=` (CR-19), and the batch round set includes round 0 on HLTV demos (CR-20) |
| `cs2archive/shorts/build_short_timeline.py` | **Safe** | `[verified]` `build_short_timeline_from_action` reads `kills`/`bomb_actions`/round metadata and ignores new fields; `persist_action_timeline` writes a *different* minimal file (no `timeline_version`) into `renders/shorts/shorts-{stem}/` (`pov/create_backlog.py:463`, faceit equivalents) — no path collision with `renders/hl-{stem}/`, and the `< 2` gate rejects that file with a clear message |
| `cs2archive/hltv/score_cards.py` | **Safe** | `[verified]` reads only `kills` (`:254-257`); unfiltered-4K counting unaffected |
| `thumbnail/utils.py`, `misc/*golden*`, `tests/test_cache_full_golden.py` | **Safe** | `[verified]` read `kills`/`tickrate` (`thumbnail/utils.py:162-175`) |
| `cs2archive/highlights/round_score.py` | **Works, but is a second eco gate** | `[verified]` `_champion_key` zeroes tier-failed multikills and taints farm-adjacent values (`:84-100`, `:118-130`). Any buys integration done in `build_multipov_timeline` must land here in the same commit or the round-level and moment-level verdicts diverge |
| `render_edit_timeline.py` | **Cannot consume multipov output at all** | `[verified]` requires `edit_tl["segments"]` (`:143`, `:185`); multipov emits `picked[]`. So today the only consumer of v3's new fields is multipov's own `picked[]` — the "silently misread" risk is latent, not live, and `hf/` has no `action_timeline` usage |
| v2 caches | **Silently identical, which is the risk** | `[verified]` `moment_value`/`_tainted_rounds` never touch stakes, so a v2 cache produces last week's cut with no signal — acceptable by D6, but only with the CR-23 provenance |

---

## 5. Scoring-integration readiness

**`rounds[].stakes.buys` is sufficient *as data*, insufficient *as a verdict*, and the approved step as
worded — "replace the `tier_ok` anti-eco proxy with real buys" — should not be done literally.** Three
reasons, in order of weight:

1. `[verified]` `tier_ok` is *per-victim equipment at the moment of death* (`_meets_tier_criterion`,
   `shorts/build_short_timeline.py:504-525`), which is strictly finer-grained than a team-level
   freeze-end buy class. It already reproduces the SIEZ ground truth: attacker AK (tier 4) vs four
   Tec-9 victims (tier 1) fails → 0. Buys would *rescue* that case — 5 Tec-9 + kevlar ≈ 1150/player
   classifies as `force`, not `eco` — so a literal swap regresses a calibrated skip.
2. Buys' unique contribution is the opposite direction: it can **promote** what `tier_ok` treats
   neutrally (attacker team on an eco taking a multikill off a full-buy team currently scores the same
   as a farm kill), and it catches the eco-with-looted-rifles farm that `tier_ok` misses. Both need
   the attacker's and the victims' *team* identity, which moments do not carry today.
3. Blockers to wire anything: moments have no team ids (only steam ids + `round_won_by`), and
   `moment_value(moment, tainted)` has no access to `rounds` (`build_multipov_timeline.py:60`), while
   `score_rounds(atl)` does. Any use must gate on `buy_source == "ticks"` or every v2 cache changes
   behaviour (contradicting D6).

**Exact change I approve** (conservative: adds two behaviours, removes none, no SIEZ retune, inert on
v2/unknown data):

```python
# build_action_timeline.py — multikill moment detail (deterministic, single source of truth)
"attacker_team": team_by_sid.get(aid),
"victim_teams": sorted({team_by_sid.get(k["victim_steam_id"]) for k in ak} - {None, 0}),

# build_multipov_timeline.py
def moment_value(moment, tainted_rounds, rounds_by_round=None):
    ...                       # base value + why, unchanged
    # v2 rule, unchanged: per-victim precision outranks the team-level class
    if moment["type"] == "multikill" and not moment.get("tier_ok", True):
        return 0.0, why + ["tier-fail anti-eco farm -> 0"]
    st = (rounds_by_round or {}).get(moment["round"], {}).get("stakes") or {}
    if st.get("buy_source") == "ticks" and moment["type"] == "multikill":
        buys = st.get("buys") or {}
        vbuy = {buys.get(str(t)) for t in moment.get("victim_teams") or []}
        abuy = buys.get(str(moment.get("attacker_team")))
        if vbuy == {"eco"} and int(moment.get("kill_count", 0)) >= 4:
            return 0.0, why + ["4K+ vs eco buy -> 0"]      # buys-only veto
        if abuy == "eco" and vbuy and vbuy <= {"full", "force"}:
            base *= 1.5; why.append("eco hero x1.5")        # new promotion
    ...
```

plus `_tainted_rounds(moments, rounds_by_round)` unioning the rounds where the buys veto fired, and the
same helper wired into `round_score.py`'s taint/champion path in the same commit.

**Explicitly rejected variants** (each resurrects a case the calibration deliberately dropped):
(a) dropping the `tier_ok` veto when buys say `full`/`force` (rescues the Tec-9 4K);
(b) softening `force` to `×0.5` instead of 0 (same case, and inventing a new weight with no ground
truth); (c) any path that reads stakes when `buy_source != "ticks"` (makes v2 caches behave
differently — violates D6 and kills the byte-identical regression test).

**Acceptance criteria for the integration:** `picked` identical on the two v2 fixtures and on
`_rounds5`-style synthetic input with `equip_by_round={}`; on the verified 13-0 FACEIT demo the only
deltas allowed are eco-hero promotions and buys-veto disqualifications, each with its `why` string.

---

## Reproduction (all commands run from the repo root)

```powershell
# suite + lint (agrees with the brief)
C:\Users\jembo\anaconda3\envs\cs2archive\python.exe -m pytest tests/test_timeline_v3.py tests/test_action_timeline_v2.py tests/test_multipov_timeline.py -q   # 34 passed
uvx ruff@0.16.9 check cs2archive/highlights/build_action_timeline.py cs2archive/highlights/build_multipov_timeline.py tests/test_timeline_v3.py           # clean

# property/round-numbering probes (scratch scripts kept in .tmp/)
python .tmp/probe_equip.py        # freeze-tick equip/balance/team_num on the FACEIT demo
python .tmp/probe_balance.py      # 'balance' is not flat 16000 (refutes the docstring)
python .tmp/probe_plant2.py       # planter's last_place_name at plant tick == BombsiteA/B (10/10)
python .tmp/probe_hltv_props.py   # HLTV: current_equip_value + last_place_name + team_num resolve
python .tmp/probe_hltv_rounds2.py # HLTV: round_start(1) at tick 1, freeze_end has no round col
python -m cs2archive.highlights.build_action_timeline demos/hltv/...m1-anubis.dem --output .tmp/hltv_anubis_timeline.json   # 19 rounds, 12-7, zone.site="210"
```

Scratch artifacts (`.tmp/probe_*.py`, `.tmp/hltv_anubis_timeline.json`) are evidence for CR-19/CR-20
and can be deleted with them.
