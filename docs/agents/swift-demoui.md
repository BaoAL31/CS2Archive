# Swift DemoUI Pro speaker indicators

CS2Archive integrates **Swift DemoUI Pro v0.1.6** (MIT, niceday_zhu) into the existing
CSDM/HLAE capture. It uses the actual upstream compiled Panorama layout/styles and
voice indexer, with a small runtime adaptation for unattended POV recording.

- Lower-left speaker icon, Steam avatar and name rows come from recorded voice packets.
- Canonical look matches CS2's live voice HUD (no dark bars); Swift's original chrome is `--voice-indicators legacy`.
- Only the POV team's SteamIDs appear; selection survives halftime and slot changes.
- Only the POV team's SteamIDs appear; selection survives halftime and slot changes.
- Canonical names from the render's `--rename` map also apply to speaker labels.
- Swift's interactive controls stay hidden. The adaptation does not alter voice masks
  or native mute state; CS2Archive's existing team audio mix remains responsible for comms.
- The game resources are mounted only around a CSDM invocation, including hook retries.
  The original `gameinfo.gi` bytes are journaled and restored on success or failure.
- No Steam restart, alternative game launcher, source demo rewrite or DLL patch is needed.

## Installation

```powershell
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/overlay/swift_demoui.py --install
```

Downloads the official Windows release into `.cache/swift-demoui/package/SwiftDemoUIPro-v0.1.6/`.
The ZIP and tagged JavaScript are SHA-256 pinned; runtime binaries are checked before use.
The package retains upstream `LICENSE.txt` and third-party notices. It is local tooling,
not committed binaries. Installing only downloads into the project; mounting occurs at render time.

Source: <https://github.com/nicedayzhu/SwiftDemoUIPro/releases/tag/v0.1.6>.
Our adapter in `scripts/overlay/swift_demoui.py` patches six checked runtime locations:
team filtering, canonical names, native HUD restyle, menu visibility, audio commands and native unmuting.
Unexpected source versions fail explicitly rather than guessing compatible replacements.

## Pipeline use

`pipeline.py` defaults to `--voice-indicators swift` **when voice comms are enabled**.
That is the native in-game speaker HUD. `--voice-indicators legacy` keeps Swift's
dark-bar chrome. `--voice-indicators shade` is the old scoreboard-avatar effect.
The existing FACEIT eligibility threshold still applies; `--enable-voice-comms` forces it.
Swift (native or legacy) and shade are mutually exclusive in a normal new pipeline render.

For a separate manual render:

```powershell
python scripts/pov/render_pov.py <demo.dem> <steam64> --voice-indicators swift --output renders/<new-folder>
# native HUD (canonical)
python scripts/pov/render_pov.py <demo.dem> <steam64> --voice-indicators legacy --output renders/<new-folder>
# Swift's original dark-bar chrome
```

The renderer defaults to `off` when called directly; the production pipeline selects Swift.
Both ordinary rounds and trimmed tick windows use the same temporary resource mount.
Existing render flags and CSDM's offline `-insecure` launch remain in use.

Prepare and inspect the resources without launching CS2:

```powershell
python scripts/overlay/swift_demoui.py --demo <demo.dem> --steam-id <steam64> --output renders/<new-folder>
```

The cache under `<output>/.swift-demoui/` includes the voice-data VPK, adapted runtime,
compiled runtime, combined session VPK and manifest. Cache identity includes demo path,
size/mtime, POV team, name map and adapter version. Changing the adapter requires bumping
`PROFILE` so cached resources cannot retain old logic.

## Resume and recovery

Indicators are baked into the game capture. Old MP4s cannot acquire them by resuming at
step 3 or 4. `voice_indicators.json` marks a render's style/player; mismatched or unmarked
old footage is rejected for Swift. Keep saved progress and select `shade` to finish an old
run, or use a fresh render directory for Swift. Check `.pipeline/<run_id>.json` before
any manual re-render/cleanup. The integration never deletes old clips to force migration.

If a render process is forcibly terminated, first make sure that CS2 session has exited,
then recover its resource mount:

```powershell
python scripts/overlay/swift_demoui.py --restore
```

The journal is `<CS2 game/csgo>/.cs2archive-swift-session.json`; resources are exclusively
under `overrides/cs2archive_swift/`. A second mount refuses to run. Recovery will not
overwrite a concurrently edited `gameinfo.gi`; preserve the journal for manual recovery
if this happens. Atomic replacement avoids partially written game configuration files.
No recursive directory or demo deletion is part of cleanup.

## Validation

`tests/test_swift_demoui.py` covers VPK CRCs, mount cleanup after failure, concurrent edits,
resume compatibility and the actual adapted JavaScript. With the installed package and
Node available, `tests/swift_demoui_runtime.cjs` exercises the upstream runtime for team
filtering, slot changes, simultaneous speakers, expiry, hidden controls and audio isolation.
Real-game rendering is additionally required after a CS2/Swift update because compiled
Panorama layouts and the demo APIs can change.

Local validation on 2026-09-20: the package indexed 75,309 packets from the test FACEIT
demo with no malformed/unresolved packets. Focused automated tests passed. A short
CSDM/HLAE capture failed with `Game error` and no encoder output both with Swift mounted
and in the baseline without it. In-game visual verification therefore remains pending;
the temporary mount was cleaned up after failure.
