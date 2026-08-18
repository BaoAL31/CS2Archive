"""Avatar-box geometry for the CS2 top scoreboard strip, keyed by resolution.

Each entry maps the two team blocks (LEFT / RIGHT, as they appear on screen)
to their 5 avatar box x-ranges. All 10 boxes share the same vertical band
(``y0``..``y1``) in a given resolution, so only x varies per box.

Player -> box mapping is NOT the box color: within a team, box position
(left->right) equals the player's slot order (``DemoParser.parse_player_info``
row order). The POV side (which block is the POV player's team) is determined
per match, not stored here.

Verified against renders:
  - 1920x1080 (16:9 native): HeavyGod "team_doublemagic vs team_doxoN - dust2"  (re-derived from live frame)
  - 1280x960  (4:3):         donk "team_donk666 vs team_KiMaRR - Mirage"          (recorded from prior
                             scoreboard-render analysis; the source frame was since deleted, so these values
                             should be re-confirmed against a fresh donk frame before relying on them)
"""
AVATAR_BOXES = {
    "1920x1080": {
        "aspect": "16:9",
        "y0": 16,
        "y1": 65,
        "LEFT": [
            (664, 711), (716, 763), (768, 815), (820, 867), (872, 921),
        ],
        "RIGHT": [
            (1000, 1048), (1050, 1101), (1104, 1152), (1156, 1204), (1208, 1257),
        ],
    },
    "1280x960": {
        "aspect": "4:3",
        "y0": 14,
        "y1": 57,
        "LEFT": [
            (378, 421), (424, 467), (470, 513), (516, 559), (562, 605),
        ],
        "RIGHT": [
            (674, 717), (720, 763), (766, 809), (812, 855), (858, 901),
        ],
    },
    # 1152x864 (4:3): detected from second-half scoreboard colored borders.
    "1152x864": {
        "aspect": "4:3",
        "y0": 12,
        "y1": 51,
        "LEFT": [
            (338, 377), (380, 419), (422, 461), (464, 503), (506, 545),
        ],
        "RIGHT": [
            (608, 647), (650, 689), (692, 731), (734, 773), (776, 815),
        ],
    },
}


def boxes_for_resolution(width: int, height: int) -> dict | None:
    """Return the avatar-box config for ``width``x``height``, or None."""
    return AVATAR_BOXES.get(f"{width}x{height}")
