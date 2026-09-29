"""Weapon knowledge shared by the Shorts detector and the Highlight Reel timeline.

CR-11. This module exists to break a cross-product import cycle. The two products used to own
half the vocabulary each:

* ``shorts/build_short_timeline.py`` held ``_WEAPON_TIER`` (short name -> tier) and the tier
  predicates.
* ``highlights/build_action_timeline.py`` held ``_CS2_WEAPON_IDS`` (def-index -> display name)
  and ``_resolve_weapon_id``.

Each then imported the other's private symbol, deferred inside a function to survive the cycle,
and the shorts side wrapped that import in ``except Exception: victim_weapon_map = {}``. If the
cycle ever broke, the victim-weapon map emptied, ``is_wallbang_rifle`` was handed empty weapon
strings, and **wallbang detection silently produced nothing** — no error, no warning.

Both vocabularies live here now, so both products depend on this module and neither on the other.
"""
from __future__ import annotations

# Def-index -> display name. CS2 item definition indices, as emitted in demo weapon-fire events.
WEAPON_IDS: dict[int, str] = {
    # Pistols
    1: "Desert Eagle", 2: "Dual Berettas", 3: "Five-SeveN", 4: "Glock-18",
    30: "Tec-9", 32: "P2000", 36: "P250", 61: "USP-S",
    63: "CZ75-Auto", 64: "R8 Revolver",
    # Rifles
    7: "AK-47", 8: "AUG", 10: "FAMAS", 13: "Galil AR",
    16: "M4A4", 39: "SG 553", 60: "M4A1-S",
    # Snipers
    9: "AWP", 11: "G3SG1", 38: "SCAR-20", 40: "SSG 08",
    # SMGs
    17: "MAC-10", 19: "P90", 23: "MP5-SD", 24: "UMP-45",
    26: "PP-Bizon", 33: "MP7", 34: "MP9",
    # Heavy
    14: "M249", 25: "XM1014", 27: "MAG-7", 28: "Negev",
    29: "Sawed-Off", 35: "Nova",
    # Equipment
    31: "Zeus x27", 42: "Knife", 49: "C4",
    50: "Kevlar Vest", 51: "Kevlar + Helmet", 52: "Defuse Kit",
    54: "Rescue Kit", 55: "Medi-Shot", 57: "Healthshot", 59: "Knife",
    80: "Shield",
    # Grenades
    43: "Flashbang", 44: "HE Grenade", 45: "Smoke Grenade",
    46: "Molotov", 47: "Decoy Grenade", 48: "Incendiary",
    68: "TA Grenade", 81: "Frag Grenade",
    # Knives
    500: "Bayonet", 503: "Karambit", 505: "Flip Knife",
    506: "Gut Knife", 507: "M9 Bayonet", 508: "Huntsman Knife",
    509: "Falchion Knife", 512: "Bowie Knife", 514: "Butterfly Knife",
    515: "Shadow Daggers", 516: "Paracord Knife", 517: "Survival Knife",
    518: "Ursus Knife", 519: "Navaja Knife", 520: "Nomad Knife",
    521: "Stiletto Knife", 522: "Talon Knife", 523: "Classic Knife",
    525: "Skeleton Knife",
    # Danger Zone tools
    85: "Tablet", 86: "Axe", 87: "Hammer", 88: "Wrench", 89: "Spanner",
}

# Weapon name -> tier. Handles both player_death short names (e.g. "m4a1") and display
# names (e.g. "M4A4").
WEAPON_TIER: dict[str, int] = {
    # Tier 0: Melee / eco items
    "knife": 0, "world": 0, "zeus": 0, "zeus x27": 0,
    "c4": 0, "planted_c4": 0,
    "bayonet": 0, "karambit": 0, "flip knife": 0, "gut knife": 0,
    "m9 bayonet": 0, "huntsman knife": 0, "falchion knife": 0,
    "bowie knife": 0, "butterfly knife": 0, "shadow daggers": 0,
    "paracord knife": 0, "survival knife": 0, "ursus knife": 0,
    "navaja knife": 0, "nomad knife": 0, "stiletto knife": 0,
    "talon knife": 0, "classic knife": 0, "skeleton knife": 0,
    "axe": 0, "hammer": 0, "wrench": 0, "spanner": 0, "tablet": 0,
    # Tier 1: Pistols
    "glock": 1, "glock-18": 1, "usp_silencer": 1, "usp-s": 1,
    "p2000": 1, "p250": 1, "deagle": 1, "desert eagle": 1,
    "elite": 1, "dual berettas": 1, "fiveseven": 1, "five-seveN": 1,
    "tec9": 1, "tec-9": 1, "cz75": 1, "cz75-auto": 1,
    "r8revolver": 1, "r8 revolver": 1,
    # Tier 2: Shotguns
    "nova": 2, "xm1014": 2, "mag7": 2, "mag-7": 2,
    "sawedoff": 2, "sawed-off": 2,
    # Tier 3: SMGs
    "mac10": 3, "mac-10": 3, "mp5sd": 3, "mp5-sd": 3,
    "mp7": 3, "mp9": 3, "p90": 3, "ump45": 3, "ump-45": 3,
    "bizon": 3, "pp-bizon": 3, "m249": 3, "negev": 3,
    # Tier 4: Rifles & Snipers
    "ak47": 4, "ak-47": 4, "m4a4": 4, "m4a1": 4,
    "m4a1_silencer": 4, "m4a1-s": 4, "famas": 4, "galilar": 4,
    "galil ar": 4, "aug": 4, "sg553": 4, "sg 553": 4,
    "awp": 4, "g3sg1": 4, "scar20": 4, "scar-20": 4,
    "ssg08": 4, "ssg 08": 4,
    # Tier 5: Special (always count — grenades, fire, bomb)
    "inferno": 5, "hegrenade": 5, "he grenade": 5,
    "flashbang": 5, "smokegrenade": 5, "smoke grenade": 5,
    "decoy": 5, "decoy grenade": 5, "incendiary": 5,
    "molotov": 5, "tagrenade": 5, "ta grenade": 5,
    "fraggrenade": 5, "frag grenade": 5,
}

# Tier 0 names that are NOT knives (nothing to do with a knife/Zeus round).
JUNK_MELEE = frozenset({
    "world", "c4", "planted_c4", "tablet", "axe", "hammer", "wrench", "spanner",
})

UNKNOWN_TIER = -1


def resolve_weapon_id(def_idx: int) -> str:
    """Display name for a CS2 item definition index."""
    return WEAPON_IDS.get(def_idx, f"item_{def_idx}")


def weapon_tier(weapon: str) -> int:
    """Tier for a weapon name, or ``UNKNOWN_TIER`` when unrecognised."""
    return WEAPON_TIER.get(str(weapon or "").strip().lower(), UNKNOWN_TIER)


def is_wallbang_rifle(weapon: str) -> bool:
    """Rifles and snipers (tier 4). Pistols through a box are not a wallbang Short."""
    return weapon_tier(weapon) >= 4


def is_zeus(weapon: str) -> bool:
    return str(weapon or "").strip().lower() in ("zeus", "zeus x27")


def is_knife_or_zeus(weapon: str) -> bool:
    w = str(weapon or "").strip().lower()
    if is_zeus(w):
        return True
    if "knife" in w:
        return True
    if w in WEAPON_TIER and WEAPON_TIER[w] == 0 and w not in JUNK_MELEE:
        return True
    return False


# Headshot bonus (hook quality stacking). Every headshot scores the same —
# a headshot is a headshot, whatever the gun. Bodies score nothing. Each
# kill counts once no matter how many fused candidates reference its tick;
# two headshots in one segment always outscore one.
HS_BONUS_HEADSHOT = 50.0


def headshot_bonus(weapon: str) -> float:
    """Quality points for one headshot kill with ``weapon`` (0 for bodies).

    Flat across guns; non-guns (nades/knives) score 0.
    """
    w = str(weapon or "").strip().lower()
    if weapon_tier(w) in (1, 2, 3, 4):
        return HS_BONUS_HEADSHOT
    return 0.0
