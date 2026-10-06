# Shorts picker: live Allstar audit exposes candidate/feature mismatch

Status: reproduced; scoring/candidate fixes not applied by this audit.
Extraction bugs below marked FIXED 2026-10-06 with regression tests; model/ranking
work remains open. Do not cite the model's aggregate log-loss advantage as evidence
that the production top-two picker selects good clips.
Measured 2026-10-06 Australia/Sydney, using fresh public sections, not cached
view snapshots. Do not cite the model's aggregate log-loss advantage as evidence
that the production top-two picker selects good clips.

## Scope and primary evidence

Primary [EPL24 results](https://www.hltv.org/results?event=8244&startDate=2026-10-02&endDate=2026-10-05&offset=0).
Twenty completed matches have match-start timestamps on October 3–5 Sydney time,
verified via HLTV `.timeAndEvent .date[data-unix]`, event link and final scores.
Fresh Allstar API retrievals: 2026-10-05 15:37:59–15:44:44 UTC. Seventeen sections
contain 467 fixture-matching clips; three sections return 79 historical fallback
clips, excluded. Local demos exist for only three of these matches.

Raw evidence and reproducible debug scripts (TEMP, never production writes):

- `C:\Users\jembo\AppData\Local\Temp\opencode\allstar-recent-days-audit.json`
- `C:\Users\jembo\AppData\Local\Temp\opencode\audit_recent_picker.py`
- `C:\Users\jembo\AppData\Local\Temp\opencode\recent-picker-audit-results.json`
- `C:\Users\jembo\AppData\Local\Temp\opencode\trace_missing_4ks.py`
- `C:\Users\jembo\AppData\Local\Temp\opencode\missing-4k-gate-trace.json`

## Wallbang feature is applied to a different product

The current cleaned training store has 469 wallbang-labelled clips:

| Explicit kill count in factory label | Clips |
|---|---:|
| 1 | 11 |
| 2 | 96 |
| 3 | 275 |
| 4 | 72 |
| ACE / 5 | 8 |
| Unspecified | 7 |

451/469 (96.2%) explicitly describe 2–5 kills. Two-kill context is not separately
encoded in the current kind vocabulary. The recent factory cohort has 57 wallbang
clips, only two explicitly labelled 1K. Our local candidate pool, however, has
34 wallbang cuts and **all 34 contain one kill**.

`build_short_timeline.detect_shorts` emits `short_type=wallbang` for individual
rifle/AWP penetration kills. `clip_observation.kinds_from_cut` awards wallbang
only when that is the cut's type; multikill/clutch cuts do not inherit the tag
from their underlying penetration kills. Thus the learned modifier is applied to
the single-kill population while real wallbang multikills can lose the feature.
This is distribution/representation mismatch, not proof the fitted coefficient
is a causal quality measure. Training on published factory clips also does not
establish view calibration for ordinary demo kills that factories don't publish.

For zorte Cache round4, the current log score is:

```
intercept 6.231419 + player 1.516823 + opponent(FURIA) 1.317660
+ stage(group) -0.538792 + wallbang 0.472598
```

The wallbang term multiplies the view estimate by **1.604**: 5,050 becomes 8,101.
It is not the whole score, but this lift moves the clip into the second slot.
Setting only that coefficient to zero in the same local preview drops zorte out;
the replacement is a m0NESY three-kill perfect-shots cut (~6,080), not donk's
much more-viewed four-kill clip. No production coefficient was changed.

## Live ranking check

Compare predicted top two with actual top two within each fixture, restricted
to recognized pros for a fair product comparison. This uses **factory features**
and known factory clips, not the end-to-end detector. It is the cleaner ranking
subproblem; extraction can lose additional clips.

| Fixed-model diagnostic | All 17 fixtures | 11 fixtures absent from training |
|---|---:|---:|
| Current top-two hits | 16/34 (47.1%) | 9/22 (40.9%) |
| Both leaders found | 4/17 | 2/11 |
| Neither leader found | 5/17 | 4/11 |
| Actual views captured versus best two (pooled) | 46.6% | 50.2% |
| Wallbang coefficient zero: top-two hits | 18/34 (52.9%) | 11/22 (50.0%) |
| Wallbang coefficient zero: pooled view capture | 63.1% | 67.6% |

155 live clip IDs / six fixtures overlap training; the 11-fixture column removes
those entire fixtures. The zero-coefficient result is a one-variable ablation
on this inspected cohort, **not** a independently validated replacement model.
Absent clips are not assigned zero views.

## Deterministic extraction failures reproduced on real demos

### First pistol round incorrectly treated as warmup — FIXED

[FURIA vs BetBoom](https://www.hltv.org/matches/2398724/furia-vs-betboom-esl-pro-league-season-24):
YEKINDAR Cache Glock wallbang4K round1 has **55,166** Allstar views at this snapshot.
`detect_shorts` discarded start events at tick<=1, then unconditionally inserted
`(first_freeze, 0)`. In this demo first_freeze=1272 and the next retained start is
tick5902/round2. Four real pistol kills at ticks3850,5337,5372,5582 consequently
mapped to round0 and were discarded as warmup. The tier criterion passes and his
team wins; neither is the reason for this omission.

Fix (`build_short_timeline.detect_shorts`): tick<=1 starts carrying a real round
number are kept (CR-20 parity with `build_action_timeline`); the
`(first_freeze, 0)` warmup entry is only inserted when the earliest numbered
start is after first_freeze, keeping the list sorted. Regression:
`test_round_1_start_at_tick_1_is_not_warmup`. Re-traced on the real demo: the
clip now emits as round 1 (`passed4k_gates`).

### Manual weapon-tier gate excludes a popular 4K

[9z vs NAVI](https://www.hltv.org/matches/2398735/9z-vs-natus-vincere-esl-pro-league-season-24):
luchov Cache MP9 4K round2 has **22,513** views. The raw four kills are present,
his team wins, and he is recognized. Three victims hold Glocks and one holds an
AK. `_meets_tier_criterion` requires two victims holding weapons at least as high
a tier as the MP9, so it rejects the entire moment. Single wallbang kills do not
have an equivalent weapon-tier gate. This is an intentional but unvalidated
product rule whose impact conflicts with the view-prediction objective.

Fix (`_meets_tier_criterion`): the gate now applies only to rifle-tier (>= 4)
attackers — the eco-farming abuse case. Sub-rifle (pistol/shotgun/SMG) 4Ks pass
unconditionally. Existing eco-farm rejection tests (AK vs pistols) still pass;
new regression `test_4k_with_smg_attacker_passes_tier_gate`. Re-traced on the
real demo: luchov's MP9 4K round 2 now emits.

Also fixed alongside: final-round fallback — a round with kills but no end event
(truncated demo tail) ends at last-kill + 10s instead of silently dropping its
moments (`test_final_round_without_end_event_keeps_clutch`).

### Coarse score still underrates the actual biggest local clip

[Spirit vs PARIVISION](https://www.hltv.org/matches/2398725/spirit-vs-parivision-esl-pro-league-season-24):
donk Ancient4K round16 is correctly extracted, but predicted at 5,101 versus
**104,624** actual views (~20.5x underprediction). Removing only the wallbang
bonus does not repair this player/kind/coarse-context ranking failure.

## Correct acceptance criterion for follow-up work

Repair round reconstruction and align detector features/candidate population with
the training labels before declaring the picker good. Measure actual leader
coverage, ranking hits and captured views alongside view loss on fresh,
fixture-held-out data. Feature ablations must be reported separately from deployed
fixes; ordinary singles should not receive a confident view forecast merely
because a factory-highlight model has a positive wallbang coefficient.
