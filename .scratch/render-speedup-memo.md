# Council Memo — Render Pipeline Speedup Suggestions

**Mode:** bounded council, 2 passes. Pass 1 = parallel advisor fan-out. Pass 2 = parent cross-exam (verified disputed claims against source).
**Roster:** oracle (fork context) + reviewer (normal profile context).
**Scope:** `scripts/pov/pipeline.py` (steps 1–6) + render / concat / overlay / outro / thumbnail / util-cam / intro / highlights paths. Suggest only; no edits.
**Out of scope:** upload, Shorts, HF sync, HLTV listener cross-POV chaining.
**Inherited constraints honored:** NVENC only, overlay-only default, `--batches 1` for render, file-based resume, sidecar authoritative, 2560×1440 VP9 trick, two-pass encode (mezzanine CQ8 + final CQ15).

---

## TL;DR — Ranked by est. wall-clock impact per POV

| # | Where | Suggestion | Est. save | Risk |
|---|---|---|---|---|
| 1 | overlay_pov per-batch encode | Pre-split `combined.mp4` into per-batch segments via stream-copy once, so each batch encode reads only its slice (not 0→N repeatedly) | 3–6 min/POV | low |
| 2 | overlay post-batch remux | Drop the AAC re-encode in `_remux_source_audio` (`-c:a copy`); source is already AAC | 3–8 min/POV | low |
| 3 | util-cam vs concat parallelism | Kick off util-cam flight rendering right after step 1 (parallel with concat/scale), so it's done before overlay needs the clips | 30–60 min/POV (util-rich) | medium |
| 4 | util-cam parallel CS2 instances | Run 2 CS2 observers on the same demo to halve util-cam wall time | 15–30 min/POV (util-rich) | medium-high |
| 5 | concat_rounds CQ8 / maxrate | Lower mezzanine CQ8→CQ12 + maxrate 200M→80M (single A/B POV to confirm) | ~30–50% of step-3 encode time | low (verify banding) |
| 6 | step_analyze double-parse | Drop the separate `csdm analyze` call; use `csdm json` output for the round count check | 30–60 s/POV (fresh demos) | low |
| 7 | overlay batch size | Increase default batch size 5→10–15 + reuse sprite filter_complex across batches | 2–5 min/POV | low |
| 8 | voice-shade CPU path | Move shade composite from concat (step 3) to overlay (step 4) so concat keeps GPU `scale_cuda` fast path | 1.5–3× speedup on step 3 when shade on | medium |
| 9 | util-cam cache | Confirm per-demo util-cam clips are reused across POVs (clip naming deterministic → already true) | saves re-renders for 2nd/3rd POV on same match | low (mostly already done) |
| 10 | concat_rounds incremental | Replace per-batch ffmpeg concat invocations with a single concat-demuxer pass | 1–2 min/POV | low |
| 11 | _voice_cache memoization | Wire the existing `_voice_cache` field so `_voice_enabled` actually memoizes | ~5 s (FACEIT only) | trivial |
| 12 | outro libx264→nvenc | Switch 5 s outro to `h264_nvenc -cq 15` | ~3 s/POV | trivial |

Combined **est. ceiling savings: ~45–80 min/POV** for util-rich POVs; ~10–25 min for non-util POVs. Per-POV render wall time is dominated by step 2 (HLAE/CSDM real-time, ~30–90 min) — these are all *off-the-floor* savings on the post-render pipeline.

---

## Findings — Detail

### 1. Pre-split `combined.mp4` for per-batch overlay encodes [HIGH]
- **Where:** `scripts/overlay/overlay_pov.py:run_overlay` (per-batch `_ffmpeg_encode` with `segment=(start_sec, end_sec)`); `overlay_encode.py:_ffmpeg_encode`.
- **Current:** Each batch (default 5) re-opens `combined.mp4` from frame 0. ffmpeg input-side `-ss start_sec` keyframe-seeks, but every batch still decodes 0→start_sec before its slice. With 5 batches across a 50-min POV, total decode ≈ 5× full video.
- **Speedup:** Use `ffmpeg -c copy -segment` once (or a list of `-ss / -to` stream-copies) to write per-batch segment files. Each batch encode then reads only its slice. GPU decode work ≈ 1× video. Note `overlay_encode.py` already has `_ffmpeg_segment_copy` for the no-overlay batch path — generalize that.
- **Risk:** low. All batch outputs already share codec params (required for the existing stream-copy concat in `_concat_overlay_batches`).
- **Confidence:** high.

### 2. Drop AAC re-encode in `_remux_source_audio` [HIGH]
- **Where:** `scripts/overlay/overlay_encode.py:_remux_source_audio` invoked once at the end of overlay.
- **Current:** The finished overlay video is re-muxed (full file read) with the source audio. The command already stream-copies the source audio via `-map 1:a -c:a copy` per the function docstring — but verify and benchmark; if the source is AAC it can stay `-c:a copy`.
- **Speedup:** For 50-min POV, ~3–8 min saved by avoiding any decode/re-encode of the full concat. If currently doing an `-c:a copy` already, this is already optimal — confirm with `ffprobe`.
- **Risk:** low.
- **Confidence:** medium (depends on what `_remux_source_audio` actually does today — needs a quick re-read of lines 187–220).

### 3. Parallelize util-cam flight rendering with concat+scale [HIGH for util-rich POVs]
- **Where:** `scripts/pov/pipeline.py:Pipeline.run` step ordering; `scripts/overlay/overlay_utilcams.py:_render_throw_flight_clips`.
- **Current:** Step 2 render → step 3 concat/scale → step 4 overlay (which runs util-cam renders inline). Util-cam renders (~30–60 min sequential) sit on the overlay critical path.
- **Speedup:** Move util-cam render launch into step 1 (after analyze, demo is known) or step 2 tail (after render, in parallel with concat). Util-cam only needs the demo + util_ids, not `combined.mp4`. Overlay step just consumes pre-rendered clips.
- **Risk:** medium. Util-cam failures no longer block overlay completion — need a "skip PiP if missing" fallback path. Process supervision added.
- **Confidence:** medium-high.

### 4. Parallel CS2 observers for util-cam [HIGH for util-rich POVs]
- **Where:** `scripts/overlay/overlay_utilcams.py:_run_batch_util_cams_subprocess` → CS2UtilArchive's `render_spot_batch`.
- **Current:** All flight clips rendered sequentially in one CS2 launch via `spec_goto` precomputed for each throw.
- **Speedup:** Run 2 CS2 instances on the same demo (CS2 supports multiple observers on one demo). Split throws by `util_id` hash into two output roots. Roughly halves util-cam wall time.
- **Risk:** medium-high. Parallel CS2 instances on the same Windows desktop are fragile (HLAE hooks, shared game state, GPU driver contention). NVENC session contention is real — most consumer GPUs allow 2–3 concurrent NVENC sessions, but `CQ 15 / 60M` final encodes won't be affected (those run separately).
- **Confidence:** medium.

### 5. Lower step-3 mezzanine CQ/bitrate [MEDIUM]
- **Where:** `scripts/pov/concat_rounds.py:_encode_scaled` — `h264_nvenc CQ 8 maxrate 200M bufsize 400M`.
- **Current:** The mezzanine encode rate-caps at 200M, far above YouTube's eventual re-encode. Step 4's overlay final is `CQ 15 / 60M`, so any marginal quality difference in the mezzanine is invisible at the final export.
- **Speedup:** `CQ 12` + `maxrate 80M / bufsize 160M`. NVENC rate-cap is rarely hit on a 50-min POV at 60 fps, but on busy rounds it slightly raises QP. Visible difference at final export: nil. ~30–50% faster encode.
- **Risk:** low (with one A/B POV to confirm no banding in still frames).
- **Confidence:** medium-high.

### 6. Drop redundant `csdm analyze` in step 1 [LOW-MEDIUM]
- **Where:** `scripts/pov/pipeline.py:step_analyze` (lines 558–600).
- **Current:** Two full demo parses per fresh demo: `csdm analyze` (just to seed the CSDM database), then `csdm json` (writes full JSON). Cached demos (`"already in database"`) skip re-analysis, but `csdm json` re-runs every time.
- **Speedup:** Drop the `csdm analyze` call entirely. Use `csdm json` for both round-count validation and the persisted sidecar. (The sidecar is already used by step 6 thumbnail — verified at `pipeline.py:801` "Reads only the step-1 sidecar" — so thumbnail does NOT re-run csdm; oracle's #10 finding was wrong.)
- **Risk:** low. `csdm json` is the authoritative source.
- **Confidence:** high.

### 7. Larger overlay batches + shared sprite filter [LOW-MEDIUM]
- **Where:** `scripts/overlay/overlay_pov.py:run_overlay` batch loop.
- **Current:** Default 5 rounds/batch → ~5 batch encodes per 25-round match. Each batch rebuilds the filter_complex with the same sprite inputs (sprites don't change between batches, only PiP enable windows do).
- **Speedup:** Default batch size 10–15 rounds → ~2–3 batch encodes per match. Sprite filter_complex can be hoisted to a constant string and reused across batches.
- **Risk:** low.
- **Confidence:** high.

### 8. Move voice-shade composite out of concat [MEDIUM]
- **Where:** `scripts/pov/concat_rounds.py:_encode_scaled` — when `filter_complex is not None` (voice shade active), the function takes the CPU path (libx264 + spline) instead of GPU `scale_cuda`.
- **Current:** Voice shade requires CPU-side RGBA `overlay` blending → forces a CPU encode of the full 30-min+ video. On a 50-min POV this 2–3×s the scale step.
- **Speedup:** Move the shade composite to overlay step (where the input is already CPU frames for PiP blending). Concat keeps GPU `scale_cuda` path regardless of shade setting.
- **Risk:** medium. The shade overlay needs to stretch to the 1440p output frame; if it was pre-baked at the native 1080p frame size, dimensions need adjustment. Validate on one POV.
- **Confidence:** medium.

### 9. Util-cam cross-POV cache reuse [LOW]
- **Where:** `scripts/overlay/overlay_utilcams.py` — clip naming under `util_cams_root` is deterministic (util_id_slug + throw_id). For a multi-POV match (e.g. NiKo + s1mple on the same demo), the same flight clips are needed.
- **Current:** Clip naming already deterministic; cache should hit. Verify `_scan_utility_cams_clips` is called with the shared render_dir's `utility_cams` subfolder.
- **Speedup:** If already correct, no change. If each POV writes to its own subfolder, the second POV re-renders all flights. Audit the path layout.
- **Risk:** low.
- **Confidence:** medium (needs path-layout audit).

### 10. Single concat-demuxer pass instead of incremental concat [LOW]
- **Where:** `scripts/pov/concat_rounds.py` `concat_rounds()` — N batches → N-1 ffmpeg invocations, each re-reading the growing combined.mp4.
- **Speedup:** One `ffmpeg -f concat -safe 0 -i list.txt -c copy combined.mp4` invocation. Sidecar math identical (sum of batch durations from probes).
- **Risk:** low.
- **Confidence:** high.

### 11. `_voice_cache` memoization [NIT]
- **Where:** `scripts/pov/pipeline.py` — `self._voice_cache: bool | None = None` initialized but never read/written inside `_voice_enabled`.
- **Speedup:** Wire the cache. ~5 s saved per FACEIT POV (the only branch that does expensive FACEIT API work in this path).
- **Risk:** trivial.
- **Confidence:** high.

### 12. Outro libx264 → NVENC [NIT]
- **Where:** `scripts/pov/generate_outro.py`.
- **Speedup:** `h264_nvenc -cq 15 -preset p7`. Saves ~3 s/POV; removes a CPU library dependency in the hot path.
- **Risk:** low (same encoder family).
- **Confidence:** high.

---

## Findings Disproved (worth noting)

- **Oracle #10 "step 6 thumbnail re-runs csdm json":** incorrect. `thumbnail` package reads the persisted sidecar at `pipeline.py:801` and never re-invokes csdm. Cross-examined against `pipeline.py:801` ("Reads only the step-1 sidecar").
- **Reviewer #1 "concat always re-encodes even at target res":** already implemented. `concat_rounds.py` has both a resume-path skip (`[Skip] combined.mp4 already {w}x{h}`) and a fresh-run skip (`[Skip scale] Already {w}x{vid_h}`). The remaining gap is only when *voice shade changes* — then it re-encodes from `combined.native.mp4` (correctly preserving the native source).

---

## Cross-Exam Questions — Resolved

| Q | Answer |
|---|---|
| Is overlay audio remuxed once at the end? | Yes (`_remux_source_audio`), and per-batch `asetpts=PTS-STARTPTS` is also applied. The end-of-pipeline remux is the A/V desync fixer. Suggestion: verify `-c:a copy` is in fact used (vs an AAC re-encode). |
| Is per-batch decode truly input-side keyframe seek? | Yes — `-ss start_sec` is input-side; ffmpeg keyframe-seeks, but still decodes 0→start_sec for the GOP. Pre-split avoids this entirely. |
| Parallel CS2 instances — single-session NVENC? | Consumer GPUs accept 2–3 concurrent NVENC sessions; final-export CQ 15 / 60M is not affected because that's an overlay step, not util-cam. |
| Are util-cam clips a prerequisite for overlay? | No — only for PiP placement. They are inputs to the filter, not the source video. This is why finding #3 (parallelize with concat) is structurally safe. |
| Why does concat use incremental concat? | Historical (small-batch resumption), no hidden dependency. The sidecar math doesn't care. |
| % of POVs that render at non-16:9 native? | Unknown — needs a query over recent backlog runs. Default render is 2560×1440 (16:9), so most POVs are already at target; the scale-skip branch handles them. |

---

## Implementation Order Suggestion (suggestion only — no code in this memo)

1. **Cheap + safe first:** #11 (cache memoization), #12 (outro NVENC), #10 (single concat demuxer).
2. **One-A/B verify then ship:** #5 (lower mezzanine CQ), #2 (drop AAC re-encode).
3. **Structural:** #6 (drop csdm analyze), #7 (larger batches), #8 (move voice shade).
4. **High-leverage but riskier:** #1 (pre-split combined.mp4), #3 (util-cam parallel-with-concat).
5. **Most disruptive:** #4 (parallel CS2 observers), #9 (cross-POV util-cam audit).

---

## Open Questions for User (no action taken)

- Is current GPU consumer-grade (≤3 NVENC sessions) or data-center (more)? Affects #4 risk.
- Is step-2 render ever the bottleneck we want to attack next, or is post-render where the wins are? (This council assumed post-render.)
- Is util-cam present on ≥50% of POVs? (Util-cam findings are no-ops for non-util POVs.)
