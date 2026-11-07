"""Kill in-game chat / console text in render CFGs (Shorts, Hook, POV, highlights).

What actually showed up in renders was the server's relayed console chatter,
e.g.::

    Console: This server's password has been changed to: esl38
    Console: Reminder: You can request drinks and other items via team chat ...

Those are ``TextMsg``/``SayText2`` prints drawn by the client HUD in the
bottom-left. They are re-printed every time a demo is (fast-)played from the
start, so during a CSDM seek the message can still be inside its display
window when recording begins — which is why they appear in the first seconds
of a segment even though the message was sent much earlier in the match.

Verified against the live CS2 cvar dump (cs2.poggu.me/dumped-data/convar-list,
2026-10):

- ``cl_showtextmsg 0`` — EXISTS (``developmentonly clientdll defensive``):
  "Enable/disable text messages printing on the screen." This is the direct
  killer for the ``Console: ...`` HUD prints above.
- ``hidehud 128`` — EXISTS (``clientdll cheat``), bitmask ``128=chat``:
  hides the chat panel itself. Belt-and-braces; a no-op if the demo's local
  server has cheats off.
- ``tv_nochat 1`` — EXISTS but is **only** "Don't receive chat messages from
  other SourceTV spectators". It does NOT cover relayed server console lines
  — the previous fix relied on it and the text kept showing. Kept as a
  harmless extra.
- ``tv_show_allchat 0`` — EXISTS (``gamedll release``): stops the demo
  server relaying all chat into the playback view.
- ``closecaption 0`` — EXISTS: "Enable close captioning." **This is what was
  still drawing radio/voice lines on screen** after ``cl_showtextmsg 0``:
  radio commands and voice lines surface as close captions, which the
  text-message cvar does not touch. ``cc_subtitles 0`` (voice-overs only) is
  set alongside it so a config that re-enables captions still hides the
  sound-effect lines.
- ``tv_relayradio 0`` — EXISTS: "Relay team radio commands to TV: 0=off, 1=on".
  The radio text relayed into a spectator/TV view is its own switch.
- ``sv_ignoregrenaderadio 1`` — EXISTS (gamedll): "Turn off Fire in the hole
  messages". Server-side, so it may be refused on the demo's local server;
  harmless and correct where it lands.
- ``cl_showtextmsg 0`` does NOT reliably kill the ``Console: ...`` prints on
  current builds, and ``hidehud 128`` / ``screenmessage_show`` are
  CHEAT-protected (a render cfg can never flip them without sv_cheats).
  The display kill is the CLIENT timer ``cl_screenmessage_notifytime``
  (developmentonly clientdll: settable, 0s display hides the bottom-left
  ``Console: ...`` / ``[ALL] ...`` list no matter when CSDM's seek printed
  them — which also closes the round-1 seek race). The bare
  ``screenmessage_notifytime`` (no ``cl_``) is the SERVER-side timer and does
  NOT hide the client display (wrong-cvar bug: frames proved the full set
  without the ``cl_`` line still draws, 2026-10-07).
- ``cl_chatfilters`` — **DOES NOT EXIST in CS2** (no such convar; the old
  ``cl_chatfilters 63`` lines were unknown-command no-ops).
- ``tv_relaytextchat`` / ``hud_saytext_time`` — DO NOT EXIST in CS2 either.

The list is emitted into the launch autoexec (in case a path ever honors
it) as well as the per-sequence cfg. NOTE: pixel-proven 2026-10-07 that the
launch autoexec does NOT reach the CSDM render session, so only the
per-sequence cfg matters there — and it executes after CSDM's seek, i.e.
after the prints. Print-gating cvars therefore cannot win on this path;
only display gates had a chance, and the bitprobe cleared them too.

Cheat-gate mechanism: ``hidehud 128`` and ``screenmessage_show`` are
CHEAT-protected, so setting them on a cheats-off server is a silent no-op.
``sv_cheats 1`` flips on the local demo server, and HLAE ``mirv_cvar_hack``
force-applies the two display kills bypassing cheat protection entirely.
We deliberately do NOT copy CS2UtilArchive's ``cl_chatfilters`` /
``hud_saytext_time`` / ``tv_relaytextchat`` hack lines — none of those cvars
exist in the current CS2 dump, so the hack targets nothing there.

VERDICT 2026-10-07 (NiKo Mirage, five frame-proven renders): none of the
above hides the bottom-left chat panel in CSDM demo-playback renders — not
even ``hidehud 1023`` (every low bit incl. "hide everything" left the full
HUD drawn), and the launch autoexec provably never reaches the render
session (a launch-only ``cl_hud_color`` marker left pixel-identical HUD
colors across four renders). Per explicit user decision there is no pixel
cover-up: it is cfg suppression or nothing — this list stays as the full
best-effort for every render path.
"""

from __future__ import annotations

CHAT_HIDE_CFG: list[str] = [
    "sv_cheats 1",
    # HLAE force-apply: bypasses cheat protection entirely (no cheats
    # needed). The sequence cfg runs with the hook engaged, so these land.
    "mirv_cvar_hack hidehud 128",
    "mirv_cvar_hack screenmessage_show 0",
    "cl_showtextmsg 0",
    "hidehud 128",
    "tv_nochat 1",
    "tv_show_allchat 0",
    # Radio / voice lines are drawn as CLOSE CAPTIONS, a different subsystem
    # from the TextMsg prints (cl_showtextmsg 0 does not touch them). All four
    # verified against the live CS2 convar dump (2026-10).
    "closecaption 0",
    "cc_subtitles 0",
    "tv_relayradio 0",
    "sv_ignoregrenaderadio 1",
    # On-screen console/chat text (bottom-left `Console: ...` + `[ALL] ...`
    # share one display list). `screenmessage_show` defaults to -1 = enabled
    # when vgui is absent (the render case) but is cheat-protected, so the cfg
    # line is only an attempt; the worker is the CLIENT display timer
    # `cl_screenmessage_notifytime` — 0s display hides the lines no matter
    # when CSDM's seek printed them. The bare `screenmessage_notifytime`
    # (no `cl_`) is the server-side timer and does NOT hide the client
    # display.
    "screenmessage_show 0",
    "cl_screenmessage_notifytime 0",
]
