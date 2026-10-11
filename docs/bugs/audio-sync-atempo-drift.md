# Recurring POV audio delay: unsafe automatic correction / atempo drift

## Status

Reproduced on retained footage. Patch implemented. Focused suite: **11 passed**, including the opt-in retained-capture regression; targeted Ruff and whitespace checks pass. Additional overlay integration suite: **9 passed** (20 across both runs). Review copy verified: 0.000 ms added audio lag at all three windows, correlations 0.9988/0.9995/0.9998. Original and review video metadata match (2560x1440, 110398 frames, 1839.966016 s). Selected-shot picture/audio inspection completed at rounds 2, 10 and 19 using 20 fps contact sheets and 10 ms audio envelopes: source and source-timing review recoil/ammo transitions coincide with corresponding shot transients to within the ~50 ms inspection resolution. No large source-side delay or overlay-picture shift was observed in those samples. This is sampled inspection, not exhaustive proof for every shot.
This identifies overlay-added delay, not yet all capture-side A/V delay reported by the user.

- `overlay_pov.py`: automatic kill-onset offset/rate corrections disabled; preserve source timing.
- `overlay_encode.py`: explicitly supplied rate corrections use normalized 48 kHz asetrate/aresample instead of atempo. Integer sample-rate rounding limits precision to ~10.4 ppm; pitch changes by the same small factor.
- Real-capture regression (opt-in via `CS2ARCHIVE_AUDIO_DRIFT_FIXTURE`) failed old code: +24.750 ms measured vs -3.284 ms requested at 84.2 s. The synthetic pulse test passed old code and is not evidence of reproducing the capture defect.
- Review-only pre-hook video built and verified at `.pi/audio-drift-probe/zywoo.source-timing.nohook.mp4`; original video and upload are unchanged. Machine-readable verification: `.pi/audio-drift-probe/review-verification.json`.

## Recurrence and what must not be forgotten

Late audio was reported across ZywOo, donk and earlier flameZ POVs. The symptom is recurring; the controlled filter reproduction below proves this mechanism for ZywOo only. Do not assume every future A/V failure has the same cause.

The previous diagnosis repeatedly treated noisy fitted signs as ground truth, added more robust statistics, then proposed a constant trim. Neither a better slope estimator nor a guessed trim establishes that the selected audio transient belongs to the visible shot. A correction that looks mathematically plausible can worsen the delivered video. Short pulse tests passed the broken implementation, so they did not close this bug.

**Production guardrails:**

1. Preserve source audio timing by default. Do not silently re-enable kill-onset offset/rate correction.
2. Never infer "audio early" from a negative fit when visible/audible evidence shows it late.
3. Do not use near-unity `atempo` for this capture correction without a real-content long-form regression. Explicit tiny corrections use rate resampling, which also changes pitch by that tiny factor.
4. Do not trim AAC priming as a generic sync fix; this muxer tags it. Do not stack a constant shift on an unvalidated fit.
5. Separate capture, concat, overlay, hook/intro and upload stages. Waveform correlation checks audio changes; it must be paired with visible recoil/ammo timing to detect picture changes.
6. Do not delete retained sources, sidecars or repair copies during investigation. Check pipeline progress first. Render cleanup can run after upload, so preserve diagnostic artifacts outside `renders/` before relying on them.

**Gate before re-enabling automatic correction:** independent visible-shot/matching-audio landmarks across early/middle/late rounds, demonstrated correct sign and magnitude, a real-capture regression that fails old behavior, no increasing residual delay in repaired output, and investigation of any round-boundary discontinuities. Synthetic-only green tests are insufficient.

## Reproduction (2026-10-11 investigation)

Source: `renders/pov-aurora-vs-vitality-m2-cache_ZywOo/combined.mp4`.
Delivered stages: `youtube/2398754_aurora-vs-vitality-m2-cache_ZywOo_Cache_overlay/video.nohook.mp4` and `video.mp4`.
Pipeline log records `atempo 1.000039 applied`.

Cross-correlate 12-second decoded mono 8 kHz audio windows against a 16-second search window, allowing +/-2 seconds. Positive lag means audio is later than source. Round sidecars account for the hook prefix.

| Source time | Pre-hook overlay added delay | Final added delay |
|---:|---:|---:|
| 84.200 s | +24.750 ms | +46.125 ms |
| 972.930 s | +167.125 ms | +188.500 ms |
| 1657.511 s | +314.500 ms | +335.875 ms |

Correlations: 0.940–0.993. Hook adds a constant ~21.375 ms relative to the sidecar shift; it does not explain growing delay.

## Controlled filter reproduction

Encode retained source audio to AAC 256k / 48 kHz stereo, no video:

- `volume=0.85,atempo=1.000039`: reproduces the pre-hook delays exactly.
- `volume=0.85`: 0.000 ms added lag at all three windows, correlations >0.998.
- `asetpts=PTS-STARTPTS,volume=0.85,atempo=1.000039`: same bad delays; resetting timestamps does not cure it.
- `asetpts=PTS-STARTPTS,volume=0.85,aresample=48000,asetrate=48002,aresample=48000`: -3.625 / -40.750 / -69.375 ms, consistent with intentional +41.667 ppm advancement, not added delay.

Conclusion: the `atempo` path adds drift on this retained source even though the requested factor is above 1. The internal ffmpeg/WSOLA mechanism has not been diagnosed; do not call it a confirmed ffmpeg-wide bug. Rate resampling is a deterministic alternative for these tiny corrections, with negligible pitch shift. It does not make untrusted fitted rates safe.

## Fitter is not an A/V ground truth

Current fitter uses kill ticks projected through sidecars and the first amplitude-threshold crossing in a +/-0.7 s audio window. It can select earlier unrelated audio and cannot see +1.1 s delay outside that window. Robust slope estimation cannot repair systematically incorrect landmarks. Current fits on retained sources:

- ZywOo: offset -0.172671 s, slope +0.039259 ms/s, 15 marks.
- donk: offset -0.106098 s, slope -0.224868 ms/s, 9 marks.

These do not invalidate the user's audible late-audio report. Do not automatically pad or slow production audio based on them. Actual capture A/V timing needs visible shot onset matched to the corresponding audible shot.

## Picture/shot inspection and retained artifacts

- Round 2 Deagle: projected shot 110.715625 s.
- Round 10 AWP: projected shot 1002.398750 s.
- Round 19 AWP: projected shot 1717.245375 s.
- Contact sheets: `.pi/audio-drift-probe/{source,review}-round-{2,10,19}-shots.jpg`.
- Source render directory disappeared during investigation (external purge, not deleted by this investigation). The pre-hook sidecar in `youtube/` provided the identical round mapping for review inspection. Baseline decoded/re-encoded source audio survives as `.pi/audio-drift-probe/plain.m4a`; review video and evidence survive outside renders.
- Neither the original delivered video nor the uploaded video was replaced by this investigation. A pre-hook review is not a full hook/outro replacement upload.

## Regression and recovery procedure

Run focused tests in the installed project environment:

```cmd
set "CS2ARCHIVE_AUDIO_DRIFT_FIXTURE=D:\Projects\CS2Archive\renders\pov-aurora-vs-vitality-m2-cache_ZywOo\combined.mp4"
python -m pytest tests/test_audio_sync.py tests/test_audio_sync_remux.py -q
python -m pytest tests/test_overlay_audio_copy.py tests/test_overlay_sync.py tests/test_overlay_round_starts.py tests/test_overlay_gop.py -q
```

Use an available retained capture or an audio-only copy as the fixture; the original source above was subsequently purged externally. The opt-in real-capture regression is skipped when the environment variable is absent, so a green ordinary CI run is not evidence that real footage was tested. The exact ZywOo source was used for the recorded failing/passing runs. Its source-timing audio survives locally in `plain.m4a` under the probe directory.

For recurrence:

1. Identify exact match/player, pipeline state, source, final file, sidecars, and logged correction. Do not trust stale handoff match names.
2. Save representative capture audio and sidecars outside cleanup-managed folders. Keep originals unchanged.
3. Compare source vs pre-hook vs final audio windows at several positions, accounting for prefixes and freezes. Report positive lag as late audio. Require a strong, unambiguous match.
4. Match visible ammo decrement/recoil with the corresponding audio transient on those stages, not merely a kill timestamp or the earliest loud sound in a window.
5. If this defect recurs, build a new review copy using source audio, plain volume normalization and AAC 48 kHz stereo. Keep video stream-copied and pad only the tail to avoid truncation. Do not apply a guessed global offset.
6. Verify review waveform timing, visible shot timing, duration and frame count. The user reviewed the source-timing copy and confirmed it was correct.
7. Rebuild hook/intro/outro into a separate final candidate, validate joins, then explicitly coordinate replacement publishing. Local file repair does not change an already-uploaded YouTube video. No uploaded video was deleted or replaced during this investigation.

## Evidence

Background task outputs in the active pi session:

- `bea33ac2f`: stage probes and waveform correlations.
- `b7d029b42`: with/without atempo reproduction.
- `bd359a572`: timestamp reset and deterministic resampling comparison.

Diagnostic encodes live in `.pi/audio-drift-probe/`; production files and upload `ITtM7qd4O9k` were not modified by this investigation.
