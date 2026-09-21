# CS2 demo voice and speaker indicators: primary-source research

Researched 19 September 2026. Scope: distinguish audible comms from the visible speaking-player HUD, identify existing implementations, and assess a route for CS2Archive. No production or game configuration changed; no tool installed or render run.

## Main finding

**There is a real, publicly documented solution for recreating speaking indicators in CS2 demos: parse the recorded voice packets and render a speaker HUD synchronized to demo ticks.** Swift DemoUI Pro implements this in-game through Panorama. Enabling voice playback alone is a different operation and does not establish that the native speaker HUD will work.

CS Demo Manager's current playback guide specifically describes missing HUD voice indicators as a CS2 issue dating to early October 2024. Its voice aliases select audible players: `voice_all`, `voice_ct`, `voice_t`, and `voice_STEAMID64`. The team aliases follow each player's starting side rather than current side. [CS Demo Manager playback guide](https://cs-demo-manager.com/docs/guides/playback)

The linked YouTuber's actual tool cannot be attributed solely from these sources. An image resembling the native HUD could be the original in-game HUD, a replacement Panorama HUD, or an overlay composited during editing. Publication date and video inspection are necessary evidence.

## A working modern speaker HUD: Swift DemoUI Pro

The maintainer documents an avatar-and-name display in the lower-left corner while recorded speakers are active. The full launcher parses voice packets before playback and supplies the resulting index; the standalone VPK does not contain a populated per-demo index. The overlay follows demo seeking, pausing and speed changes. FACEIT voice is commonly available, but the project cannot recover packets absent from the file. It does not rewrite the demo or patch `client.dll`. Its launcher temporarily changes the game's resource SearchPath, runs an `-insecure` playback session, and offers cleanup/restoration afterward. This is an actual game-resource modification, not simply a console command. [Project README](https://github.com/nicedayzhu/SwiftDemoUIPro)

The implementation has three parts: a Panorama extension to `huddemocontroller`, a Windows Qt launcher, and a Rust voice indexer. The indexer reads `SvcVoiceData`, generates a compact tick/slot JavaScript data resource, and packages it into a per-demo VPK. The documented architecture gives a concrete reference for building our own deterministic overlay. [Developer guide](https://github.com/nicedayzhu/SwiftDemoUIPro/blob/main/DEVELOPMENT.md)

Source inspection confirms that `VoiceIndexCollector` handles `SvcVoiceData` and records packet ticks per slot. It resolves the modern `entity` field (1–64) or falls back to `client_deprecated + 1`; it then converts entity index to zero-based slot. Repeated ticks for the same slot are deduplicated. `SPEAKING_HOLD_TICKS` is 30, approximately 0.469 seconds at 64 ticks/second. This produces a packet-activity indicator, not speech recognition or speaker identification inferred from mixed audio. Its diagnostics count malformed and unresolved packets. [Rust implementation](https://github.com/nicedayzhu/SwiftDemoUIPro/blob/main/tools/voice-indexer/src/main.rs)

## Audio commands versus visual indicators

The common audio-enabling commands are:

```text
tv_listen_voice_indices -1
tv_listen_voice_indices_h -1
```

They select two halves of a 64-slot voice bitmask: low slots 0–31 and high slots 32–63. A value of zero disables that half; all bits set enables everyone represented by that half. Selective masks must use actual demo player slots, not Steam IDs, scoreboard positions, or arbitrary team numbers. `-1` for both may include the opposing team's communications. The server-plugin maintainer documents these playback commands and their bitfield semantics. [CS2-FixDemoVoiceChat](https://github.com/b0ink/CS2-FixDemoVoiceChat)

CS Demo Manager's video guide separately documents recording audio, global voice enabling, and per-player voice selection. It also exposes HUD-only-death-notices controls via `cl_draw_only_deathnotices`. HLAE provides capture/HUD capabilities, but this documentation does not claim HLAE repairs native voice indicators. Therefore a successful audio recording or enabled per-player voice checkbox is not evidence of working speaker labels. [CS Demo Manager video guide](https://cs-demo-manager.com/docs/guides/video)

No primary source located here establishes that `voice_modenable`, legacy `voice_enable`, `snd_voipvolume`, or `tv_relaytextchat` restores the missing visual HUD. Avoid describing these as fixes for the indicators. Text-chat relaying is especially easy to conflate with voice controls.

## Does the demo contain voice at all?

CS Demo Manager's 2D viewer can generate an audio file from recorded voices. Its guide explicitly says Valve matchmaking demos lack voice data. It also says generated audio matches the demo duration, whereas imported external audio may require an offset. This is an inexpensive diagnostic avenue before any expensive rendering. [2D viewer guide](https://cs-demo-manager.com/docs/guides/2d-viewer)

The original CS2VoiceData example independently warns that matchmaking demos lack voice audio. It demonstrates pulling voice data through demoinfocs-golang and decoding it with Opus. [CS2VoiceData](https://github.com/DandrewsDev/CS2VoiceData)

Do not generalize from FACEIT to every HLTV tournament download. HLTV is a distribution source; the presence of voice depends on what the recording server captured and where players communicated. External TeamSpeak/Discord audio is not created inside the demo merely by setting a playback variable. An organizer's separately recorded comms could be synchronized in editing, but that is a different input source. This is an inference from the requirement for actual recorded packets, not a claim that every HLTV file is silent.

The FACEIT support article found explains downloading the matchroom demo and using `playdemo`; it does **not**, in the retrieved version, document the voice-mask commands. A secondary guide claiming FACEIT publishes those commands should not be cited as if verified against this particular official page. [FACEIT demo guide](https://support.faceit.com/hc/en-us/articles/10622392832412-How-to-download-and-watch-a-CS2-demo)

## Offline extraction and compositing route

Akiver's voice extractor supports Windows and CS2 despite its historical `csgo` name. `split-full` creates a separate WAV for each player preserving demo timestamps and silence; `single-full` mixes voices on the full timeline. The default `split-compact` removes silence and is unsuitable for directly synchronizing speaker labels to a video. SteamID filtering is supported. Example documented syntax adapted to full per-player tracks:

```text
csgove.exe -mode split-full -output voice-output demo.dem
```

Full tracks allow separate volume control or per-player voice activity detection. Packet timestamps are a more direct identity signal if labels alone are required. [Akiver voice extractor](https://github.com/akiver/csgo-voice-extractor)

## Recommended CS2Archive direction (inference, not implementation)

For a quick interactive evaluation, test the complete Swift DemoUI Pro package on a known voice-bearing FACEIT demo, then compare the result with native playback. Do not assume its launcher integrates with CSDM/HLAE without testing resource overrides and capture behavior.

For repeatable production output, a post-render speaker overlay is likely the cleaner fit: parse voice packet ticks once, map slots to Steam IDs/names, filter to the selected POV's team, and burn small name/avatar rows into the existing overlay export. This avoids coupling normal captures to a second game launcher. It still requires proper alignment through discarded freeze time, per-round concatenation, cuts, speed changes and intro insertion. A whole-demo timestamp cannot simply be added to a concatenated POV's video seconds.

Suggested validation: overlapping speakers, muted players, POV death while teammates speak, halftime, reconnection/slot reuse, seeking and segment cuts, and demos with zero voice packets. Native silence, absent packet data and broken visual HUD should be reported as three different conditions. Do not infer microphone activity from kills, key presses or gameplay audio.

Open question: the exact method used by the reference video creator remains unverified. This report establishes viable mechanisms and their limits, not creator attribution.

## HLAE proposal: current status verified

Fresh GitHub API metadata on 19 September 2026 reports advancedfx PR #1182, "Add CS2 demo voice HUD fix", as **closed and not merged** (`merged=false`, `merged_at=null`; last update 19 June 2026). Its proposed `mirv_voiceHudFix 0|1` scopes an `IsPlayingDemo()` override to the speaker-HUD call site, feeds server voice chunks into `CVoiceStatus`, expires synthetic speaking state, and clears it after seeks. The author reports a smoke test, not a maintained-release guarantee. The comments endpoint returned no comments. This must not be presented as a supported stock HLAE command or as confirmation that HLAE maintainers endorse the implementation. [PR](https://github.com/advancedfx/advancedfx/pull/1182), [live API metadata](https://api.github.com/repos/advancedfx/advancedfx/pulls/1182)

## MulNX: separate hooking approach, HUD claim not established here

MulNX is a public CS2 observing/production platform built around runtime hooks. Its current source has an `AntiVoiceBan` feature hooking `ProcessVoiceBan` and clearing register `r8`. That establishes a voice-mute-related intervention, **not by itself a proven speaker-HUD repair**. The separate `SoundCircleFix` uses the observed pawn for sound-circle calculations and should not be conflated with a speaking-player name display. No current speaker-HUD patch implementation was positively identified in this research branch, so MulNX should remain an unverified alternative rather than a recommended fix. [Project](https://github.com/Co1Swet/MulNX_CS2), [AntiVoiceBan implementation](https://github.com/Co1Swet/MulNX_CS2/blob/main/source/MulNXExtensions/CS2/Feature/Sound/AntiVoiceBan/AntiVoiceBan.cpp), [SoundCircleFix implementation](https://github.com/Co1Swet/MulNX_CS2/blob/main/source/MulNXExtensions/CS2/Feature/Sound/SoundCircleFix/SoundCircleFix.cpp)

## Reference video observation supplied by the main investigation

The reference is Siez CS2's "TRYHARD FACEIT MATCH! - SPRIT DUO vs FALCONS DUO on FACEIT | CS2", published 17 September 2026, describing a 2 September FACEIT match. At approximately 1:35 it shows stacked speaker-icon/avatar/name rows in the lower left above the money; `7kick` and `HEAVENACHE11` are visible. At 1:30 no active row is visible. It is a native-looking speaking display, not a scoreboard shade. The description does not disclose a tool. The observation establishes appearance and timing, not whether the creator used a native-HUD patch, Panorama replacement, or editing overlay. [Reference video](https://www.youtube.com/watch?v=3_yS6S8kdIk&t=90s)

## Why a replacement can work when the native HUD does not

Swift's author reports reverse-engineering a distinction between the speaking-state bit and a nonzero voice-level table queried by the native HUD. In the analyzed demo playback path, sound can play without populating the latter as live network voice does. This is the author's build-specific reverse-engineering result, not a Valve specification or an independently reproduced experiment here. The same report attributes a client-logic workaround to MulNX; this attribution is stronger than the directly inspected MulNX evidence above, but remains an attribution. [Author's technical investigation](https://github.com/nicedayzhu/SwiftDemoUIPro/blob/main/docs/CS2_DEMO_VOICE_SPEAKING_ICON_ANALYSIS_CN.md)

The replacement's actual JavaScript independently establishes its own mechanism: `_SpeakingSlotsForTick` finds the last packet pulse at or before the current tick and retains slots within `holdTicks`; `_FilterSpeakingSlotsForSelection` applies listening selection; `_RenderSpeakingPlayers` fills names, Steam avatars, team styling and death state. `_PollVoiceActivity` schedules updates every 0.05 seconds. Therefore it can reconstruct the display without asking the broken native speaking-status function. [Panorama source](https://github.com/nicedayzhu/SwiftDemoUIPro/blob/main/addon/panorama/scripts/hud/swift_demo_voice.js)

## Existing CS2Archive implementation: substantial reuse available

These are observations of local source on 19 September 2026, not claims that a new render was tested:

| Existing component | What it already provides | Relevance to the requested appearance |
| --- | --- | --- |
| [mix_team_voice.py](D:/Projects/CS2Archive/scripts/faceit/mix_team_voice.py:201), `load_voice` | `DemoParser.parse_voice()` records containing tick, SteamID and packet bytes | Speaker identity and timing already exist; a new Rust parser is unnecessary |
| [voice_shade.py](D:/Projects/CS2Archive/scripts/overlay/voice_shade.py:85), `_player_talk_segments` | Per-player talk intervals from grouped packets, with decoded duration and POV-team filtering | Reuse as the input to name/avatar rows |
| [mix_team_voice.py](D:/Projects/CS2Archive/scripts/faceit/mix_team_voice.py:305), `tick_to_time` | Maps demo ticks through per-round offsets and recorded durations | Provides the existing alignment mechanism; validate at edits and joins |
| [pipeline.py](D:/Projects/CS2Archive/scripts/pov/pipeline.py:550), `_voice_enabled` | Explicit flags enable voice; otherwise FACEIT requires estimated team voice of at least 180 seconds, HLTV defaults off | Missing visual output may be a pipeline eligibility decision, independently of the game HUD bug |
| [pipeline.py](D:/Projects/CS2Archive/scripts/pov/pipeline.py:1213) | Sends voice-shade inputs into concatenation/scaling; later mixes team voice | Current appearance is shading of top scoreboard avatars, not lower-left labels |

The threshold is an estimate that sums 10 ms per packet, rather than measured unique speaking time. Overlapping speakers count separately. The function's older docstring says FACEIT always enables comms, but the executable code gates it on this estimate. `--enable-voice-comms` and legacy `--voice-shade` force both existing audio and shade features; neither implements the requested label layout.

Several comments in [cs2_pov.cfg](D:/Projects/CS2Archive/assets/cs2_pov.cfg:10) and [render_pov.py](D:/Projects/CS2Archive/scripts/pov/render_pov.py:815) imply the voice-mask commands also ensure indicators. Treat those comments as unsupported expectations, not evidence that the native HUD is repaired. Similarly, the older [voice research note](D:/Projects/CS2Archive/docs/research/csdm_voice_recording.md) overgeneralizes PBDEMS2: the header alone does not establish voice-packet availability. Its inference that a self-resolved issue proves a particular CS2 root cause is also stronger than the evidence. This report supersedes those claims; production source was not changed.

### Proposed implementation and bounded validation

Reuse the existing packet-to-player timeline and add a lower-left stack with one row per active speaker: icon, cached avatar, readable name, and optional dead-player styling. Use the same player selection for audible comms and visible rows. Permit simultaneous speakers. Keep the timing data separate from presentation so the same intervals can drive either scoreboard shading or labels.

Compose the labels in an existing video export pass where practical. Ensure its timestamps refer to that pass's footage: apply any intro offset, clipping, speed change or removed interval consistently. Existing proportional per-round mapping is a starting point, not proof of exact alignment after arbitrary edits. Packet activity can include transmitted silence/noise and is not an exact speech detector.

Before production adoption, render a short separate sample with known overlapping voices and a round transition. Check name/audio agreement, onset and end timing, POV-team filtering, death state, resolution scaling, and placement above money without colliding with input/utility overlays. Then check halftime and a no-voice demo. This work is proposed, not performed during the research task.

**Recommendation:** use Swift DemoUI Pro as a documented in-game reference or interactive trial; implement production labels using CS2Archive's existing extracted activity data. The requested result is feasible without knowing Siez's private workflow. Exact visual parity and synchronization still need an implementation and sample review.
