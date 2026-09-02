# CS2Archive — Shorts demand

Partial stars for which Shorts to cut, estimated from public clip views. Separate from long-form listener weighting.

## Language

**Partial star**:
The views one Internal factor still explains after the other Internal factors in the same model. Donk’s ACE moves donk a lot and ACE only by what ACEs do without donk.
_Avoid_: Naive star, raw view dump onto every tag, the listener chip `star` (that is org rank / 2)

**Internal factor**:
An attribute of the match or the moment (player, org, stage, kind of play, …). Not title, thumbnail, description, upload time, or channel packaging.
_Avoid_: covariate, feature, tag (when you mean the thing in the game)

**Naive star**:
Giving a clip’s full view count to every Internal factor on that clip. Rejected: star players would inflate ACE, stage, and opponent together.
_Avoid_: using this as the estimate

**Clip Observation**:
One public clip of a play, with its own view count and source. Latto’s ACE on Allstar, BLAST Highlights, and BLAST main is three Clip Observations, not one averaged number.
_Avoid_: pooled views, combined view count, “the play’s views”

**View outcome**:
log(views) on a Clip Observation. The number Partial stars are estimated from.
_Avoid_: raw view count, views per day

**Clip age**:
Days since that factory posted the clip. A control in the model, not an Internal factor and not a Partial star.
_Avoid_: upload time (that is our packaging), recency star

**v1 Partial stars**:
The four Internal factors that get Partial stars first: POV player, opponent org, kind of play, stage. Map, weapon, pistol, HP, site are not v1. v1 is HLTV: fit Clip Observations from the Source allowlist, rank HLTV cuts with the same four stars. FACEIT scoring waits; when it comes back it reuses this model, not a second index.
_Avoid_: one star per checklist row, a parallel FACEIT star system, ELO as opponent org

**Player**:
The POV on a Clip Observation or a cut, keyed by steam64. Allstar clips already carry steamid + username on the same record as views. TO Shorts resolve nick via the joined match roster or `.data/player_accounts.json`. Unresolved → player unset. Only Recognised Pros get a player Partial star.
_Avoid_: display-name star, @handle as player key, fitting randoms, treating Allstar’s title string as if the player were missing

**Shrinkage**:
Partial stars pull toward zero (the average Recognised Pro / empty opponent / typical kind) when a level has few Clip Observations. No minimum row count. Not the demand-gate floor of 8 POV-channel videos.
_Avoid_: hard clip cutoff, copying `SHORTS_MIN_VIDEOS` onto this model

**Refresh**:
Clip Observation views are re-fetched and Partial stars refit on the daily 12:00 `CS2ArchiveStarRefresh` job (or immediately after it). Not per render, not on the listener poll. Rows older than **180 days** (same window as team demand) are dropped. Clip age inside the model is the recency control, not a shorter window.
_Avoid_: live refit per POV, a second Shorts-only cron, a 30- or 90-day Shorts-only window

**Stage** (v1):
Group (incl. Swiss / opening), playoff (QF/SF), grand final. Not round number, not map number, not FACEIT.
_Avoid_: M1R7 as stage, event brand as a fifth star, FACEIT as a v1 stage bucket

**Source**:
Which factory posted the Clip Observation. A **control** in the model (like Clip age), not a Partial star and not a way to drop a factory. The allowlist is every factory that **counts as a row**, not a menu to pick one: Allstar (HLTV match playlist) **and** BLAST Highlights **and** ESL Highlights (no “in 3 mins” / best-of) **and** PGL Highlights **and** StarLadder Highlights **and** EWC Extra `#cs2` **and** BLAST main play Shorts only. v1 fit uses **all** of those rows together. Out: this channel, team orgs, fan farms, interviews, talent, rumours, BTS, HLTV Twitch boxes.
_Avoid_: source star, YouTube-only first fit, treating Allstar as optional, counting talk or recaps as plays

**Opponent org**:
The other side of the fixture, not the POV’s team. The Partial star key is the canonical HLTV ranking name (`Natus Vincere`, not a second “NaVi” star). Lookup folds popular aliases onto that key (`NaVi`, `NAVI`, `navi`, plus the existing `HIGHLIGHT_ALIASES` / `TEAM_ALIASES` table — extend that table for Shorts spellings, do not fork a new one). Once a Clip Observation is joined to a demo or HLTV match, opponent comes from that match. BLAST @handles are usually the POV’s org and are not this star. Unresolved alias or no join → opponent unset.
_Avoid_: parsing @mentions as opponent, own-org star (v1), a separate NaVi Partial star, a second Shorts-only alias file

**Fixture join**:
Attaching a Clip Observation to a demo or match record. Allstar rows come from the match page’s Allstar iframe **Trending** tab (`topViewedList=true`, sorted by views). The visible label is player + kind (e.g. `latto Dust 2 1V3 Ace Clutch`); the fixture is already the match URL. TO Shorts join through the nearby Team A vs Team B long-form package, then the demo if we have it. Not the global allstar.gg/clips feed, not the HLTV Twitch highlight grid.
_Avoid_: title-only opponent, scraping Allstar without a match page, using the Highlights grid as Allstar

**Kind source**:
Kinds on a Clip Observation come from that factory’s clip label, not from the demo. Allstar: the Trending-tab label on the match iframe (player + map + kinds, same clip as views). TO Shorts: the YouTube title. The joined match is for opponent and stage when those are not already on the clip.
_Avoid_: guessing kind from “this demo has a 1v3 somewhere”, storing Allstar’s title field without the player on that clip, using the HLTV Highlights grid

**Popular event**:
HLTV fixtures we collect Allstar from: BLAST (Open / Premier / Bounty), Esports World Cup, Majors (incl. RMR), IEM, ESL Pro League, PGL and StarLadder. Same 180-day window — not 2027 placeholders, CCT, Challenger, academy, showmatch, open qualifiers, or BLAST Rising.
_Avoid_: every HLTV event in the 180-day window

**Incremental scrape**:
Allstar/HLTV match pages are visited once per match and stored, **Popular events only**. A first full pass of those events is allowed to test Cloudflare; the daily 12:00 job then opens at most **10–20** unseen match pages, **new listener URLs first**, then paced backfill of the 180-day popular-event set. It does not recrawl known pages, wipe the table, or wait to finish backfill before refitting. YouTube Clip Observations can refresh views on **already stored video IDs** via the Data API; that is not an HLTV recrawl.
_Avoid_: scraping every HLTV tournament, deleting the observation store and refilling it, blocking the fit until backfill is complete

**Candidate score**:
Predicted log(views) for a cut we might upload: intercept + player + opponent + every active kind. Stage is a v1 Internal factor in the **fit** (so it does not leak into the other stars) but is **not** in this score — same as Source and Clip age. Empty categories and unset opponent add nothing (baseline), not a penalty. For HLTV Shorts this is the ranker: it replaces `demand_gate.py` (no NAVI/Spirit/Vitality hard hook). Recognised Pro stays. FACEIT keeps the old gate until FACEIT work comes back. A cut at or below intercept is skipped — do not fill the two daily slots with baseline plays.
_Avoid_: four separate thresholds, adding raw view bonuses, Naive star on our own Shorts, stacking Candidate score on top of the hook allowlist, uploading below intercept to fill the day, putting the playoff Partial star back into the ranker

**Slot floor**:
Skip. If every pending HLTV Short scores at or below intercept, upload none that day rather than the two least-bad.
_Avoid_: always filling two slots, treating intercept as a bonus

**HLTV highlight box**:
Editor-picked Twitch embed on the match page, labelled `M{map}R{round} | player — play`. Sorted by map then round. Not view-ranked. Not a Clip Observation (out of Source).
_Avoid_: calling this Allstar, using it as the view source or as Kind source for Allstar rows

**Kind** (v1):
The closed set of play labels a Clip Observation may carry: `1v5_won`, `1v4_won`, `1v3_won`, `2vx_won`, `ace`, `4k`, `3k`, `wallbang`, `knife` (incl. Zeus), `defuse` (1v1 kit, or Ts outnumber CTs), `flick`, `perfect_shots` (kills with fire count ≈ kill count). A clip can hold several at once, one from each Kind category. Categories with nothing matching stay empty (baseline), not a penalty.
_Avoid_: AWP-only `perfect_shots`, `awp_pair`, map in the kind, fused `1v3_ace`, forcing leftover clips into `3k`, `nearly`

**Kind category**:
A mutex group. Clutch (at most one): `1v5_won`, `1v4_won`, `1v3_won`, `2vx_won`. Multikill (at most one): `ace`, `4k`, `3k`. Flick, perfect_shots, wallbang, knife, and defuse are each their own category and may stack with clutch and multikill. A 1v3 ACE is `1v3_won` and `ace`. A 1v3 of perfect flick shots is `1v3_won` + `perfect_shots` + `flick`.
_Avoid_: one exclusive kind per clip, both `1v3_won` and `1v4_won`, collapsing a 1v3 ACE into only `ace` or only `1v3_won`

**perfect_shots**:
Kills with any gun where fire count ≈ kill count. Its own category: a tap ACE is `ace` and `perfect_shots`; a tap 1v3 is `1v3_won` and `perfect_shots`. Not AWP-only. Stacks with flick.
_Avoid_: `awp_pair`, AWP-only kind, replacing `ace`/`4k` with `perfect_shots`

**knife** (detector):
A Shorts-only kind. Skip knife round. Keep only punch-up (victim had a rifle), last kill of a won round, or last-alive Zeus. Not every knife/Zeus frag.
_Avoid_: spawn shanks, eco tases, round-0 knife round

**defuse** (detector):
The kit completes in 1v1, or while Ts still outnumber CTs (1v2, 1v3, 2v4, …). A kit where CTs are ahead, or 2v2 and up even, is not this kind. HP / smoke / spotted do not keep or reject on their own.
_Avoid_: 3v1 leftover T, 2v2 kit, kit after every T is dead

**Intro pick**:
Still only won 1v3/1v4/1v5 clutches and 5-kill ACEs (`short_type` clutch / `4k` with ≥5 kills). New Shorts kinds do not qualify.
_Avoid_: stuffing knife/wallbang into `4k` or `clutch`

**Deferred kind**:
Kinds TOs post that are not v1 Partial stars: `1v2_won`; `util` (molly / HE / flash as the hook); `lurk` (hide / 200 IQ / invisible); `duo` (two named players, not a POV). Recap (“match in 3 mins”) and talk are not kinds and not Clip Observations.
_Avoid_: treating these as v1 Kind stars
