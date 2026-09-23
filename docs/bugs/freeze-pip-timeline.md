# Freeze cam breaks PiP timeline — open bug (TODO)

> Status: freeze pre-pass is OFF by default (`--freeze` re-enables).
> Do not re-enable until this is resolved.

## Symptom (2026-09-23, donk Mirage POV)

With lineup freeze holds enabled, everything after the first freeze drifts:
PiPs land off the throws they belong to. Disabling freeze (PiP-only) renders
correctly.

## Suspects (not yet proven — investigate before re-enabling)

- The chained trim/loop/concat-per-freeze graph silently emitted one hold
  regardless of count (rewritten as single-concat in `8553e6d`, exact counts
  verified on small files — but the full-render desync report postdates that
  fix, so something else may still be off).
- Sidecar expansion vs actual video length mismatch (final was 593s against
  587s source + 39s holds + 5s outro — holds missing from the bitstream
  while the sidecar claimed them).
- youtube sidecar provenance (was: render-dir pristine copy; fixed in
  `5e64f86` to prefer the expanded work-dir file).

## Repro / verify path

1. Rerun overlay with `--freeze` on a POV with ≥2 non-straightforward
   lineups (donk Mirage card:
   `backlog/faceit/2026-09-19/high/donk-mirage-team-cemen-bakin-vs-team-oskar-on-mirage.json`).
2. Check: final duration == source + sum(holds) + outro; each PiP's first
   frame shows the matching throw's flight start; each hold sits on its
   lineup aim frame with the throw immediately after.
3. `scripts/overlay/lineup_freeze.py` owns the graph + expansion;
   `overlay_pov.py::_apply_lineup_freezes` owns resume/persist.

## Related commits

- `c770173` overlay rework (keyboard off, dedupe, freeze pre-pass)
- `22028f5` single-concat rewrite + frame-count gate
- `8553e6d` straightforward-only render + recipe strips
- `5e64f86` expanded sidecar copy to youtube
- `0956f78` freeze off by default
