# HLAE hook failures (Steam online) — long-running bug

> Read this before debugging “CS2 opened the demo but wrote no video”, `Game error`, `Raw files not found`, or a leftover vanilla demo viewer on the desktop.
> Implementation: `scripts/hook_aware.py` (`run_csdm_hook_aware`). Version gate: `scripts/pov/render_version_check.py`. **Do not** run `scripts/misc/steam_mode.py` unless the user explicitly asked — both `--offline` and `--online` call `steam.exe -shutdown` first.

The missing-recording failure now has a **causally tested CSDM initialization defect** (see September 21 below). The clean fix is now installed locally for CSDM 3.20.1. The broader historical association with Steam online/offline remains unproven. Older theories and mitigations below are retained as investigation history, not established explanations.

## 2026-09-24: HLAE was too old for CS2 1.41.8.2 (check this FIRST)

**Before touching Steam or the CSDM plugin, read the HLAE error window.** The
Sep-24 failure looked identical to the Steam-online flake (CSDM prints only
`Starting Counter-Strike... / Recording in progress... / HLAE error`, exits 0 at
~6s, no video, nothing in `~/.csdm/logs/csdm.log`) but was unrelated. Growing up
the CSDM stack shows the same three lines and no detail; the reason is a **GUI
message box**, not a log line.

Capture it: `.data/hlae_win_probe.py <batch_config.json>` launches a render and
enumerates the CS2/HLAE window titles + child control text, then cleans up. The
dialog read:

```
Error - AfxHookSource2
Problem in C:\source\advancedfx\AfxHookSource2\addresses.cpp:120
```

`addresses.cpp` is HLAE's byte-signature scanner; that throw means HLAE's
pattern table does not match the installed CS2 build (it is version-pinned to a
CS2 patch). CS2 had updated to **1.41.8.2 (VersionDate Sep 22 2026)** while the
installed HLAE was **2.192.1 / AfxHookSource2 0.41.1**, whose changelog last
says *"Adjusted to CS2 update (1.41.6.8)"*.

**Fix:** install the matching HLAE. `advancedfx/advancedfx` releases a new HLAE
whenever CS2 moves the offsets:

| CS2 build | Required HLAE |
|---|---|
| 1.41.8.2 | **HLAE 2.192.3** (AfxHookSource2 0.41.3, 2026-09-23) |

Install steps (no destructive change; keep the old version):
1. Download `https://github.com/advancedfx/advancedfx/releases/download/v2.192.3/hlae_2_192_3.zip`
   (sha256 `680b90dd5bed3e5b17de0945cc62e147696817ecdcf5e80b5de3c3cb77a84ab1`).
2. Extract its **root contents** into `%USERPROFILE%\.csdm\hlae-versions\HLAE-2.192.3\`.
3. Point `~/.csdm/settings.json` `video.hlae.customExecutableLocation` at
   `...HLAE-2.192.3\HLAE.exe` (back up settings.json first).

After that the hook works again (verified: a pip render recorded on the 2nd
harness attempt). Note the harness's `FFMPEG_GRACE` (45s) can still kill a slow
first attempt; the retry succeeds.

## Installed fix — 2026-09-21

The clean plugin is retained at `assets/csdm-startup-fix/server.dll` with its
license and build instructions. `scripts/misc/install_csdm_startup_fix.py install`
selects `server_cs2archive_startup_3_20_1.dll` in CSDM's own plugin directory and
adds `+csdm_initialize` to `playback.launchParameters`. It does not replace
`server.dll`, modify Steam mode, or add rendering retries. This saved CSDM setting
applies to CS2Archive and other projects using the same CSDM installation.

The installer refuses a mismatched stock/candidate binary and refuses to run
while CS2 or CSDM is active. The original two playback fields are journaled in
`.data/csdm-startup-fix-install.json`; use the same script with `undo` to restore
them. Unrelated settings are preserved. Subsequent changes to those two fields
cause undo to refuse rather than silently overwrite them.

Five deployment tests cover install/undo, idempotence, version rejection,
interrupted installation recovery, concurrent edits, and preservation by the
production Steam preflight (some tests cover multiple checks). Together with
the existing diagnostic/hook/preflight tests, **17 tests pass**. Updates to CSDM
or CS2 require revalidation; this is a pinned local plugin fix, not an upstream
release. See [installation details](../../assets/csdm-startup-fix/README.md).

Post-install capture `renders/hlae-diagnostic-20260921-180322` used the saved
settings with **no temporary plugin, console logging, or launch overrides**.
The mounted DLL hash matched the clean build. CSDM exited 0 at 56.45s and
produced a 38,171,491-byte, 20-second 1920x1080 H.264/AAC video. Recording started
on the first launch. The harness bypassed the production watchdog to avoid its
global encoder cleanup disrupting an unrelated CS2UtilArchive encode; preflight
compatibility is covered by the deployment test. This was not a full pipeline run.

## 2026-09-21: causal reproduction and successful intervention

Steam is **online**, confirmed by the user. No mode switch or restart was performed.
The same custom build of the CSDM 3.20.1 plugin was used for both sides of this
experiment, with its mounted SHA-256 verified by the harness:
`31196363406112b6d845480d0648875d04f8bb4fe865c101cd69589673f46409`.

**Defect:** CSDM installs `FrameStageNotify`, which drives all recording commands,
only inside the server's `ClientFullyConnect` callback. In healthy traces that
callback belongs to the local background-scene connection, before demo playback.
Demo playback itself can proceed without that connection. When it does, CSDM
never installs its frame hook, although its WebSocket and HLAE are working.
Its existing delayed-playdemo fallback also lives inside the missing frame hook,
so it cannot recover from this state.

The source is [CSDM v3.20.1 main.cpp](https://github.com/akiver/cs-demo-manager/blob/v3.20.1/cs2-server-plugin/cs2-server-plugin/main.cpp),
commit `8961f5072fe4d42803dde68e8e71b3c90b216504`.
The test commands and logging are preserved in
[causal-experiment.patch](hlae-startup-experiment/causal-experiment.patch);
[reproduction instructions](hlae-startup-experiment/README.md) explain the controls.

| Test / evidence folder under `renders/` | Outcome |
|---|---|
| `hlae-diagnostic-20260921-114633`: probe, ordinary startup | Client interface available at 7.495s with no frame hook; background connection finally installs hook at 9.835s; recording succeeds. |
| `hlae-diagnostic-20260921-114803`: explicitly start demo before background connection | Client available at 8.938s; demo plays, no connection callback, no frame hook, no recording through the 90s deadline. |
| `hlae-diagnostic-20260921-115235`: identical early playback, initialize frame hook first | No connection callback; recording commands execute; exit 0 at 57.44s; 38,168,121-byte MP4. |
| `hlae-diagnostic-20260921-115351`: repeat intervention | No connection callback; recording succeeds; exit 0 at 50.77s; 38,175,871-byte MP4. |
| `hlae-diagnostic-20260921-115614`: independent initialization, ordinary playback | Recording succeeds; exit 0 at 49.20s; 38,116,660-byte MP4. |
| `hlae-diagnostic-20260921-175019`: clean candidate, ordinary playback | Frame hook installed before connection callback; exit 0 at 52.16s; 38,231,904-byte MP4. |
| `hlae-diagnostic-20260921-175143`: clean candidate logic plus test-only early-playback trigger | No connection callback; recording succeeds; exit 0 at 52.49s; 38,067,670-byte MP4. |

The intervention runs on the engine command thread and installs the same vtable
hook the original callback would install. It does not retry the launch or alter
Steam/RTSS/overlay state. Both Steam's overlay and RTSS were loaded during the
successful intervention. The first MP4 was checked with ffprobe (20s, 1920x1080,
H.264 plus AAC) and an extracted frame was visually inspected: actual donk Mirage
gameplay, with demo controls visible. The clean candidate below corrects the
experimental UI setup ordering. The full experimental patch itself must not be
used as a default production plugin.

### Clean candidate fix

[startup-fix-candidate.patch](hlae-startup-experiment/startup-fix-candidate.patch)
factors frame initialization into an idempotent engine-thread helper and adds
the `csdm_initialize` console command. Launching with `+csdm_initialize` installs
the frame hook without requiring a background connection. The existing
`ClientFullyConnect` callback remains a fallback. UI setup is executed
synchronously before playback rather than queued for a later frame.

The isolated clean binary is `.cache/hlae-diagnostic/build-candidate/server.dll`,
SHA-256 `9042b9abeb1efee881440c1c96aad74858791f69c63ec8118611c6ae7b3c9f79`.
It contains no test commands. The regression build adds only the early-playback
trigger, SHA-256 `d4d474d57f611b702dfa08eeb8db782ec7aef90871682c9553b4f0ec4d67dae2`.
Both recorded successfully. Extracted frames from both match the stock successful
capture's UI style (including its existing thin timeline bar); the larger control
panel introduced by the earlier diagnostic timing is absent.

At this stage it was a **tested candidate**; the subsequent installation is
recorded above. Temporary plugin selection was restored after each experimental
run. The DLL alone is insufficient without `+csdm_initialize`. Validation covers
one current compatible demo and one CS2 build, not the whole production workload.
The diagnostic and existing
hook/preflight unit tests pass (12 tests); the actual startup regression is
verified by the real CS2 captures above, not mocked by those unit tests.

**What this proves:** recording incorrectly depends on an optional background
connection; bypassing that connection causes the stall, and independent frame
initialization removes it. This reproduces the plugin-stage signature of the
earlier spontaneous failure with the official binary.

**What this does not prove:** that every historical failure has this cause, or
exactly how Steam online changes the natural startup ordering. The spontaneous
failure did not have an engine console trace, and no online/offline A/B was run.
There is no evidence here that Steam killed/relaunched CS2 or that HLAE failed
to inject. A production fix should initialize on engine readiness independently
of a server connection, ensure UI setup precedes playback, and retain the server
callback as an idempotent fallback. Avoid a worker-thread call into the engine.

## 2026-09-20 investigation: reproduced CSDM initialization stall

**The latest reproduced failure is after successful HLAE injection and before
CSDM installs its frame hook. It is not evidence of Steam relaunching a vanilla
CS2.** The older explanation below remains historical, unproven for this case.

Two consecutive diagnostic captures used the same demo, tick range 6727–8007,
1920×1080 at 30fps, CSDM 3.20.1, HLAE 2.192.1 and matching demo/game patch
1.41.8.1. Swift was not mounted. Steam was not restarted or switched between
modes. Its current mode was not independently confirmed, so this is **not** an
online-versus-offline A/B test.

| Evidence | Successful run, 23:50:16 | Failed run, 23:51:58 |
|---|---|---|
| First observed CS2 with AfxHookSource2.dll | 8.55s | 7.84s |
| RTSSHooks64.dll loaded in that same process | Yes | Yes |
| Steam GameOverlay DLL observed | No | No |
| Plugin WebSocket connection / Source2GameClients001 interface | Yes | Yes |
| `ClientFullyConnect: playerSlot=0` | Yes | Never logged |
| `Hooked FrameStageNotify` | Yes | Never logged |
| Actual `mirv_streams record start` execution | Yes | Never logged |
| Capture encoder | 39.74s, parent = test CS2 PID | Never observed |
| Result | CSDM exit 0 at 61.66s, valid 20s H.264/AAC video | Still running at 150s, no video; probe then terminated its processes |

Only one CS2 process was observed per run. Polling cannot rule out a sub-second
process, but these traces contain no evidence of the proposed vanilla twin.
The successful video was probed and a gameplay frame was visually inspected.
RTSS can cause other failures, but it did not prevent this successful capture.
Waiting past the production 45s encoder deadline did not rescue the failed run.

Evidence directories (local, not committed media):

- `renders/hlae-diagnostic-20260920-235016/`: `processes.jsonl`, `plugin.log`,
  `csdm.log`, capture config, 38,165,945-byte MP4 and inspected `frame.png`.
- `renders/hlae-diagnostic-20260920-235158/`: same logs/config, no MP4.

### Source-level explanation and remaining uncertainty

The installed and game-mounted `server.dll` both hash to
`43f15c8c689efb4b1edad6b0b7b537bc4cf36223b870b8356a430f65a800a4d7`.
This is the same binary identified in [CSDM issue #1458](https://github.com/akiver/cs-demo-manager/issues/1458).
That report is corroborating evidence, not proof of every proposed cause/fix.

The [official v3.20.1 plugin source](https://github.com/akiver/cs-demo-manager/blob/v3.20.1/cs2-server-plugin/cs2-server-plugin/main.cpp)
confirms the dependency:

1. `CreateInterface` patches `Source2GameClients001` slot 15 to
   `NewClientFullyConnect`.
2. **Only that callback** resolves `Source2Client002` and installs
   `NewFrameStageNotify` at slot 36.
3. `NewFrameStageNotify` drains queued engine commands and executes recording
   actions by demo tick. Even the explicit offline `+playdemo` workaround lives
   inside this callback.

The failed log stops before step 2. Merely loading AfxHookSource2 and connecting
the plugin WebSocket therefore does not mean the recording controller is ready.
We have localized the failure to the missing callback/frame-hook initialization;
the engine lifecycle reason that the callback is intermittent is still unproven.
This investigation did not change DLLs, offsets or install an unofficial patch.

The next causal fix to test is a bounded, lifecycle-safe way to initialize the
frame hook independently of `ClientFullyConnect`, with original callback support
retained. It belongs in the CSDM plugin, not in HLAE injection retries. Review
thread safety, hook-once protection and shutdown before testing a replacement.
Repeated captures in each user-selected Steam mode are needed before declaring
the longstanding online/offline issue solved.

### Problems in our watchdog that obscure the diagnosis

These were found in the current working tree; production behavior was not changed
as part of this investigation:

- `_ffmpeg_pids()` counts every encoder on the machine. The probe observed an
  unrelated CS2UtilArchive encoder while our failed capture had none. Track the
  capture's parent PID/output path instead.
- `kill_stale_processes()` kills **all** CS2/HLAE/ffmpeg processes by image name.
  That can terminate the sibling project's active encoder. Cleanup must be scoped
  to an owned launch, and concurrent game captures need a shared lock.
- The 20s/45s branches describe processes as "stray" and kill CS2 without evidence
  of a second process. `HOOK-FAIL` / final "failed to hook" also conflate injection,
  plugin initialization, seeking and encoder startup.
- The no-encoder branch checks current PID presence even after an encoder was
  observed. A finished/failed encoder is a different state from never starting.
- `Game error` after the wrapper's kill is a consequence of that kill, not proof
  of the initial failure. Save plugin evidence **before** cleanup/retry.
- Steam overlay DLL renaming has no paired restore in this helper; it is not
  actually limited to a render session. Crash/overlay flags have not established
  causality and should not substitute for stage-specific evidence.
- `Raw files not found` remains a fatal marker even though this document's older
  mitigation described bounded retries. Review that policy after classification.

### Repeatable diagnostic probe

`scripts/misc/diagnose_hlae_capture.py <CSDM-config.json> --seconds 150` copies the
config into a fresh timestamped render directory, captures process/module and
plugin logs, and reports the last observed plugin stage. It does not invoke the
production preflight, change Steam mode, rename DLLs, or terminate unrelated
encoders. It refuses an already-running CS2. It needs process visibility outside
the restricted agent shell and requires `psutil` (present in `cs2archive`).

This is a diagnostic utility, not a replacement production renderer. Stopping at
the configured deadline is recorded explicitly. Tests in
`tests/test_hlae_diagnostic.py` distinguish scheduled recording commands from
commands actually executed. The diagnostic and existing hook/preflight tests
passed together (12 tests).

HLAE's [official FAQ](https://github.com/advancedfx/advancedfx/wiki/FAQ#hlae-doesnt-record-anything-except-audio-and-not-giving-any-errors)
does document RTSS interference. It does not establish RTSS or Steam crash
recovery as the cause of the missing CSDM callback reproduced here.

## What a healthy render looks like

1. CSDM starts HLAE (`-customLoader` / AfxHookSource2).
2. `cs2.exe` appears. **`AfxHookSource2.dll` is loaded** (tasklist `/M AfxHookSource2.dll`) in ~2–10s.
3. A **new** `ffmpeg.exe` PID appears within **45s of first AfxHook** (`FFMPEG_GRACE`). That is mirv_streams actually recording.
4. An output `.mp4` ≥ 1 MB appears. After CS2 exits, Steam may spawn a **second** unhooked `cs2.exe`; `reap_steam_respawned_cs2` kills it. **Never kill `steam.exe`.**

`run_csdm_hook_aware` retries the whole CSDM command (default 2 extra attempts) if any stage fails.

## Two different failures (do not collapse them)

| Stage | Symptom | Meaning |
|---|---|---|
| **A — inject** | No `AfxHookSource2` in any `cs2.exe` within `HOOK_INJECT_GRACE` (60s) | Loader never latched, or only a vanilla CS2 is running. |
| **B — record** | AfxHook **is** loaded, but **no new ffmpeg PID in 45s** | Demo is playing (often looks “fine” on screen) and **not recording**. Same class as CSDM `Raw files not found` / `Game error` after “Recording in progress…”. |

Stage B is the one that fooled us: the process looks hooked, the window is CS2, nothing is encoded.

A third lookalike is **demo patch too old for installed CS2** (see below). That is not a hook flake; do not burn HLAE retries on it.

## Historical working theory (not established; superseded for the reproduced stall)

When Steam is **online**, overlay inject / HLAE inject often looks like a **crash** to Steam. Steam then `steam://`-relaunches a **vanilla** `+playdemo` with **no** AfxHook. Offline never does that handshake.

Supporting observations:

- Steam **offline**: hook latches; vanilla relaunch does not appear.
- Steam **online**: a **second** `cs2.exe` with the same `+playdemo` line and no `AfxHookSource2.dll` (stock demo viewer left on the desktop).
- CSDM itself treats a CS2 window title containing `Error - AfxHookSource` as HLAE failure (`tasklist /v`).
- Adding CSDM launch flags **`-nominidumps -nobreakpad -nocrashdialog`** (plus existing `-steam -insecure -allow_third_party_software` and `+sv_lan 1`) **worked in one Steam-online experiment**. Later runs still flake. Treat the flags as a mitigator, not proof.

We have **not** found a definitive HLAE wiki / Steam changelog that states “inject = crash recovery”. Do not document a vendor URL as the root cause until one exists.

## Mitigations in tree (what we actually do)

All of this is `prepare_steam_hlae()` + the wait loop in `run_csdm_hook_aware`. Tests: `tests/test_hook_aware_vanilla.py`, `tests/test_steam_hlae_preflight.py`.

1. **CSDM `playback.launchParameters`** — keep `-steam -insecure -allow_third_party_software -nominidumps -nobreakpad -nocrashdialog +sv_lan 1` in `~/.csdm/settings.json`.
2. **Steam overlay DLLs** — rename `GameOverlayRenderer*.dll` and `SteamOverlayVulkanLayer*.dll` under `D:\Steam` to `*.blocked` for the render. Warn if they are still live. Env: `DISABLE_VK_LAYER_VALVE_steam_overlay_1=1`.
3. **`steam_appid.txt`** — write `730` next to CS2 **before launch only** (rewriting while CS2 is up fights Steam).
4. **Do not kill unhooked `cs2.exe` until AfxHook is visible** — killing early murdered the HLAE process before the DLL showed up in tasklist (10s `Game error`).
5. **Reap Steam’s vanilla respawn** after the hooked CS2 dies (~8s). Do not touch `steam.exe`.
6. **Fail fast** on stage B (45s after first AfxHook, no ffmpeg) instead of watching a dead demo for the full hook timeout.
7. **Do not copy leftover `*_pov.mp4`** after a hook fail — a previous good file or a tiny partial is not this attempt.
8. **`--batches 1`** — every extra CS2 launch raises the odds of the flake.
9. **RTSS / MSI Afterburner** — warn if running (`RTSSHooks64.dll` fights HLAE Present).
10. **`Game error` / `HLAE error` in the CSDM log are not fatal** in `hook_aware` — they also appear on our own timeout kills. Bounded retries still cap a truly broken config.

**Steam offline** still works as a last resort. Agents must **not** toggle it (`steam_mode.py` shuts Steam down). Only the user does that.

Agent-shell `Get-Process steam` / `tasklist` is a **false negative** while Steam is running. Never start/stop Steam from an empty listing. Authority is pipeline `RENDER_STEAM_NOT_RUNNING` from a real render process.

## Demo patch vs hook flake

On CS2 **1.41.8.1** (Sep 2026):

- **Records:** demo patch **≥ 1.41.6.4** (exact match not required; current CS2 can play a few older patches).
- **Does not record:** **1.41.3.8**, **1.41.4.1**, and anything below `MIN_RENDERABLE_DEMO_PATCH`. AfxHook can still load in ~2s; ffmpeg never starts; CSDM prints `Raw files not found`. Same *shape* as stage B.

Gate: `INCOMPATIBLE_DEMO_PATCHES` + `MIN_RENDERABLE_DEMO_PATCH` in `scripts/pov/render_version_check.py` → `RENDER_DEMO_TOO_OLD`. Pipeline / `render_pov.py` hard-fail. If a rewind/one-off renderer skips that gate, you will waste HLAE retries on an old `.dem`.

When adding a new “incompatible” patch, put the **patch string** on the list (or raise the min), not a match filename.

## What is still unknown

- Which natural startup ordering bypassed the background connection in the spontaneous failure, and whether Steam mode affects that ordering.
- Whether other historical incidents involved a separate injection failure or a second CS2 process; neither was observed in the controlled missing-frame-hook case.
- How best to initialize the frame hook reliably across supported CS2 builds while preserving demo UI setup and normal startup ordering.

For another spontaneous Steam-online failure, preserve the **engine console log and plugin log before CSDM cleans them up**, along with PID/module history and the demo patch version. Compare background-map loading and sign-on ordering with the controlled failure. Count only the tested CS2's encoder processes; unrelated encoders do not prove that this capture started.
