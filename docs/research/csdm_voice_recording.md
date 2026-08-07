# CSDM + HLAE: Recording in-game player voice for CS2 (Source 2) demo renders

> Research compiled from primary sources: the bundled `cli.js` inside the installed
> `cs-demo-manager` app (CSDM 3.20.0), the akiver/cs-demo-manager GitHub repo, its
> docs site, and the advancedfx/advancedfx (HLAE) repo + wiki.
>
> Context: user renders FACEIT (PBDEMS2) CS2 demos with
> `csdm video --mode player --event rounds --record-audio --player-voices --recording-system HLAE`
> plus an in-game cfg containing `voice_enable 1`. Result: rendered audio has **no voice**
> and the CS2 HUD shows **"voice disabled"**.

---

## TL;DR

- For **CS2** demos, CSDM's `--player-voices` does **NOT** touch `voice_enable`. It only
  issues `tv_listen_voice_indices -1` and `tv_listen_voice_indices_h -1` at the pre-roll
  tick, plus generates console aliases (`voice_all`, `voice_ct`, `voice_t`,
  `voice_<SteamID64>`). `voice_enable 1/0` is CS:GO-only in CSDM's code.
- HLAE/mirv_streams captures the game's **full mixed audio** via
  `mirv_streams record startMovieWav 1` (on by default, and what `--record-audio` sets).
  It needs **no special voice setting** — it records whatever the game actually outputs.
- Therefore a "no voice in render" failure means **the game is not outputting voice at
  render time**, not that HLAE is failing to capture it. The two gatekeepers are:
  1. the demo must actually **contain** the voice data, and
  2. the client must be both listening (`tv_listen_voice_indices`) **and** have
     `voice_enable 1` so the engine emits the voice into the audio mix.
- The closest match to this exact symptom is GitHub issue **#966** (FACEIT demo, CS2
  V3.10 update, "no player voice in video generation" while **Watch → Round** still had
  voice). It was closed as "somehow solved" — i.e. a **CS2-side bug**, not a CSDM or HLAE
  one.
- The "voice disabled" HUD is separately a **known CS2 bug** (broken speaker/voice
  indicator in demo playback); HLAE has a proposed fix for the speaker icons
  (`mirv_voiceHudFix`, advancedfx PR #1182, not yet merged).

---

## 1. How CSDM's "player voices" works for CS2 (Source 2)

Source: the bundled `cli.js` inside
`C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\resources\app.asar`
(minified; the sequence builder is the `Jo` class). The relevant logic:

```
enablePlayerVoices(e){ ...
  return this.game===y.Game.CSGO
    ? this.actions.push({cmd:"voice_enable 1",tick:r})
    : (this.actions.push({cmd:"tv_listen_voice_indices -1",tick:r}),
       this.actions.push({cmd:"tv_listen_voice_indices_h -1",tick:r})) }

disablePlayerVoices(e){ ...
  return this.game===y.Game.CSGO
    ? this.actions.push({cmd:"voice_enable 0",tick:r})
    : (this.actions.push({cmd:"tv_listen_voice_indices 0",tick:r}),
       this.actions.push({cmd:"tv_listen_voice_indices_h 0",tick:r})) }
```

Key facts:

- **CS2 path uses only `tv_listen_voice_indices` / `tv_listen_voice_indices_h`.**
  - `--player-voices` → `tv_listen_voice_indices -1; tv_listen_voice_indices_h -1` (listen to all).
  - default `--no-player-voices` → `tv_listen_voice_indices 0; tv_listen_voice_indices_h 0` (listen to none).
- **`voice_enable 1/0` is emitted only for CS:GO (`y.Game.CSGO`).** CSDM never sets
  `voice_enable` for CS2 — it assumes it is left to the user's cfg / defaults.
- These execs are placed at the pre-roll tick (default tick 1) via `getValidTick`.
- The per-player listening indices are bit-masks (32 players per 32-bit word):
  `function $E(t){... e=e|1<<n ...; r=r|1<<n-32; return {valueLow:e,valueHigh:r}}` → mapped to
  `tv_listen_voice_indices <valueLow>` / `tv_listen_voice_indices_h <valueHigh>`.

CSDM additionally generates **console aliases** for CS2 (not CS:GO) so you can pick voices
interactively while watching an analyzed demo (see
[Playback docs — Player voices filtering](https://cs-demo-manager.com/docs/guides/playback)):

- `voice_all` — listen to all players
- `voice_ct` — players who **started** as CT
- `voice_t` — players who **started** as T
- `voice_<SteamID64>` — a specific player

These aliases were added in
[PR #1035](https://github.com/akiver/cs-demo-manager/pull/1035) (see discussion
[#1026](https://github.com/akiver/cs-demo-manager/discussions/1026)).

> Note: `voice_ct` / `voice_t` refer to the player's **starting side**, not their side at
> the point you're watching — documented warning on the playback page.

---

## 2. Known issues: in-game voice NOT captured / not played during recording

### #966 — "Bug found: No Player Voice Recorded in Video Generation Mode" — EXACT match
https://github.com/akiver/cs-demo-manager/issues/966
- **FACEIT** demo, "generate → rounds", both encoders tried.
- Symptom identical to the user's: after the **CS2 V3.10 update**, generated round videos
  had **no player voice even with `tv_listen_voice_indices -1; tv_listen_voice_indices_h -1`**,
  while **"Watch → Round" still included player voice**.
- Reporter (CSDM 3.10.1, win32) closed it with **"Issue somehow solved"** — i.e. it went
  away on its own (a CS2-side change), with **no CSDM code change**. This is the strongest
  evidence that the voice-in-render failure is a **CS2/game bug**, not CSDM or HLAE.
- Implication for the user: even a fully correct CSDM config (`--player-voices` → the
  `tv_listen_voice_indices -1` pair) can produce no voice if the CS2 client currently
  misbehaves the way V3.10 did.

### #958 — "Can't see who is talking when watching demos (from faceit at least)"
https://github.com/akiver/cs-demo-manager/issues/958
- FACEIT demos, speaker/voice **indicators in HUD/chat** stopped showing after a CS2 update.
- This is the CS2-side **HUD indicator** bug that the docs page alludes to
  ("Player voice indicators are not displayed in the HUD"). It affects the *visual*
  indicator, and in some CS2 states the HUD can show **"voice disabled"** even when the
  underlying audio path is intact.

### #880 — "Export voice exhibits audible glitches"
https://github.com/akiver/cs-demo-manager/issues/880
- After the CS2 update of **May 29th**, the exported voice in new-version demos had
  audible glitches (old-version demos fine). Another data point that CS2 demo voice
  playback/export has been repeatedly broken by CS2 client updates.

### advancedfx (HLAE) #1182 — "Add CS2 demo voice HUD fix" (proposed, not merged)
https://github.com/advancedfx/advancedfx/pull/1182
- Adds `mirv_voiceHudFix 0|1` for CS2 demo playback **speaker icons**, plus logic to make
  speaker state recover after seeking mid-voice. **Not merged / not in a release** — just
  confirms HLAE maintainers recognize the CS2 speaker-HUD bug.

---

## 3. What is actually required for CS2 demo voice to be recorded by HLAE

### HLAE / mirv_streams audio capture
- HLAE records audio through the game's `startmovie` audio path:
  `mirv_streams record startMovieWav 1` (**default on**) — this is exactly what CSDM's
  `--record-audio` sets:
  ```
  addExecCommand(g, `mirv_streams record startMovieWav ${m.recordAudio?1:0}`)
  ```
- `startMovieWav` captures the **complete mixed game audio** (voice included) that the game
  produces. There is **no additional HLAE/mirv_streams voice-specific setting** required.
  Reference: [advancedfx wiki — Source2:mirv_streams, "Record sound"](https://github.com/advancedfx/advancedfx/wiki/Source2%3Amirv_streams).

### Therefore the real preconditions are on the *game side*:
1. **The demo must contain the voice data.** Voice is only present if the match server
   recorded it (GOTV `tv_record_voice`). PBDEMS2/FACEIT/PGL/BLAST demos do contain
   per-player voice chat (see §4).
2. **The client must actually listen to it:** `tv_listen_voice_indices -1` (+ `_h -1`),
   which CSDM sets with `--player-voices` (or run `voice_all`).
3. **The client must be emitting it into the audio mix:** `voice_enable 1`. CSDM does
   **NOT** set this for CS2 — it's the user's job (their cfg already does). If
   `voice_enable` is effectively `0` at render time, the engine outputs no voice and
   `startMovieWav` has nothing to capture — **no HLAE setting can recover it**.

### "voice disabled" HUD
- The CS2 HUD showing "voice disabled" is most consistent with either (a) the known CS2
  broken voice/speaker **indicator** (issues #958, advancedfx #1182), or (b) `voice_enable`
  genuinely being 0 in-game at render time. Because CSDM does not touch `voice_enable` for
  CS2, the user's `voice_enable 1` cfg is the only thing enforcing it — if that value isn't
  actually landing (or is being reset by the CS2 demo/sequence), voice is lost.

---

## 4. PBDEMS2 / FACEIT demo voice specifics

- **PBDEMS2** (the 7-byte header `PBDEMS2`; used by FACEIT/PGL/BLAST) is a custom demo
  wrapper that bundles **per-player voice chat** inside the demo payload. The local repo
  already encodes this assumption in `scripts/pov/render_pov.py::_is_pbdems2`
  ("PBDEMS2 demos (FACEIT/PGL/BLAST) record per-player voice chat").
- Issue #966 confirms voice **is** present and playable in FACEIT demos ("Watch → Round"
  had voice) yet was **not captured in the HLAE video render** — so the voice is in the
  file; the failure is in playback/rendering, not storage.
- The repo's own render script previously used `cl_mute_enemy_team 1`, which was found to
  **mute all voice in PBDEMS2 playback and hide indicators**; it was removed in favor of
  `voice_enable 1` + `--player-voices`. Any `cl_mute_*` cvar that silences other players
  will also kill voice capture since it stops the client from playing that voice.

---

## 5. Recommendations / workaround checklist

1. Confirm the render cfg's `voice_enable 1` is actually active during the HLAE render
   (e.g. open the game's console while the sequence runs and check `voice_enable`; verify
   no `voice_enable 0` / `cl_mute_*` is being applied after CSDM writes its sequence cfg).
2. Ensure `--player-voices` (not `--no-player-voices`) is on — CSDM's default is to push
   `tv_listen_voice_indices 0` and mute all voice. (The user already passes it.)
3. Do **not** rely on `voice_all`/`voice_ct`/`voice_t` being auto-executed during a render;
   they are interactive aliases. For a scripted render the `tv_listen_voice_indices -1`
   pair that `--player-voices` injects is the relevant mechanism.
4. If voice is missing despite everything correct, it is very likely a **CS2-side bug** that
   reappears/sticks in certain CS2 versions (see #966 — same exact symptom on a FACEIT demo,
   resolved only by a later CS2 update). Update CS2 to the latest build and re-test.
5. Verify the audio device / `startMovieWav` is producing *any* audio (music, footsteps): if
   other sound is present in the render but voice isn't, the capture path works and the
   problem is specifically that the game isn't playing voice. If *no* audio at all is
   present, check the HLAE/CS2 audio device setup (that is a separate, capture-side issue).
6. Watch for `mirv_voiceHudFix` (advancedfx #1182) once it ships if the only symptom left is
   the speaker-icon/indicator HUD.

---

## References

- CSDM `cli.js` (bundled): `C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\resources\app.asar`
- CSDM Playback docs — Player voices filtering:
  https://cs-demo-manager.com/docs/guides/playback
- Issue #966 (exact match, voice lost in video generation on FACEIT): 
  https://github.com/akiver/cs-demo-manager/issues/966
- Issue #958 (FACEIT voice/HUD indicator): https://github.com/akiver/cs-demo-manager/issues/958
- Issue #880 (voice glitches after May-29 CS2 update): https://github.com/akiver/cs-demo-manager/issues/880
- PR #1035 (CS2 voice aliases): https://github.com/akiver/cs-demo-manager/pull/1035
- Discussion #1026 (alias design): https://github.com/akiver/cs-demo-manager/discussions/1026
- HLAE Source2:mirv_streams wiki (Record sound → `startMovieWav 1`, default on):
  https://github.com/advancedfx/advancedfx/wiki/Source2%3Amirv_streams
- advancedfx PR #1182 (`mirv_voiceHudFix`, not merged):
  https://github.com/advancedfx/advancedfx/pull/1182
- Local context: `scripts/pov/render_pov.py` (BASE_FLAGS + `_write_render_autoexec`) and
  `assets/cs2_pov.cfg`
