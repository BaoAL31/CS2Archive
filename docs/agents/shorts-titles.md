# Shorts Titles — Reference

How to write YouTube Short titles for CS2 FACEIT highlights. This is the
hands-on guide with concrete good/bad examples; models should reference this
instead of re-deriving the convention.

## Core rules

1. **Must contain** the PLAYER name, the clip KIND, and the OPPONENT.
2. **Kinds:** clutch (e.g. "1v4 Clutch", "2v5 Clutch", "ACE") or multikill
   ("4K", "5K"). Add the weapon/gun for flavour when it's distinctive (e.g.
   "AK 4K", "M4 + AK 4K").
3. **Opponent label** (FACEIT only — opponent is a random/level-10 lobby, not
   a pro org, so don't name a team):
   - Opponent average ELO **≥ 3000** → put the number in the title:
     `against 3.1k ELOs`, `against 3.2K ELOs`.
   - Opponent average ELO **< 3000** → "level 10" / "level 10 faceit players".
   - The label goes **in the title**, not just the description.
4. **Hashtags** go in the TITLE: `#cs2 #counterstrike` (+ tournament hashtag
   if any). Never a `tags` field, never `#csgo`, never `#Shorts`, no map
   hashtags.
5. **No em-dashes** (`—` / `\u2014`). Use a plain hyphen `-` or restructure.
6. Vary the format — don't repeat one template for every short. Lead with the
   player, the gun, the clutch, or a hook as fits the clip.

## Good examples (approved / shipped-style)

Real, from the current batch — all are "make it creative" wins:

- `kyousuke casually drops an AK ACE against 3.1k ELOs #cs2 #counterstrike`
  (5K, AK, opponent ~3.1k ELO → number in title, casual voice)
- `kyousuke dropping a 4K against 3.2K ELOs #cs2 #counterstrike`
  (4K on Ancient, opponent ~3.2k ELO → number in title)
- `HeavyGod's AK 4K against level 10 faceit players #cs2 #counterstrike`
  (4K on Cache, opponent ~2.8k ELO → "level 10", possessive player + gun)
- `jL 2v5 CLUTCH - drops a 5K ACE to save the round #cs2 #counterstrike`
  (2v5 clutch, opponent ~2.1k ELO → "level 10" implied, no em-dash)
- `2v5? No problem for jL 💀 (5K Ace) #cs2 #counterstrike`
  (2v5 clutch — hook + emoji for personality; opponent ~2.1k ELO)

## Examples that follow the rules but are weaker

- `kyousuke goes off - AK 5K for the round` — fine, but generic "goes off"
  is less specific than naming the ELO/opponent context.
- `kyousuke's M4 + AK 4K on Ancient` — the opponent label is missing from the
  title (only in the description), so it's incomplete per rule 3.

## Prior-convention example (kept for historical reference)

- `donk's 1v3 Clutch + 4K vs MOUZ #cs2 #counterstrike #blastbounty2026`
  (the `vs MOUZ` team form applies to HLTV/tournament matches with a named
  opponent org — NOT FACEIT lobbies, which use the ELO/level-10 label).

## FACEIT-only nuance

The `~X ELOs` figure comes from the backlog card's `opp_avg_elo` (opponent
team average). Threshold: use the number at **≥3000**, "level 10" below that.
