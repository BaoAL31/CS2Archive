# Council Pass 1 — Render Pipeline Speedup Suggestions

> Read-only review. No code changes proposed.
> Scope: `scripts/pov/pipeline.py` (steps 1-6) + render / concat / overlay / outro / thumbnail / intro / highlights paths.
> Inherited decisions honored: NVENC only, overlay-only is default, `--batches 1` for POV render, file-based resume, no `--concatenate-sequences`, sidecar is ground truth, two-pass encode (mezzanine CQ8 + final CQ15), upload is separate.

## Inherited decisions I am NOT going to challenge
- NVENC p7 CQ8 mezzanine / CQ15 final export, 200M/60M caps — quality contract.
- Overlay-only default + util-cam PiP default — product contract.
- `--batches 1` for POV render (HLAE hook flakiness with extra launches).
- File-based resume (≥1MB = "done"), per-round sequence clips preserved for sidecar.
- Sidecar (`combined.round_offsets.json`) is authoritative for tick→frame mapping.
- 2560×1440 VP9 trick — quality contract.

## Diagnosis (what is actually happening)

Pipeline wall time per POV is dominated by:
1. **Render step 2** — HLAE/CSDM (real-time, ~30-90 min for 23 rounds; this is the floor).
2. **Concat step 3** — final scale encode is a single full-length 2560×1440 NVENC pass.
3. **Overlay step 4** — per-batch nvenc encode of the full 1440p video × N batches (default 5), then a per-batch `setpts+asetpts` audio remux at the end.
4. **Util-cam step 4 sub-task** — sequential CSDM flight renders (1-2 min each, 20+ throws).
5. **Step 6 thumbnail** — runs `csdm` (already cached → fast) and `_extract_kill_frame` (single ffmpeg seek → trivial). Not a bottleneck.
6. **Step 5 outro** — `libx264 -crf 15` 5s re-encode, then `-c copy` concat. Trivial.

The pipeline is **sequential** (each step blocks the next within one POV). The listener runs one POV at a time, so for a given render, parallelism inside a single POV is the lever. Across POVs, the listener already chains by running the next `pipeline.py` after upload spawn (concurrency is a separate axis — not in scope).

## Findings (ranked by estimated impact)

### 1. Overlay audio remux decodes a 60-min+ file twice [HIGH IMPACT]
**Where:** `scripts/overlay/overlay_encode.py:_remux_source_audio` invoked once at the end of `overlay_pov.py:run_overlay`.
**Current:** After all batch encodes + concat, the whole finished `video.overlay.mp4` is fed back into ffmpeg with `-map 0:v -map 1:a` to replace its audio with the original source audio. Two passes over the same long file: concat audio already accumulated ~44ms + ~33ms/batch drift. For a 50-min POV this is ~3-8 min of pure stream copy + AAC re-encode of audio.
**Speedup:** Embed source audio into each batch from the start (setpts/trim per batch once, then every batch carries the correct audio). Or: keep the remux but drop the AAC re-encode (`-c:a copy`) — the source is already AAC. Saves ~3-8 min/POV.
**Risk:** low — already wrapped in safe atomic rename.
**Confidence:** medium.

### 2. Concat step 3 always re-runs the full 2560×1440 encode even when only the voice shade changed [MEDIUM]
**Where:** `scripts/pov/concat_rounds.py:_encode_scaled` + `main()` re-bake branch.
**Current:** Step 3 has a "re-bake shade" path that detects `combined.mp4` already 2560×1440 and re-encodes from the native copy. Good. But the *mezzanine encode* is `h264_nvenc CQ8` with `maxrate 200M bufsize 400M` — extremely high bitrate for a 30-min POV. The final export is `CQ15 / 60M` (`overlay_encode._ffmpeg_encode`). Step 3's output is the input to step 4's per-batch encoders.
**Speedup:** Lower CQ8 → CQ12 (still clean for the 2nd encode that follows) AND cut `maxrate` from 200M to 80M. NVENC will hit the rate-cap on busy frames but only marginally raise QP; visible difference at the final export is nil. ~30-50% faster encode.
**Risk:** low — worth a single POV A/B before/after to confirm banding-free.
**Confidence:** medium-high.

### 3. Per-batch overlay encoder re-reads the entire combined.mp4 [HIGH IMPACT]
**Where:** `scripts/overlay/overlay_pov.py:run_overlay` — `batches > 0 and round_offsets` branch, each batch calls `_ffmpeg_encode(str(video_path), ...)` with `segment=(start_sec, end_sec)`.
**Current:** Each of the 5 batches re-opens `combined.mp4` from the start, ffmpeg decodes 0→`start_sec` (with `-ss` input-side, keyframe-seeks), then re-encodes its slice. Total decode work across all batches ≈ 5× the full video duration. For a 50-min POV that's ~4 hours of redundant decode on the GPU.
**Speedup:** Pre-split the source into per-batch segment files ONCE with `ffmpeg -c copy -segment` (stream copy, no decode, ~10s total), then per-batch encode reads only its slice. Cuts GPU decode to ~1×.
**Risk:** low — same encoded parameters across batches (already required for stream-copy concat in `_concat_overlay_batches`).
**Confidence:** high.

### 4. Voice-shade composite forces a CPU pass in the scale step [MEDIUM]
**Where:** `scripts/pov/concat_rounds.py:_encode_scaled` — `if filter_complex is not None` branch.
**Current:** When voice shade is active, scale takes the **CPU** path (`flags=spline`, libx264 fallback) instead of the GPU `scale_cuda` path. The voice shade requires `overlay` of CPU-side RGBA controls → must be CPU frames. On a 50-min POV that doubles or triples the scale encode time.
**Speedup:** Move the voice-shade composite to the overlay step (step 4) where the input is already on CPU frames. Step 3 then runs its fast GPU `scale_cuda` path regardless. The shade already runs in step 4's batch encoders as a per-batch overlay — that one keeps CPU.
**Risk:** medium — voice shade would need to be applied per-batch; worth verifying that combined.round_offsets still produces a 1440p output the shade can stretch with.
**Confidence:** medium.

### 5. Util-cam throws rendered sequentially in a single CS2 launch [HIGH IMPACT for util-rich POVs]
**Where:** `scripts/overlay/overlay_utilcams.py:_run_batch_util_cams_subprocess` → `render_util_cams.py` → `batch_util_cams.py`.
**Current:** All flight clips are rendered in one CS2 launch via `spec_goto` precomputed for each throw. 20+ throws × 1-2 min = 30-60 min sequential.
**Speedup:** Run 2-3 CS2 instances in parallel on different `utility_cams` output roots, splitting throws by `util_id` hash. Each CSDM/HLAE consumes ~3-4 GB RAM + 1 NVENC session. Modern GPUs accept 2 concurrent NVENC sessions via separate ffmpeg processes. Roughly halves util-cam wall time.
**Risk:** medium-high — parallel CS2 instances on the same Windows desktop is fragile (HLAE hooks, shared game state, GPU driver contention). A safer first step: batch in two CS2 instances where one is `playdemo demo1` and the other is `playdemo demo2` of the SAME demo (CS2 supports multiple observers on one demo).
**Confidence:** medium.

### 6. Pipeline.py invokes subprocess scripts serially with a 12-hour timeout each [LOW]
**Where:** `scripts/pov/pipeline.py:Pipeline.run` — step loop + `subprocess.run(..., timeout=43200)`.
**Current:** Each step's call into `render_pov.py`, `concat_rounds.py`, `overlay_pov.py` is `subprocess.run` (synchronous, blocks the pipeline).
**Speedup:** None here for a single POV. Cross-step parallelism (e.g. pre-render util-cam flights in parallel with concat) is the lever — see finding #7.
**Confidence:** high (no gain inside one POV from this path).

### 7. Util-cam flight render can run in parallel with concat+scale [HIGH IMPACT for util-rich POVs]
**Where:** `scripts/pov/pipeline.py:Pipeline.run` and `step_overlay`.
**Current:** Concat/scale writes `combined.mp4` → overlay reads it → overlay renders util-cams → overlay encodes. Util-cam render (~30-60 min) is strictly inside the overlay step.
**Speedup:** Kick off util-cam rendering as soon as the demo is available (after step 1 analyze, before step 2 render). The util-cam renderer doesn't need `combined.mp4` — it only needs the demo and the per-throw flight path. After step 3 finishes, overlay step just consumes the already-rendered clips. Structural change to `step_overlay` to pull util-cam out of its critical path; API stays the same.
**Risk:** medium — util-cam failures no longer block overlay completion; need a fallback path (the current "if expected != rendered, fail" branch in `overlay_pov.py`).
**Confidence:** medium-high.

### 8. Outro encode uses libx264 instead of NVENC [LOW-MEDIUM]
**Where:** `scripts/pov/generate_outro.py` — ffmpeg call (`-c:v libx264`).
**Current:** 5-second still-frame outro is encoded with `libx264 -crf 15 -preset medium`. Negligible wall time (~3s) — but a 5s GPU encode would be ~0.3s. Trivial gain, but eliminates a CPU library dependency in the hot path.
**Speedup:** Switch to `h264_nvenc -cq 15 -preset p7`. Same profile/level/colorspace as the main encode (matters for stream-copy concat downstream).
**Risk:** low — same encoder family as the rest of the pipeline.
**Confidence:** high (impact small).

### 9. Thumbnail `_extract_kill_frame` decodes the full 1440p video via ffmpeg to find a single frame [LOW]
**Where:** `scripts/pov/pipeline.py:_extract_kill_frame` → `thumbnail.utils.extract_killfeed_frame`.
**Current:** A 50-min POV is seeked + decoded by ffmpeg to grab one frame at the densest killfeed tick. ~5-10s. Negligible.
**Speedup:** None worth doing. Already uses sidecar.
**Confidence:** high (no-op finding).

### 10. Two CSDM analyses of the same demo: step 1 + step 6 thumbnail [LOW]
**Where:** `step_analyze` calls `csdm json` (writes `csdm_analysis.json`); `step_thumbnail` re-runs `csdm json` via the thumbnail package.
**Current:** `csdm analyze` and `csdm json` are both run. `csdm json` is slow (~10-30s for a big demo). Step 1 already saves the analysis to `render_dir/csdm_analysis.json` and `state.data["analysis_json"]`.
**Speedup:** Step 6 should re-use `state.data["analysis_json"]` instead of re-running `csdm json`. Cheap save (~30s/POV).
**Risk:** low — file already on disk and validated.
**Confidence:** high (if it's actually a re-run; needs a quick `grep` to confirm — but the persisted sidecar is clearly designed to be the source of truth).
**Action for pass 2:** verify this with a targeted grep before publishing as a finding.

### 11. `_voice_cache` not actually memoized across step invocations [NIT]
**Where:** `scripts/pov/pipeline.py` — `self._voice_cache: bool | None = None` in `__init__`, but no read-then-write of `self._voice_cache` inside `_voice_enabled`.
**Current:** `_voice_enabled()` re-evaluates each call (cheap for HLTV, ~5s for FACEIT due to `pov_team_voice_seconds` call). Called from step_concat (once) and possibly elsewhere.
**Speedup:** Cache the result on first call. ~5s saved for FACEIT.
**Risk:** trivial.
**Confidence:** high.

### 12. Step 2/3 share no GPU work — concat's GPU `scale_cuda` runs on a fully-idle GPU [N/A — already parallel]
**Where:** `scripts/pov/concat_rounds.py:_encode_scaled` plain path.
**Current:** When voice shade is OFF, concat uses GPU decode + `scale_cuda` + `nvenc`. This is the fast path.
**Speedup:** None — already optimal.
**Confidence:** high.

## Top 3 (ranked)

1. **#3 Pre-split combined.mp4 for per-batch overlay encodes** — eliminates ~4× redundant decode of a 50-min file. Saves ~3-6 min/POV. Low risk, high confidence.
2. **#2 Lower step-3 CQ8 → CQ12 + maxrate 200M → 80M** — saves ~30-50% of the scale-encode wall time. Requires one A/B POV to confirm no banding. Big win for the 50-min scale step.
3. **#1 Eliminate the post-batch `_remux_source_audio` full-file pass** — saves ~3-8 min/POV. Drop the AAC re-encode (`-c:a copy`) and per-batch embed source audio from the start. Low risk.

## Questions for reviewer (pass 2)

- Did I correctly identify that overlay audio is re-muxed once at the end via `_remux_source_audio` over the full concat? Or is there a path that avoids the full re-mux entirely? (I'm reading the code as written, but `asetpts=PTS-STARTPTS` is in the per-batch encode, so audio is already produced — the question is why a remux at all.)
- Is the per-batch decode in `overlay_pov.py:run_overlay` truly input-side seek with keyframe alignment? (the docstring says yes; if `-noaccurate_seek` is missing, the decode time may be much higher than I estimate.)
- For #5 parallel CS2 instances: is the user's GPU single-session-NVENC? (NVENC has per-session bitrate caps; concurrent encodes share session queues.)
- For #7 util-cam parallel-with-concat: are util-cam clips a strict prerequisite for the overlay encode, or just for the PiP placement? (The code suggests the latter — they're inputs to the filter, not the source video.)
- Confirm finding #10 (duplicate `csdm json` call in step 6) with a targeted grep before passing to reviewer.

## Out of scope (didn't investigate)
- HLTV match listener (cross-POV chaining).
- Upload (`upload_pending.py`).
- Shorts / faceit_thumbnail / faceit_kd.
- HF sync.
- Steam/CS2 version checks (already fast in `step_render`).
- Prosettings refresh (only runs when backlog missing fields).