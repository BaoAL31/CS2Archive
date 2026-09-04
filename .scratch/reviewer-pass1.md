Now I have a thorough understanding of the pipeline. Let me compile the findings.

```json
{
  "verdict": "ok",
  "findings": [
    {
      "step": "concat",
      "where": "scripts/pov/concat_rounds.py:580-650 (_encode_scaled, main)",
      "current": "After stream-copy concatenating batch files, concat_rounds.py ALWAYS does a full GPU re-encode (scale_cuda lanczos + NVENC CQ 8, 200M cap) to 2560x1440, even when render_pov.py already output at 2560x1440. This is a complete generation-loss + time waste for 16:9 native renders.",
      "speedup": "Skip the scale/encode pass when render resolution == target resolution (2560x1440). Detect via ffprobe on first batch file; if (w,h) == (2560,1440), stream-copy concat all batches directly to final combined.mp4 and write sidecar. Only run _encode_scaled when upscale/stretch is actually needed (e.g. 4:3 or 16:10 native).",
      "est_impact": "large - eliminates 1 full re-encode of 20-40 min 1440p60 video (~5-15 min GPU time per POV)",
      "risk": "low - sidecar generation already happens during stream-copy concat phase; combined.native.mp4 preservation logic adapts trivially. Only affects code path when render resolution matches target.",
      "confidence": "high"
    },
    {
      "step": "render",
      "where": "scripts/pov/render_pov.py:500-580 (main loop, --batches default=1)",
      "current": "Default --batches=1 renders all rounds sequentially in one CS2/HLAE launch. While this minimizes hook-failure risk, it serializes 20-30 rounds on a single GPU context. No parallelism across rounds.",
      "speedup": "Allow --batches > 1 as default for stable demos (e.g. 3-4 batches). Each batch launches fresh CS2/HLAE but runs in parallel if multiple GPUs available, or at least isolates hook failures to one batch. Add a heuristic: if demo has >20 rounds and no prior hook failures, default to --batches=3. Keep --batches=1 opt-in for flaky demos.",
      "est_impact": "medium - 2-3x render throughput on multi-GPU or when hook failures isolated; single-GPU still benefits from failure isolation (one bad batch doesn't kill full match)",
      "risk": "medium - more CS2 launches = more hook-failure surface area. Mitigated by hook-aware retry (already present) and --skip-failed-rounds for known-bad demos.",
      "confidence": "medium"
    },
    {
      "step": "overlay",
      "where": "scripts/overlay/overlay_pov.py:700-850 (run_overlay batch loop), scripts/overlay/overlay_encode.py:60-120 (_ffmpeg_encode)",
      "current": "Overlay batches each round group (default 5 rounds/batch) and does a full NVENC CQ 15 re-encode per batch with filter_complex (keyboard sprites + PiP). Then stream-copy concats batches. For a 25-round match: 5 batch encodes + 1 concat. Each batch encode processes the full 1440p frame through filter_complex.",
      "speedup": "Increase default batch size from 5 to 10-15 rounds (fewer batch encodes, less filter_complex setup/teardown overhead). Also: pre-compute keyboard sprite filter once and reuse across batches (sprite inputs don't change). The per-batch filter_complex rebuild is redundant - only the PiP enable windows and frame offsets change.",
      "est_impact": "small-medium - reduces batch encodes from ~5 to ~2-3 per match, saving ~2-5 min GPU encode time",
      "risk": "low - larger batches just mean larger segments; filter_complex logic is identical. Sprite filter reuse is a pure code refactor.",
      "confidence": "high"
    },
    {
      "step": "analyze",
      "where": "scripts/pov/pipeline.py:480-520 (step_analyze)",
      "current": "Runs `csdm analyze` then separately `csdm json --output-folder` - two full demo parses. csdm analyze already parses the demo for round/kill counts; csdm json re-parses for full JSON export.",
      "speedup": "Use `csdm json` output for both round counting AND analysis persistence. Drop the separate `csdm analyze` call. The JSON export contains rounds[], kills[], tickrate - everything step_analyze needs.",
      "est_impact": "small - saves one full demo parse (~30-60s per demo)",
      "risk": "low - csdm json is the authoritative source; analyze is just a human-readable summary. Pipeline already validates rounds>0 from JSON.",
      "confidence": "high"
    },
    {
      "step": "overlay",
      "where": "scripts/overlay/overlay_utilcams.py:380-450 (_render_throw_flight_clips -> _run_batch_util_cams_subprocess)",
      "current": "Utility throw flight clips rendered via CS2UtilArchive's render_spot_batch in chunks (--chunk-size). Default chunk_size=0 means all throws in one CS2 launch. But render_util_cams.py is called as a subprocess per overlay run, adding process spawn overhead.",
      "speedup": "Cache rendered flight clips aggressively across POVs on same demo. The util_cams_root (under renders/) is already shared - but _scan_utility_cams_clips only scans when video_path is passed. Ensure pipeline passes the render_dir's utility_cams to overlay step so clips are reused. Also: increase default chunk_size to batch more throws per CS2 launch (e.g. 10-15 throws/chunk).",
      "est_impact": "medium - avoids re-rendering identical throw flights for multiple POVs on same match; larger chunks reduce CS2 launch overhead (~30-60s per launch)",
      "risk": "low - clip naming is deterministic (util_id_slug + throw_id). Cache invalidation only needed when render params change (pip size, video height).",
      "confidence": "high"
    },
    {
      "step": "concat",
      "where": "scripts/pov/concat_rounds.py:200-280 (_parse_batches, concat_rounds)",
      "current": "Concatenates batches one-by-one: each iteration runs ffmpeg stream-copy concat of (combined.mp4 + next_batch) -> tmp -> replace. For N batches, this does N-1 ffmpeg invocations reading/writing the growing combined.mp4 each time.",
      "speedup": "Use single ffmpeg concat demuxer pass with a file list of all batches. One invocation, one read of each batch file, one write of final combined.mp4. The round_offsets sidecar can be computed from batch durations (already probed) without incremental concat.",
      "est_impact": "small - saves N-1 ffmpeg invocations and repeated I/O on combined.mp4 (~1-2 min for 5-10 batches)",
      "risk": "low - concat demuxer is standard ffmpeg; sidecar math is identical (sum of batch durations). Must ensure all batches share codec params (they do - same render_pov.py call).",
      "confidence": "high"
    },
    {
      "step": "outro",
      "where": "scripts/pov/generate_outro.py:70-95 (main)",
      "current": "Generates 5s outro using libx264 (CPU encode) at source resolution/fps. Then pipeline must concat it (extra step). The outro is static content - same for every video.",
      "speedup": "Pre-render outro at 2560x1440@60 once (assets/outro_1440p60.mp4). Pipeline step_outro just copies the pre-rendered file and stream-copy concats. Or better: append outro during overlay step's final concat (overlay already does batch concat + audio resync - add outro.mp4 to file list).",
      "est_impact": "small - saves 5s CPU encode + one concat per POV (~10-20s)",
      "risk": "low - outro is pure static asset. Pre-render at max quality once.",
      "confidence": "high"
    }
  ],
  "top_3": [
    "concat: skip redundant scale/encode when render already at 2560x1440 (saves 5-15 min/POV)",
    "analyze: drop separate csdm analyze call, use csdm json for everything (saves 30-60s/demo)",
    "overlay: increase batch size + reuse sprite filter across batches (saves 2-5 min/POV)"
  ],
  "questions_for_oracle": [
    "What % of POVs render at non-16:9 native resolution (requiring concat upscale)? If >80% are 16:9, the concat scale skip is a massive default win.",
    "Is there a CSDM flag to output JSON during analyze (avoiding double parse)? Or can we patch csdm to do both?",
    "Can utility cam flight clips be pre-rendered per-demo (once) and shared across all POVs on that match? The overlay_utilcams cache logic suggests yes but needs verification.",
    "Does the HLAE hook failure rate actually increase with more CS2 launches (--batches > 1), or is it per-launch independent?",
    "Why does concat_rounds.py use incremental concat instead of single concat demuxer pass? Any hidden dependency on intermediate combined.mp4 state?"
  ]
}
```