# Research: Obtaining CS2 Pro Players' Resolution / Aspect Ratio for the POV Archive Pipeline

Date: 2026-07-16
Scope: Can we recover a pro's screen resolution/aspect ratio from `.dem` files, and if not, what are the best external sources and what would it technically mean to render a POV "as the pro saw it"?

**Bottom line up front:** Resolution/aspect ratio is a **local, client-side-only setting** in CS2. It is never sent to the server and never appears in `CMsgPlayerInfo`, string tables, or the demo's convar snapshot. It is genuinely unrecoverable from a `.dem` file — confirmed by the wire-format schema itself, not just parser limitations. The practical path is external scraping (prosettings.net / Liquipedia), and "applying" a pro's 4:3 to a render is a real gameplay-FOV change (Hor+ scaling), not just a cosmetic crop.

---

## 1. Is resolution recoverable from `.dem` / demoparser2 / csdm? What IS stored?

**Verdict: No. This is confirmed at the protobuf/wire-format level, not merely a parser gap.**

### 1.1 What a CS2 demo actually contains (Valve's own wire format)

CS2 demos are Source 2 `.dem` files: a stream of protobuf-encoded `EDemoCommands` (`DEM_FileHeader`, `DEM_Packet`, `DEM_StringTables`, `DEM_UserCmd`, etc.), defined in Valve's own leaked/tracked protobufs:

- `CDemoFileHeader` — only `server_name`, `client_name`, `map_name`, `game_directory`, `demo_version_name/guid`, `build_num`, `game`, `server_start_tick`. No client video/display fields at all.
  Source: [demo.proto — SteamDatabase/GameTracking-CS2](https://github.com/SteamDatabase/GameTracking-CS2/blob/master/Protobufs/demo.proto)
- The per-player "userinfo" data recorded in the demo is `CMsgPlayerInfo`, which has **exactly six fields**: `name`, `xuid`, `userid`, `steamid`, `fakeplayer`, `ishltv`. There is no resolution, aspect ratio, or any video-setting field in this message — this is Valve's actual network schema, not a parser omission.
  Source: [CMsgPlayerInfo (source2-demo-protobufs docs.rs, generated from Valve's schema)](https://docs.rs/source2-demo-protobufs/latest/source2_demo_protobufs/struct.CMsgPlayerInfo.html)
- `demoinfocs-golang` (markus-wa), a mature CS2/CS:GO parser, explicitly documents in its `parseUserInfo` code that several fields which *used to exist in CS:GO's userinfo* (`Version`, `GUID`, `FriendsID`, `FriendsName`, `CustomFiles0-3`, `FilesDownloaded`) are **"Fields not available with CS2 demos"** — CS2's userinfo schema is a strict subset of CS:GO's, and CS:GO's own userinfo *never* carried resolution either.
  Source: [stringtables.go — markus-wa/demoinfocs-golang](https://github.com/markus-wa/demoinfocs-golang/blob/454fddaf761f/pkg/demoinfocs/stringtables.go)

### 1.2 What convars ARE recorded — and why resolution isn't among them

Demos also snapshot a subset of console variables ("convars"), exposed by:
- demoparser2: `DemoParser.parse_convars()` → dict of ConVar key/value pairs captured from the demo.
  Source: [demoparser2 on PyPI](https://pypi.org/project/demoparser2/0.0.7/), [LaihoE/demoparser GitHub](https://github.com/LaihoE/demoparser)
- demoinfocs-golang: `GameState().Rules().ConVars()` → map of cvar keys/values, with the caveat "**Not all values might be set**."
  Source: [demoinfocs GameRules.ConVars() godoc](https://godoc.org/github.com/markus-wa/demoinfocs-golang)

Crucially, **only convars flagged for network transmission are ever candidates for this snapshot** — specifically ones with the `FCVAR_REPLICATED` (server-enforced, pushed to all clients) or historically `FCVAR_NOTIFY`/`FCVAR_USERINFO` flags (see §4). Resolution/aspect-ratio settings carry none of these flags in CS2 (see below), so they cannot appear in `parse_convars()` output even in principle — this isn't a demoparser2 limitation, it's a consequence of what the game engine chooses to network. In practice, the convars that do show up are game-rule cvars like `mp_roundtime`, `mp_maxmoney`, `hostname`, etc. — never client video settings.

### 1.3 The client-side aspect-ratio convar is explicitly a dead/legacy stub

The legacy `sys_aspectratio` convar (0 = 4:3, 1 = 16:9, 2 = 16:10) is documented as:

> "`sys_aspectratio` = `-1` client archive — **Convar used exclusively by the options screen to set aspect ratio. Changing this convar manually will have no effect.**"

Source: [CSGO Options Menu to CVARS — Steam Community Guide](https://steamcommunity.com/sharedfiles/filedetails/?id=856901094)

The actual, authoritative value CS2 uses at runtime is **not a convar at all** — it's a key in a local settings file (`setting.aspectratiomode`, `setting.defaultres`, `setting.defaultresheight` in `cs2_video.txt`), which is pure client-machine local storage, never transmitted over the network in any form, and therefore structurally cannot end up in a `.dem` file. Confirmed by multiple independent config-file guides:
- [CS2 Config File Guide — CS2.eu](https://cs2.eu/news/cs2-config-file-guide-autoexec-video-settings-and-custom-configs) — lists `cs2_video.txt` as a separate "Video settings" file distinct from the gameplay `.vcfg` convar files.
- [Steam Community: CS2 — Guide to all settings for your CFG](https://steamcommunity.com/sharedfiles/filedetails/?id=3038202828) — maps every video setting (aspect ratio, resolution, refresh rate, MSAA, shadow quality, etc.) to `cs2_video.txt` keys, not console `.cfg` convars.
- [CS Demo Manager official FAQ / docs](https://cs-demo-manager.com/docs/) — csdm's own documentation confirms recording resolution is purely an *application/CLI* setting (`width`/`height` in csdm's JSON config or CLI flags), unrelated to anything read from the demo.

### 1.4 What IS confirmed to be in demos (per project's existing knowledge, now corroborated)

- **Crosshair share codes**: `crosshair_code` field (backed by `m_szCrosshairCodes`, a real networked player entity property) is extractable via demoparser2 (`parser.parse_player_info()` / `parse_ticks(["crosshair_code"])`), and there are dedicated tools built solely around this (`BeepIsla/demo-crosshair-code`, `sahelanthropus/demo-crosshair-code`).
  Sources: [LaihoE/demoparser field list](https://github.com/LaihoE/demoparser), [BeepIsla/demo-crosshair-code](https://github.com/BeepIsla/demo-crosshair-code), [sahelanthropus/demo-crosshair-code](https://github.com/sahelanthropus/demo-crosshair-code)
- **`fov` field / `m_iDesiredFOV`-style mapping in demoparser2's docs is misleading-sounding but is not a resolution/aspect proxy.** The actual networked schema field backing player FOV is `CCSPlayerBase_CameraServices::m_iFOV` (plus `m_iFOVStart`, `m_flFOVTime`, `m_flFOVRate`, `m_hZoomOwner`) — this exists purely to network **zoom state for AWP/scoped weapons** (and so spectators/GOTV render the correct zoomed view), not the player's base rendering FOV. Confirmed directly from Valve's dumped Source 2 schema:
  Source: [CCSPlayerBase_CameraServices.h — SteamTracking/GameTracking-CS2](https://github.com/SteamDatabase/GameTracking-CS2/blob/master/DumpSource2/schemas/server/CCSPlayerBase_CameraServices.h)
- Other genuinely networked/replicated per-player fields (health, position, inventory, rank, competitive stats, etc.) are all in [LaihoE/demoparser's field table](https://github.com/LaihoE/demoparser) — none pertain to display/video settings.

### 1.5 No exceptions found

We specifically checked for the historically-suggested "exceptions" and found none hold up for CS2:
- **Convars in demo header** — `CDemoFileHeader` has no convar payload at all (see 1.1); convars live in separate `DEM_*` commands snapshotted from the *server's* networked cvar table, not the client's local video config.
- **Userinfo** — `CMsgPlayerInfo` schema (1.1) proves there's no field for it.
- **GOTV/broadcast fragments** — no evidence of any additional per-client video data; GOTV data is server-side spectator data, same schema.

---

## 2. Best external sources for pro resolutions

Since demos cannot yield this data, external scraping/reference sources are the only path. Ranked by reliability:

### 2.1 prosettings.net — best coverage, actively maintained, has direct caveats

- URL: [prosettings.net/lists/cs2](https://prosettings.net/lists/cs2/)
- Single table covering **hundreds of active pros** with columns: Team, Player, Role, Mouse, Hz, DPI, Sens, eDPI, Zoom Sens, Monitor, GPU, **Resolution, Aspect Ratio, Scaling Mode**, plus peripherals.
- The site explicitly states its methodology and limitation: *"The list you see below is connected to our database where we update the information as soon as possible. If we made any mistakes or you see any outdated information, please feel free to join us in the comments to discuss these settings and their sources."* — i.e., it's crowd-corrected but not continuously automatically verified.
  Source: [prosettings.net/lists/cs2/](https://prosettings.net/lists/cs2/)
- Their companion blog article gives an aggregate stat snapshot (as of article date) useful for sanity-checking scraped data: *"approximately 75% of active pros use 4:3 resolutions... about 70% of active pros prefer the stretched res... about 52% of pros use 1280×960."*
  Source: [Best CS2 Resolutions — prosettings.net blog](https://prosettings.net/blog/cs2-resolutions/)
- **Scraping**: The site's markup is plain HTML tables, scrapable with BeautifulSoup/lxml. A prior art scraper exists (`matej0/prosettings-webscraper`), though it only targets crosshair/viewmodel/cl_bob `<code>` blocks on a legacy page layout, not the resolution table — you'd need a new scraper targeting the current `/lists/cs2/` table structure.
  Source: [matej0/prosettings-webscraper](https://github.com/matej0/prosettings-webscraper/blob/main/prosettings.py)
- **Caveats**: No visible per-row "last updated" timestamp in the aggregate list (unlike Liquipedia's per-setting dating) — this is the resolution table's main reliability weakness. Individual player detail pages (`prosettings.net/counterstrike/<player>/`) do carry more granular sourcing.

### 2.2 Liquipedia — best per-claim sourcing/dating, more sparse coverage

- URL pattern: `liquipedia.net/counterstrike/<PlayerName>` → "Gear and Settings" section.
- Each player's Hardware table has explicit **Monitor / Refresh rate / In-game resolution / Scaling** columns, each with a footnote citation `[1]` linking to the source (usually a config-dump video, stream screenshot, or interview), and an **"Updated as of YYYY-MM-DD (N days ago)"** stamp.
  Examples: [EliGE — Liquipedia](https://liquipedia.net/counterstrike/EliGE) (1680×1050, Stretched, updated 2023-03-30), [Twistzz — Liquipedia](https://liquipedia.net/counterstrike/Twistzz) (1920×1080, updated 2023-09-18), [ropz — Liquipedia](https://liquipedia.net/counterstrike/Ropz) (1920×1080, Native, updated 2022-05-31)
- **Reliability trade-off**: Liquipedia is wiki-edited (SMW-backed), so citations are traceable and per-field dated — this is the most auditable source — but coverage/update cadence is patchier than prosettings.net (some pages are 1000+ days stale, as shown by the "days ago" counters above), and not every player has a Gear/Settings section at all.
- Liquipedia also maintains an aggregate page, [List of player mouse settings](https://liquipedia.net/counterstrike/List_of_player_mouse_settings), auto-generated from the same Semantic MediaWiki data via `{{Mouse settings list}}` — good for bulk querying (Liquipedia has a documented API for structured SMW queries, though we did not test it here) but it's mouse-only, not resolution.

### 2.3 HLTV — not a source for this data

We confirmed HLTV.org player profile pages (e.g., [hltv.org/player/26436/fr1ze](https://www.hltv.org/player/26436/fr1ze)) contain only bio, team history, and Rating 3.0 stats — **no gear/settings/resolution section exists on HLTV player pages at all.** Do not attempt to scrape HLTV for this.

### 2.4 Team configs / Twitter / stream config dumps — highest accuracy when available, but not systematically scrapable

- Some pros post their literal `autoexec.cfg`/video config on Twitter/Pastebin, or stream overlays show settings.
- This is the ground-truth source Liquipedia's citations often point back to, but it's unstructured, player-initiated, and has no central index — not viable as a primary bulk-scrape target, only as a spot-check/citation trail via Liquipedia's footnotes.

### 2.5 Accuracy caveats common to all sources (important for pipeline correctness)

1. **Settings change over time.** Pros switch resolution/monitor between tournaments/years (visible directly in the "days ago" staleness on Liquipedia). Any scraped value should be treated as "last known," not "as of this specific demo's date," unless the source is dated at/after the match.
2. **Stretched vs. black-bars confusion.** A player's listed "Resolution: 1280×960, Aspect Ratio: 4:3" is ambiguous about *display* mode unless a separate "Scaling"/"Aspect Ratio Mode" column is present (prosettings.net has this; Liquipedia usually has it too, e.g. EliGE = "Stretched", ropz = "Native"). Missing scaling-mode metadata should be treated as unknown, not assumed stretched (only ~70% of 4:3 users stretch, per §2.1's own stats — [key-drop.com breakdown](https://key-drop.com/blog/best-cs2-resolution/) confirms ~76% of 606 tracked pros use 4:3, and of those ~85% (392/462) stretch vs ~15% (70/462) black-bar).
3. **Resolution ≠ aspect ratio 1:1.** 16:10 (e.g. 1680×1050) is a distinct third category from 4:3 and 16:9 and is comparatively rare (~5% per key-drop.com's breakdown) — don't bucket it into either.
4. **CS2 removed the in-game stretched/black-bars toggle that CS:GO had.** Per a widely-corroborated community guide, achieving true "stretched" in CS2 (as opposed to letterboxed black bars) now requires **GPU driver-level scaling** (NVIDIA Control Panel "Full-screen" scaling mode, or AMD equivalent) rather than an in-game option — meaning a pro's "Stretched" listing implies a specific GPU control-panel configuration outside the game itself.
   Source: [Stretched Resolution vs Native in CS2 — SensLab](https://senslab.pro/guides/stretched-resolution-vs-native-cs2)

---

## 3. Does rendering at a different aspect ratio via HLAE/csdm actually change recorded FOV, or is FOV independent of output resolution?

**It genuinely changes the rendered field of view. This is not a purely cosmetic/output-side effect.**

### 3.1 CS2 uses Hor+ FOV scaling, and the vertical FOV is fixed

- CS2's vertical FOV is a fixed constant (~73.74°); horizontal FOV is derived from the *actual render aspect ratio* ("Hor+" scaling — wider aspect ⇒ wider horizontal FOV, vertical unchanged).
  Sources: [Stretched Resolution vs Native in CS2 — SensLab](https://senslab.pro/guides/stretched-resolution-vs-native-cs2), [CS2 FOV Calculator](https://fovcalculator.netlify.app/games/cs2-fov-calculator.html), [CS2 Resolution & Aspect Ratio Calculator — CSDB.gg](https://csdb.gg/resolution-calculator/)
- Concretely: 4:3 → 90.00° horizontal FOV; 16:10 → ~100.39°; 16:9 → ~106.26°. A player on 4:3 genuinely sees **~16° less horizontal world** than a 16:9 player, full stop — this is a real, non-cosmetic difference in what's visible on screen (e.g., a peek that's visible on 16:9 native may be just out-of-frame on 4:3).
  Source (calculation table): [SensLab stretched-vs-native guide](https://senslab.pro/guides/stretched-resolution-vs-native-cs2)

### 3.2 "Stretched" itself does NOT add FOV — it's a display-stretch of the narrower 4:3 image

Critical distinction confirmed by multiple independent sources: switching to a 4:3 **resolution** (e.g. 1280×960) changes the FOV to 90° regardless of whether you then view it as letterboxed black-bars or GPU-stretched full-screen. **"Stretched" vs "black bars" is purely how the already-4:3-rendered image is displayed on a 16:9 physical panel — it changes nothing about what part of the game world is rendered, only how those pixels are geometrically distributed on screen** (stretched = pixels stretched horizontally to fill the panel, making player models look ~25–33% wider; black bars = pixels shown 1:1 with unused black margins).
Sources: [CSDB.gg resolution calculator FAQ](https://csdb.gg/resolution-calculator/), [key-drop.com CS2 resolution guide](https://key-drop.com/blog/best-cs2-resolution/), [FOVConverter CS2 FOV Calculator](https://fovcalculator.netlify.app/games/cs2-fov-calculator.html)

### 3.3 There is no legitimate in-game FOV slider — real camera FOV is aspect-ratio-derived only

- CS2 has no "camera FOV" slider in Settings. `viewmodel_fov` (range 54–68) only repositions the weapon/hands model — it does **not** change world FOV.
- The only console command that changes real world/camera FOV is `fov_cs_debug`, and it **requires `sv_cheats 1`** — it will not work in matchmaking/official servers and, being purely client-side rendering math, is never sent to or validated by the server.
  Sources: [How to Change FOV in CS2 — blog.cs2.ad](https://blog.cs2.ad/how-to-change-fov-in-cs2/), [How to Change FOV in CS2 — UUSKINS](https://blog.uuskins.com/how-to-change-fov-in-cs2-best-viewmodel-commands), [esports.net FOV guide](https://www.esports.net/news/counter-strike/how-to-change-fov-cs2/)
- This confirms §4 below: base FOV is not a "setting" that gets sent anywhere; it's computed locally from your render resolution's aspect ratio, every frame, client-side only.

### 3.4 What "applying" a pro's 4:3 to an HLAE/csdm render technically requires

Given the above, correctly reproducing a pro's exact view (not just a stylistic crop) means:

1. **Actually set the CS2 render resolution to a 4:3 (or 16:10) value** via csdm's `--width`/`--height` (CLI) or HLAE's `-w`/`-h` launch parameters — e.g. `--width 1280 --height 960`. This is the same mechanism csdm/HLAE already use to control resolution (confirmed: csdm's CLI/JSON config directly exposes `width`/`height`, and HLAE's `AfxHookSource2` launch command line takes `-w`/`-h` args). Because CS2 computes FOV live from the actual render dimensions, this genuinely reproduces the narrower ~90° horizontal FOV the pro played with — it is not a fake/approximate effect.
   Sources: [CS Demo Manager CLI docs](https://cs-demo-manager.com/docs/cli), [AfxHookSource2 — advancedfx wiki](https://github.com/advancedfx/advancedfx/wiki/AfxHookSource2)
2. **mirv_streams captures the game's actual rendered frame**, at whatever resolution the game is currently rendering at — the advancedfx wiki's stream/recording documentation shows no independent "output resolution" knob separate from the game's own render size; the stream mirrors the live render. This means the raw captured video from a 4:3 render will itself be 4:3-shaped (e.g., 1280×960 pixels), **not stretched** — it looks like a normal (slightly narrower) view, not "fat models."
   Source: [Source2:mirv_streams — advancedfx wiki](https://github.com/advancedfx/advancedfx/wiki/Source2:mirv_streams)
3. **To visually reproduce "stretched" (fat models) rather than just narrower-FOV-but-normal-proportions**, you would need an *additional, separate* post-process step: a non-uniform horizontal stretch (e.g., an ffmpeg `scale` filter with different X/Y factors) applied to the captured 4:3 frame when compositing/upscaling to the final 16:9 canvas — this is exactly what a pro's own GPU scaling ("Full-screen" mode in NVIDIA Control Panel) does at the display layer, which HLAE's capture (happening before that final display-stretch step) does not include. This is a deliberate, separate cosmetic step you'd have to add; it is not something `mirv_fov` or `mirv_camio` (which only override/import camera FOV/position, not aspect) does automatically.
   Source (mirv_fov / mirv_camio purpose): [Source2:Commands — advancedfx wiki](https://github.com/advancedfx/advancedfx/wiki/Source2:Commands)
4. **Net effect for the pipeline**: rendering "at a pro's 4:3" is a real, meaningful FOV change (less peripheral info visible, matching what they actually played with) — worth doing if authenticity to the pro's exact view matters — but it is a materially different decision from the current fixed 2560×1440 16:9 approach, and would require re-plumbing output canvas handling (pillarboxing/letterboxing or the stretch step above) to still produce a normal 16:9 YouTube deliverable. It is not a simple metadata/cosmetic flag.

---

## 4. Are any aspect/resolution convars networked (`FCVAR_REPLICATED`/`FCVAR_NOTIFY`)? Or are they purely client-side?

**Purely client-side. No aspect/resolution-related convar carries a networked flag in CS2.**

### 4.1 The relevant FCVAR flags

From a maintained CS2 cvar/flags dump:
- `FCVAR_NOTIFY (1<<8)` — notifies other players when changed (e.g. `sv_cheats`), not a resolution-related flag.
- `FCVAR_USERINFO (1<<9)` — puts the value into the client's userinfo string sent to the server (this is the CS:GO-era mechanism that could theoretically carry client info) — **not used by any resolution/aspect convar in CS2.**
- `FCVAR_REPLICATED (1<<13)` — "server setting enforced on clients" — this flag is for *server→client* enforcement of gameplay convars (e.g. `mp_roundtime`, `cash_player_bomb_planted`), the opposite direction of what would be needed to carry a client's personal display setting to the server/demo.
  Sources: [Counter-Strike 2 ConVars/Commands Dump — gist](https://gist.github.com/SuGolYolLom/7637a0a427c41e9668fdc0ea2fe1a78a), [SuGolYolLom/CS2-Cvars-Cmds — FCVAR flag table](https://github.com/SuGolYolLom/CS2-Cvars-Cmds)

### 4.2 `sys_aspectratio` carries none of these flags in a meaningful way

As already cited in §1.3, `sys_aspectratio` is documented as `client archive` only, and explicitly "used exclusively by the options screen... changing this convar manually will have no effect" — it's a legacy stub that CS2's actual settings pipeline doesn't even read; the authoritative value lives in `cs2_video.txt`, a **local file, never a networked convar of any kind.**
Source: [CSGO Options Menu to CVARS — Steam Community Guide](https://steamcommunity.com/sharedfiles/filedetails/?id=856901094)

### 4.3 Why this matters mechanically

Even in the counterfactual case where a resolution-ish convar *did* carry `FCVAR_USERINFO`, CS2's demo format would only capture it if the *userinfo schema itself* (`CMsgPlayerInfo`, §1.1) had a slot for it — and it doesn't (6 fixed fields, none video-related). So the lack of a replication flag and the lack of a schema field are two independent, mutually reinforcing confirmations that this data cannot reach a `.dem` file by any currently-known mechanism.

---

## Summary Table

| Question | Answer | Confidence |
|---|---|---|
| Resolution/aspect in `.dem` files? | No — confirmed at protobuf schema level (`CMsgPlayerInfo`, `CDemoFileHeader`), not just parser gap | High (primary source: Valve's own tracked schema) |
| Any exceptions (header convars, userinfo)? | None found; `sys_aspectratio` is an inert legacy stub, real value lives in local `cs2_video.txt`, never networked | High |
| What IS in demos re: personal settings? | Crosshair share code (`m_szCrosshairCodes`), FOV-for-zoom (`m_iFOV`, unrelated to display), name/steamid/userid | High |
| Best external source, breadth | prosettings.net — hundreds of players, resolution+aspect+scaling columns, crowd-corrected | Medium-high |
| Best external source, per-claim trust | Liquipedia — cited + dated per field, but sparser coverage | High (per claim), Medium (coverage) |
| HLTV as a source? | Not viable — no gear/settings data on player pages | High |
| Does 4:3 render change actual FOV? | Yes — real ~16° horizontal FOV reduction vs 16:9, via Hor+ scaling; not cosmetic | High |
| Does "stretched" itself add FOV? | No — stretched vs black-bars is purely display-layer stretching of the same 4:3-rendered image | High |
| Can pipeline reproduce pro's exact FOV? | Yes, via `--width`/`--height` on csdm/HLAE matching the pro's resolution; "fat model" look needs an extra ffmpeg non-uniform scale step | Medium-high (mechanism confirmed; exact ffmpeg recipe not tested) |
| Any networked aspect/resolution convars? | No — no CS2 convar related to aspect/resolution carries `FCVAR_REPLICATED`/`FCVAR_USERINFO`; it's file-based local config only | High |

## Sources Consulted

- [LaihoE/demoparser](https://github.com/LaihoE/demoparser) (demoparser2 GitHub)
- [demoparser2 on PyPI](https://pypi.org/project/demoparser2/0.0.7/)
- [demo.proto — SteamDatabase/GameTracking-CS2](https://github.com/SteamDatabase/GameTracking-CS2/blob/master/Protobufs/demo.proto)
- [CMsgPlayerInfo — docs.rs/source2-demo-protobufs](https://docs.rs/source2-demo-protobufs/latest/source2_demo_protobufs/struct.CMsgPlayerInfo.html)
- [markus-wa/demoinfocs-golang](https://github.com/markus-wa/demoinfocs-golang/) + [stringtables.go source](https://github.com/markus-wa/demoinfocs-golang/blob/454fddaf761f/pkg/demoinfocs/stringtables.go)
- [cs-demo-manager.com documentation](https://cs-demo-manager.com/docs/) + [CLI docs](https://cs-demo-manager.com/docs/cli)
- [prosettings.net/lists/cs2/](https://prosettings.net/lists/cs2/) + [blog: Best CS2 Resolutions](https://prosettings.net/blog/cs2-resolutions/)
- [matej0/prosettings-webscraper](https://github.com/matej0/prosettings-webscraper)
- Liquipedia: [EliGE](https://liquipedia.net/counterstrike/EliGE), [Twistzz](https://liquipedia.net/counterstrike/Twistzz), [ropz](https://liquipedia.net/counterstrike/Ropz), [List of player mouse settings](https://liquipedia.net/counterstrike/List_of_player_mouse_settings)
- [HLTV player profile example](https://www.hltv.org/player/26436/fr1ze)
- [key-drop.com — Best CS2 Resolution and Aspect Ratio](https://key-drop.com/blog/best-cs2-resolution/)
- [SensLab — Stretched Resolution vs Native in CS2](https://senslab.pro/guides/stretched-resolution-vs-native-cs2)
- [CS2 FOV Calculator — fovcalculator.netlify.app](https://fovcalculator.netlify.app/games/cs2-fov-calculator.html)
- [CSDB.gg Resolution & Aspect Ratio Calculator](https://csdb.gg/resolution-calculator/)
- [advancedfx/advancedfx wiki: AfxHookSource2](https://github.com/advancedfx/advancedfx/wiki/AfxHookSource2), [Source2:mirv_streams](https://github.com/advancedfx/advancedfx/wiki/Source2:mirv_streams), [Source2:Commands](https://github.com/advancedfx/advancedfx/wiki/Source2:Commands)
- [How to Change FOV in CS2 — blog.cs2.ad](https://blog.cs2.ad/how-to-change-fov-in-cs2/), [UUSKINS FOV guide](https://blog.uuskins.com/how-to-change-fov-in-cs2-best-viewmodel-commands)
- [CCSPlayerBase_CameraServices.h — SteamTracking/GameTracking-CS2](https://github.com/SteamDatabase/GameTracking-CS2/blob/master/DumpSource2/schemas/server/CCSPlayerBase_CameraServices.h)
- [Steam Community: CSGO Options Menu to CVARS](https://steamcommunity.com/sharedfiles/filedetails/?id=856901094)
- [Steam Community: CS2 — Guide to all settings for your CFG](https://steamcommunity.com/sharedfiles/filedetails/?id=3038202828)
- [CS2 Config File Guide — CS2.eu](https://cs2.eu/news/cs2-config-file-guide-autoexec-video-settings-and-custom-configs)
- [Counter-Strike 2 ConVars/Commands Dump — gist](https://gist.github.com/SuGolYolLom/7637a0a427c41e9668fdc0ea2fe1a78a), [SuGolYolLom/CS2-Cvars-Cmds](https://github.com/SuGolYolLom/CS2-Cvars-Cmds)
