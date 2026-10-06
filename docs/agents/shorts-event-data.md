# V1 raw event evidence for Shorts data

The event-evidence layer is implemented in `cs2archive/shorts/event_data.py` and
included by `cs2archive.highlights.build_action_timeline`. It records engine
facts, not new Short categories or quality scores.

## Kill-feed evidence

Every detailed `kills_all` record contains `kill_event` with:

- `headshot`, `assistedflash`, `attackerblind`, `noscope`, `thrusmoke`,
  `attackerinair`.
- `penetrated` (the engine penetration count).
- `assister_steamid` and `assister_name` (do not infer a flash assist solely
  because an assister exists).
- `distance`, `dmg_health`, `dmg_armor`, `hitgroup`, `weapon`.
- Weapon item IDs/original-owner ID and the existing dominated/revenge/wipe
  event values.

Missing/invalid values are null; missing booleans are not guessed as false.
Steam64/item identity fields are serialized as strings without float rounding.
Distance is preserved as the engine event value, not reinterpreted as a derived
geometry measurement. The existing inferred `blinded_by` field remains distinct
from the authoritative `assistedflash` flag/assister attribution.

## Other already-available engine data

`raw_events` retains the complete parsed tables, before pro/gameplay filters:

- `player_death`: every field/row, including any world, suicide or team deaths.
- `player_hurt`: all gun and utility damage, health/armour/hitgroup and any other
  columns supplied by the parser.
- `weapon_fire`: raw shot events, including shots with no recorded hit.

Each table has `columns` and `rows`, so field availability is explicit.
`raw_player_info` retains player identity/team information already parsed from
the demo. This is basic evidence for later preprocessing; collecting it does not
attempt to identify popflash setups, fakes, jump-shot quality or tactical intent.

`event_data_version=1` identifies the schema. `ensure_action_timeline` rebuilds
older caches lacking this marker, so reuse cannot silently omit the new fields.
The old pro-involved `kills` list retains its legacy schema; enriched POV slices
use `kills_all` and therefore carry the nested `kill_event` facts.

## V2 scope

Richer audio, detailed utility trajectory/effect reconstruction, coordinated
team tactics and interpretations of intent stay outside this raw-event addition.
The LOS/timing work discussed separately can consume these raw shots/hits/kills;
the event flags themselves should not be re-inferred from LOS or titles.

## Verification

On FURIA–BetBoom Mirage, a temporary export retained 110 death events, 487 hurt
events, 2,859 shots and all ten players. It preserved seven flash assists, two
blinded-attacker kills, five through-smoke kills and eight penetration kills.
No-scope and airborne fields were present with zero positive occurrences.
The event/cache/timeline regression suites pass 62 tests; the lint gate passes.
