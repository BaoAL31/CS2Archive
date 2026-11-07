# POV selection and video performance — 28 October 2026

The selector finds some commercially promising games, but our uploads are not converting that demand into views. Selection needs tightening at the weak end; the evidence does not support simply replacing donk/kyousuke with different players. Some of the exact performances we selected earned thousands of views on other channels while ours earned single digits or tens.

## Evidence and limits

Fresh YouTube Data API snapshot at 2026-10-27 22:08 UTC (28 October Sydney): 366 public videos in the last 180 days. Of these, 209 pass the audit's POV filter: at least five minutes, a recognised player parsed by the existing collector, and FACEIT or a `vs` fixture in the title. This is a title-based operational filter, not a manually verified census. Shorts, utility tutorials, private and deleted videos are excluded. Public counts can change between requests.

Sources: [raw own videos](../../exports/pov_selection_20261028/public_raw.json), [cohort summary](../../exports/pov_selection_20261028/summary.json), [recent POVs](../../exports/pov_selection_20261028/recent_povs.csv), [matched competitor responses](../../exports/pov_selection_20261028/matched_competitors.json). Reproduce summaries with `python -m exports.pov_selection_20261028.analyze`. The collector is an isolated research script; production selection, queue and upload state were not changed.

The saved Analytics OAuth token returns `invalid_grant` on refresh. Impressions, click-through rate, watch time, retention and subscriber conversion were unavailable. No footage was watched for this audit. This is a selection/outcome audit, not audiovisual QA. We cannot distinguish a reach problem from a thumbnail/title problem or a viewing-quality problem from these counts alone.

## How recent choices performed

Use the 25 uploads aged 7–30 days as a recent cohort; the seven younger uploads are too fresh for a verdict.

| Recent cohort | Videos | Median views | Median lifetime views/day |
|---|---:|---:|---:|
| All POVs, 7–30 days | 25 | 15 | 0.90 |
| FACEIT | 14 | 16.5 | 0.84 |
| HLTV | 11 | 15 | 0.90 |
| donk | 5 | 11 | 0.63 |
| kyousuke | 4 | 10.5 | 0.44 |
| ZywOo | 2 | 15 | 0.95 |
| MaiL09 | 2 | 17.5 | 1.45 |
| NiKo | 2 | 19 | 1.38 |

Recent standouts include nocries' 29–11 Dust2 and YEKINDAR's 19–10 Mirage versus BB. The nocries upload had about 95 views in the final snapshot and YEKINDAR had 62; both outperform most recent selections, but one or two videos cannot establish a player preference. Donk and kyousuke consume nine of the 25 recent uploads (36%) without producing reliable own-channel winners.

Older cohorts have median views of 16 at 30–60 days, 27 at 60–90 days and 50.5 at 90–180 days. These are descriptive comparisons only. Lifetime views/day falls as videos age, and neither dividing by age nor requiring age >=7 days makes this a fixed-age experiment. There is no evidence here that the latest selector improved seven-day views. Do not claim a 0.90/0.33 improvement ratio as causal lift.

## Strongest diagnostic: the same performances succeed elsewhere

All competitor counts below were refreshed in the same audit. Match identity is strong for the named tournament fixture. FACEIT matches are likely matches from the same player, K/D, map and nearby dates, but demo IDs were not independently joined.

| Performance | Our upload | Competitor uploads |
|---|---:|---|
| donk 31–7, Spirit vs PARIVISION, Anubis | [10](https://www.youtube.com/watch?v=h1eYmyUZAKw) | [LIM Pro: 12,680](https://www.youtube.com/watch?v=UB-_4KZImgs), [imfcnd: 22,897](https://www.youtube.com/watch?v=CsHCynG54ig), [EDCS: 388](https://www.youtube.com/watch?v=ncjIvhST2FE) |
| donk 36–15, FACEIT Nuke | [11](https://www.youtube.com/watch?v=TpbIrRLlmOU) | [LIM: 17,317](https://www.youtube.com/watch?v=u9DOq8200nc), [EDCS: 1,693](https://www.youtube.com/watch?v=bwKJ63ou-6w) |
| kyousuke 26–11, FACEIT Ancient | [7](https://www.youtube.com/watch?v=vdDNg2-LxgA) | [LIM: 7,175](https://www.youtube.com/watch?v=Vfd5ROiM7Uw), [EDCS: 390](https://www.youtube.com/watch?v=FrC3-r99JCA) |

The tournament donk upload was published only about 46 minutes after imfcnd's upload; delay alone does not explain that example. The Nuke donk upload followed LIM by about 31.5 hours, so freshness may matter there. Different audiences, channel sizes, edits and presentation prevent interpreting these ratios as the uplift a selector change could deliver. They do show that these performances had demand: discarding them for being poor picks would misdiagnose the problem.

## What current selection actually optimises

The long-form HLTV path first filters players through supported demand >=1.40 OR a recent breakout, then chooses the highest rating among eligible cards in each series. The global queue also sorts by rating. The richer `star_score` and `model_log_views` computed in `score_cards.py` do not determine these listener choices. See [selection](../../cs2archive/listener/daemon.py:650), [queue order](../../cs2archive/listener/daemon.py:854), [scores](../../cs2archive/hltv/score_cards.py:350).

Consequently, the gate decides which players can compete and rating decides which eligible performance ships. This is a reasonable simple heuristic, but it is not a demonstrated prediction of our channel's views or watch time.

Concrete current examples from `.listener/hltv.json` and backlog cards:

- Spirit–FURIA: sh1ro Nuke 1.76 (23–12) wins over donk Nuke 1.75 (18–10) and donk Mirage 1.67 (27–13). The 0.01 margin decides the slot; neither player is proven to be the better choice for us.
- The pending queue includes ropz Cache 1.34 (13–13), PR Ancient 1.14 (21–18), Wicadia Cache 1.07 (18–17), and b1t Nuke 1.00 (16–16). Being an eligible player lets a fairly ordinary match enter the render queue. These are the weakest uses of scarce render capacity; do not assume every passing fixture deserves production.
- MOUZ–9z: PR Nuke 1.66 is chosen while torzsi Nuke 2.09 is excluded. Conversely, G2–DENDELE is wholly gated out despite doc Nuke 2.19 and HeavyGod Ancient 31–20. Those are coverage blind spots, not evidence that the rejected POV would get more views.
- Current XANTARES index 1.49 and dem0n index 1.80 still fail eligibility because sample support is absent. Reading just the numeric star misrepresents the actual gate.

Do not switch the live queue to the fitted model solely because its validation numbers look stronger: the target data, channel effects, feature timing and held-out protocol need to be checked first. Historical public counts captured after a simulated cutoff do not reconstruct what was known at that cutoff.

## Additional findings

Two separate public IDs have the identical sh1ro 19–14 Spirit–BB Anubis title and matching description lead: [18eoCNB7Ig8](https://www.youtube.com/watch?v=18eoCNB7Ig8) and [piGhqtaxV5c](https://www.youtube.com/watch?v=piGhqtaxV5c). They had three and five views in the snapshot. This establishes duplicate public listings; it does not establish byte-identical video content or two independent renders. Check repair/reupload handling before treating them as distinct selections.

The existing forward experiment was due on 23 October (`.data/gate_forward.json`), but its measurement path has a flaw: `own_channel_rows()` asks `_deduped_seed_rows()` for CS2 Archive rows even though that helper explicitly removes CS2 Archive. The fresh-data path is unreachable and it falls back to the legacy own-history CSV. See [gate_forward](../../cs2archive/faceit/gate_forward.py:63). Its historical own-channel PI is unsuitable as a current verdict without repairing and verifying the input. No repair was made in this analysis.

## Recommended next decisions

1. Keep selecting strong, demonstrably marketable performances, including donk. Do not infer that player demand vanished because our distribution is weak.
2. Hold weak queued POVs for comparison against better fresh performances rather than automatically filling capacity. A 1.00/16–16 b1t game needs a specific reason to beat another available game; gate membership alone is insufficient. Test any performance floor prospectively rather than tuning it to these 25 videos.
3. Restore read-only Analytics and capture each upload's first seven complete days: views, impressions/CTR where available, watch minutes, average percentage viewed and early retention. Compare within format, age window and release period. Low impressions would point to reach; adequate impressions with weak CTR to presentation; adequate clicks with early exits to video/opening quality.
4. Record every candidate's score and exclusion reason at selection time, the chosen card, competing card, render cost, and linked upload IDs. Evaluate a challenger ranker alongside the existing one before replacing it. Include a small, explicitly budgeted exploration sample so unsupported players can gain evidence.
5. Fix the forward measurement input and audit duplicate public listings. Those are more immediate than another unvalidated scoring formula.

Verdict: several POV choices are correct in the sense that the same performances attract a substantial audience elsewhere. Our results remain poor, and the current gate permits ordinary games while excluding some exceptional ones. Tighten that weak tail, but investigate reach and viewing experience before spending more render time on a different roster.
