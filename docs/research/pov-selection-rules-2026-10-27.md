# Long-form POV selection audit — 2026-10-28

Scope: current source and local listener/backlog state; no production changes. These observations identify selection risks, not causal estimates of views forgone.

## What actually chooses renders

The listener first removes players failing the shared demand gate: supported player index >=1.40 OR a recent breakout. Missing/stale evidence fails closed. It then selects the highest raw-rating eligible card across all maps in a match. The diagnostic weighted score and predicted views do not select the card. Sources: [gate](../../cs2archive/listener/daemon.py#L717), [single-card selection](../../cs2archive/listener/daemon.py#L697), [shared predicate](../../cs2archive/scoring.py#L84).

Fixture discovery/action order uses team and highlight demand, but the render queue is subsequently sorted by raw rating. `_enqueue` calls `_sort_queue`, which calls `sort_card_records`; the latter sorts only rating descending then path. Thus the documentation's demand-order description should not be interpreted as the final render priority. Sources: [fixture order](../../cs2archive/listener/daemon.py#L790), [queue sort](../../cs2archive/listener/daemon.py#L854), [enqueue](../../cs2archive/listener/daemon.py#L891). The two/day budget makes this distinction consequential.

## Concrete alternatives

Read-only replay of current backlog cards through the current gate and `select_best_card`:

| Match | Queued/chosen | Alternatives | Interpretation |
|---|---|---|---|
| 2399739 Spirit–FURIA | sh1ro Nuke, 1.76, 23–12 | donk Nuke 1.75, 18–10; donk Mirage 1.67, 27–13 | A 0.01 rating margin overrides any player-demand distinction. Current diagnostic score prefers donk Mirage (813,152 vs 768,685), as does model_log_views (2.856 vs 2.781), but neither prediction validates an outcome advantage. |
| 2399739 Spirit–FURIA | same | molodoy Ancient 1.59, 17–11 | Excluded at index1.38: only 0.02 below gate. A useful boundary case for offline review, not evidence to lower the gate blindly. |
| 2399742 G2–DENDELE | entire match skipped, `star gate` | HeavyGod Ancient 1.62, 31–20; NertZ Ancient 1.56, 26–18; doc Nuke 2.19, 17–12 | All three have no demand index. The gate interprets missing audience evidence as ineligible even for exceptional scorelines. This creates exploration blind spots; it does not establish that unknown-player games would outperform known stars. |

Primary records: `.listener/hltv.json`; `backlog/2399739-spirit-vs-furia-pgl-masters-bucharest/{high,medium}/*.json`; `backlog/2399742-g2-vs-dendele-pgl-masters-bucharest/{high,medium}/*.json`. Diagnostic chips were computed with `score_cards.load_indexes()` and `score_card()` at audit time; payloads refresh, so historical decisions can differ.

## Monitoring defect

`gate_forward.own_channel_rows()` tries to get fresh own-channel rows from `_deduped_seed_rows()`, but that helper explicitly removes `CS2 Archive`. The fresh branch can never succeed; it always falls back to `exports/pov_market/own/video_history.csv`, whose local modification time is September 11. This makes the default forward-monitor input stale even if the main seed exports refresh. Sources: [unreachable fresh branch](../../cs2archive/faceit/gate_forward.py#L63), [own-channel exclusion](../../cs2archive/faceit/gate_forward.py#L111). Fix this before judging the breakout trial from default forward-monitor output; the documented trial decision date was October23.

## Recommended interpretation

Keep established stars and one-series/one-slot protection. Review rating-near-ties and missing-index exceptional games as controlled experiments. Do not replace rating with the full weighted score without an independent held-out replay: the existing project documentation explicitly requires this evidence. Separately, ensure actual render priority follows the intended fixture policy and repair the forward-monitor input. Poor views on a correctly chosen high-demand performance cannot by themselves diagnose the selector: matched competitor results and packaging/exposure/retention evidence are needed.

Reference: [listener policy and validation requirements](../agents/match-listener.md).
