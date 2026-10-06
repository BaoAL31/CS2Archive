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
- ``cl_chatfilters`` — **DOES NOT EXIST in CS2** (no such convar; the old
  ``cl_chatfilters 63`` lines were unknown-command no-ops).
- ``tv_relaytextchat`` / ``hud_saytext_time`` — DO NOT EXIST in CS2 either.

Because the messages are printed while the demo plays, these MUST be in the
launch autoexec (active from CS2 startup, before the demo loads) as well as in
the per-sequence cfg — a sequence cfg alone can lose the race against a print
that happens during the seek.
"""

from __future__ import annotations

CHAT_HIDE_CFG: list[str] = [
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
]
