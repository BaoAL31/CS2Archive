"use strict";
// mirv_script_spec_lock_name: lock the demo spectator camera onto a player
// BY NAME, re-issued every frame (survives death — the camera stays on the
// POV player's deathcam instead of the auto-director cutting to whoever is
// "interesting").
//
// STATUS (verified 2026-09-05, cs2 1.41.7.8 + HLAE 2.192.1): the script
// loads, the lock command executes, and name matching works (probed via
// HUD-kill markers) — but the per-frame `spec_player` exec is IGNORED by
// the game, in both slot and name form, so the auto-director still cuts
// away seconds after the POV player's death. Console spec control is broken
// in cs2 1.41.6.2+ (advancedfx#1172) and no HLAE primitive replaces it yet.
// Keep installed + wired (zero-cost when it starts working again); do NOT
// rely on it for deathcam lock until re-verified on a newer build.
//
// Usage (CS2 console under HLAE):
//   mirv_script_load "mirv_script_spec_lock_name.js"
//   mirv_script_spec_lock_name "NiKo"   - lock onto NiKo (case-insensitive)
//   mirv_script_spec_lock_name off      - release the lock
{
    const id = 'mirv_script_spec_lock_name/a3f1c9e2-4b5d-4e6f-8a2c-9d4e5f6a7b8c';
    let wanted = null;
    // @ts-ignore
    if (globalThis[id] !== undefined) {
        // @ts-ignore
        globalThis[id].unregister();
        // @ts-ignore
        delete globalThis[id];
    }
    const command = new AdvancedfxConCommand((args) => {
        const argC = args.argC();
        const arg0 = args.argV(0);
        if (2 <= argC) {
            const a = String(args.argV(1));
            if (a.toLowerCase() === 'off' || a === '0') {
                wanted = null;
                return;
            }
            wanted = a.toLowerCase();
            return;
        }
        mirv.message('Usage: ' + arg0 + ' <playerName> - Lock spectating to player. '
            + arg0 + ' off - Release. Current: ' + (wanted || 'none') + '\n');
    });
    // @ts-ignore
    mirv.events.clientFrameStageNotify.on(id, (e) => {
        if (!e.isBefore && wanted) {
            for (let i = 0; i < 64; i++) {
                const entity = mirv.getEntityFromIndex(i + 1);
                if (null !== entity && entity.isPlayerController()) {
                    let raw = '';
                    try {
                        raw = String(entity.getSanitizedPlayerName() || '');
                    } catch (err) { /* ignore */ }
                    if (raw !== '' && raw.toLowerCase() === wanted) {
                        // Exec with the entity's EXACT-case name, in BOTH
                        // frame stages: cs2 1.41.6.2+ ignores the slot form
                        // (advancedfx#1172) and the director re-targets after
                        // death, so the lock must win the per-frame race.
                        mirv.exec('spec_player "' + raw.replace(/"/g, '') + '"');
                        return;
                    }
                }
            }
        }
    });
    // @ts-ignore
    globalThis[id] = command;
    command.register('mirv_script_spec_lock_name', '');
}
