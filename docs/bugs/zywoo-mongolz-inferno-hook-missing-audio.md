# ZywOo vs MongolZ Inferno: missing 4K and silent main POV

Investigated 2026-10-30. Run: `2399729_vitality-vs-the-mongolz-m1-inferno_ZywOo_Inferno`.
Uploaded video: `O-e--p4wHss`. Local delivery repaired on 2026-10-30;
published YouTube content has not been replaced. Original local files remain
as `video.before-audio-repair.mp4` and `video.nohook.before-audio-repair.mp4`.

## Missing 4K

Demo replay detects round-six kills at 51113, 51228, 51368, 51561: four kills
in 7 seconds. Three victims held AKs. This passes the Shorts weapon gate and
hook killfeed gate. Thumbnail log selects tick 51561 with four POV feed rows.

The unchained planner split the burst into three windows because two gaps
exceeded the two-second pre/post overlap. Fragment scoring removed its 4K tier,
letting singles outrank it. Replay using the real extracted short timeline
(LOS rows/action enrichment disabled to isolate planning) excluded it before
the fix and selects it as the climax afterward, quality 7400.

Fix: keep feed-sized 4K/5K/punch-up moments continuous. Rule version 13
invalidates old selections. Hook render version 3 also invalidates an existing
prepend even if the new selection has the same number of identical player
cfgs. Regression uses the exact real kill ticks. The real rebuilt timeline
selects a round-13 fast Glock headshot followed by the round-six 4K; the new
hook is 10.969 seconds. Four POV kills were visually confirmed on the feed.

## Missing main audio

`ffprobe`/ffmpeg inspection: retained `video.nohook.mp4` has no audio stream.
The delivered `video.mp4` has AAC but decoded samples at 20–35 seconds are
digital silence (-91 dB on volumedetect).

Pipeline log at 2026-10-28 22:14:36 reports audio remux failure. The retained
stderr tail includes backward timestamps but does not establish the terminal
failure cause. Resume at 22:15:55 accepted `video.overlay.mp4` by file size,
skipping audio remux. Hook prepend supplied silence for the missing body audio.

Fix: resume completes source audio remux before accepting existing overlay
video. Source audio mapping is mandatory, so absent audio fails loudly.
Regression covers failed remux followed by resume and a real ffmpeg source
without audio, preserving the original on failure.

Validation: 134 passed, 1 skipped across hook/audio-remux/pipeline-audio/version
tests, plus 9 passed in the final pipeline-audio/crosshair-parity check (two
overlap the first run); focused Ruff passed. The skipped test requires an
unrelated retained audio-drift fixture. Pipeline promotion and resume now
reject missing body audio, including when the final hook has its own track.
Failed audio remuxes retain their full stderr and return code in a side log.

## Recovery

Pipeline state is step 7. Its original `renders/pov-vitality-vs-the-mongolz-m1-inferno_ZywOo`
directory had been purged. All 19 exact original tick windows were recaptured
with CS2 1.41.9.3 / HLAE 2.192.10, and lossless WAVs preserved outside cleanup.
Repeated captures varied by at most three frames at round cuts. Audio is
resampled to 48 kHz and padded/trimmed only at each round tail to the original
round length, with no speed correction. The original overlay picture is
stream-copied: its actual 99,373 frames are unchanged (the sidecar's round
durations sum to 99,372, one fewer than the encoded overlay).

The completed local `video.mp4` has 100,331 frames including the new hook and
five-second silent outro. Full audio decode confirms audible sound in all 19
rounds after the final join. Audio correlation against reconstructed source
at body times 20, 800 and 1645 seconds is 0.9835/0.9883/0.9894, with constant
+21.375 ms lag at every sample, so the join adds no growing drift. Picture
comparisons across all rounds show sampled recapture offsets within 67 ms;
this is sampled alignment, not exhaustive shot-by-shot proof.

Scripts, recovered media and machine-readable reports live under
`.pi/zywoo-mongolz-repair/` (`verification.json`,
`joined-audio-verification.json`, `picture-alignment.json`, and
`recovered-rounds-verification.json`). Upload ID/status remain completed;
`rebuilt_after_upload` flags that local repair needs separate replacement
publishing. Do not delete or reset the published upload as part of local repair.
