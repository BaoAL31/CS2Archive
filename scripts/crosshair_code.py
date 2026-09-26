from __future__ import annotations

import math

_DICT = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
_DICT_LEN = len(_DICT)

# Pre–Rush Hour size/thickness were 1/480 of screen height (CS:GO scale).
# Gap was raw pixels as (4 + gap) empty space beside the crossing bar.
# Post–1.41.8.2 length/thickness/gap are plain pixels counted from centre.
_OLD_SCALE_DENOM = 480.0

# Observed post–Rush Hour: Classic Static still accepts style 4 alongside
# the new length/gap/thickness cvars (vora.tools). Leave other old indices
# unchanged until SyberiaK publishes the final style enum.
STYLE_OLD_TO_NEW: dict[int, int] = {}

PRESET_RGB = {
    0: (250, 50, 50),
    1: (50, 250, 50),
    2: (250, 250, 50),
    3: (50, 50, 250),
    4: (50, 250, 250),
}


def _sharecode_to_bytes(code: str) -> list[int]:
    raw = code.replace("CSGO-", "").replace("-", "")
    total = 0
    for ch in reversed(raw):
        total = total * _DICT_LEN + _DICT.index(ch)
    hex_str = hex(total)[2:].zfill(36)
    return [int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2)]

def decode_crosshair(code: str) -> dict:
    b = _sharecode_to_bytes(code)
    size = sum(b[1:]) % 256
    if b[0] != size:
        raise ValueError("checksum mismatch")
    return {
        "gap": (b[2] - 256 if b[2] > 127 else b[2]) / 10,
        "outline": b[3] / 2,
        "red": b[4],
        "green": b[5],
        "blue": b[6],
        "alpha": b[7],
        "splitDistance": b[8] & 7,
        "followRecoil": bool((b[8] >> 4) & 8),
        "fixedCrosshairGap": (b[9] - 256 if b[9] > 127 else b[9]) / 10,
        "color": b[10] & 7,
        "outlineEnabled": bool(b[10] & 8),
        "innerSplitAlpha": (b[10] >> 4) / 10,
        "outerSplitAlpha": (b[11] & 0xF) / 10,
        "splitSizeRatio": (b[11] >> 4) / 10,
        "thickness": b[12] / 10,
        "centerDotEnabled": bool((b[13] >> 4) & 1),
        "deployedWeaponGapEnabled": bool((b[13] >> 4) & 2),
        "alphaEnabled": bool((b[13] >> 4) & 4),
        "tStyleEnabled": bool((b[13] >> 4) & 8),
        "style": (b[13] & 0xF) >> 1,
        "length": b[14] / 10,
    }

def _sbyte(value: float) -> int:
    return int(round(value)) % 256


def encode_crosshair(ch: dict, *, version: int = 1) -> str:
    """Pack a decode-style settings dict back into a share code.

    Byte 1 carries the format version (observed as 1 on current codes); if a
    newer game version bumps the accepted version, re-encoding with that
    version is the mechanical half of an old-code -> new-code conversion.
    """
    b = [0] * 18
    b[1] = version & 0xFF
    b[2] = _sbyte(ch["gap"] * 10)
    b[3] = int(round(ch["outline"] * 2)) & 0xFF
    b[4] = int(ch["red"]) & 0xFF
    b[5] = int(ch["green"]) & 0xFF
    b[6] = int(ch["blue"]) & 0xFF
    b[7] = int(ch["alpha"]) & 0xFF
    b[8] = (int(ch["splitDistance"]) & 7) | (0x80 if ch["followRecoil"] else 0)
    b[9] = _sbyte(ch["fixedCrosshairGap"] * 10)
    b[10] = (
        (int(ch["color"]) & 7)
        | (8 if ch["outlineEnabled"] else 0)
        | ((int(round(ch["innerSplitAlpha"] * 10)) & 0xF) << 4)
    )
    b[11] = (
        (int(round(ch["outerSplitAlpha"] * 10)) & 0xF)
        | ((int(round(ch["splitSizeRatio"] * 10)) & 0xF) << 4)
    )
    b[12] = int(round(ch["thickness"] * 10)) & 0xFF
    b[13] = (
        ((int(ch["style"]) & 7) << 1)
        | ((1 if ch["centerDotEnabled"] else 0) << 4)
        | ((1 if ch["deployedWeaponGapEnabled"] else 0) << 5)
        | ((1 if ch["alphaEnabled"] else 0) << 6)
        | ((1 if ch["tStyleEnabled"] else 0) << 7)
    )
    b[14] = int(round(ch["length"] * 10)) & 0xFF
    b[0] = sum(b[1:]) % 256
    total = int.from_bytes(bytes(b), "big")
    chars = []
    while total > 0:
        total, rem = divmod(total, _DICT_LEN)
        chars.append(_DICT[rem])
    # Least-significant digit first (mirrors _sharecode_to_bytes, which
    # weights the last char highest); pad out with zero digits on the right.
    raw = "".join(chars).ljust(25, _DICT[0])
    if len(raw) != 25:
        raise ValueError("settings out of share-code range")
    return "CSGO-" + "-".join(raw[i:i + 5] for i in range(0, 25, 5))


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def effective_crosshair_height(requested: int) -> int:
    """HLAE cannot exceed the desktop; convert pixels for the height CS2 actually runs."""
    try:
        import ctypes
        desktop_h = int(ctypes.windll.user32.GetSystemMetrics(1))  # SM_CYSCREEN
        if desktop_h >= 600:
            return min(int(requested), desktop_h)
    except Exception:
        pass
    return int(requested)


def looks_like_old_scale(length: float, thickness: float, gap: float) -> bool:
    """Heuristic: pre–Rush Hour values use a float scale and allow negative gap."""
    if gap < 0:
        return True
    if length != int(length) or thickness != int(thickness):
        return True
    return False


def old_scale_to_pixels(
    length: float,
    thickness: float,
    gap: float,
    *,
    screen_height: int = 1440,
) -> tuple[int, int, int]:
    """Convert pre–Rush Hour length/thickness/gap to post-1.41.8.2 pixels.

    Formula matches community migrators (height/480 for size+thickness;
    gap = old empty pixels + ceil(new_thickness/2)). Values are for the
    resolution CS2 is running when the cvars are applied — HLAE POV
    captures at 2560x1440, so default height is 1440.
    """
    scale = screen_height / _OLD_SCALE_DENOM
    new_length = max(0, min(255, _round_half_up(length * scale)))
    new_thickness = max(1, min(31, _round_half_up(thickness * scale)))
    old_empty = int(4 + gap)  # fractions dropped, as in the old renderer
    new_gap = old_empty + math.ceil(new_thickness / 2.0)
    new_gap = max(0, min(128, int(new_gap)))
    return new_length, new_thickness, new_gap


def crosshair_to_convars(ch: dict, *, screen_height: int = 1440) -> list[str]:
    """Emit CS2 1.41.8+ crosshair cvars (pixel length/gap/thickness).

    Pre–Rush Hour share-code fields are converted for ``screen_height``.
    Already-pixel values (non-negative integer gap + integer length/thickness)
    are passed through with the new cvar names only.
    """
    length = float(ch["length"])
    thickness = float(ch["thickness"])
    gap = float(ch["gap"])
    if looks_like_old_scale(length, thickness, gap):
        length_i, thickness_i, gap_i = old_scale_to_pixels(
            length, thickness, gap, screen_height=screen_height,
        )
    else:
        length_i = max(0, min(255, int(round(length))))
        thickness_i = max(0, min(31, int(round(thickness))))
        gap_i = max(0, min(128, int(round(gap))))

    style = int(ch["style"])
    style = STYLE_OLD_TO_NEW.get(style, style)

    color = ch["color"] if ch["color"] in {0, 1, 2, 3, 4} else 5
    if color == 5:
        r, g, b = int(ch["red"]), int(ch["green"]), int(ch["blue"])
    else:
        r, g, b = PRESET_RGB[color]

    alpha = int(ch["alpha"]) if ch.get("alphaEnabled", True) else 255
    outline = 1 if ch["outlineEnabled"] else 0  # 2 = half-outline (unused)

    lines = [
        f"cl_crosshairstyle {style}",
        f"cl_crosshair_length {length_i}",
        f"cl_crosshair_thickness {thickness_i}",
        f"cl_crosshair_gap {gap_i}",
        f"cl_crosshair_drawoutline {outline}",
        f"cl_crosshairdot {1 if ch['centerDotEnabled'] else 0}",
        f"cl_crosshaircolor_r {r}",
        f"cl_crosshaircolor_g {g}",
        f"cl_crosshaircolor_b {b}",
        f"cl_crosshaircolor_a {alpha}",
        f"cl_crosshair_recoil {1 if ch['followRecoil'] else 0}",
        f"cl_crosshair_t {1 if ch['tStyleEnabled'] else 0}",
        f"cl_crosshair_sniper_width 1",
        f"cl_crosshair_dynamic_splitdist {ch['splitDistance']}",
        f"cl_crosshair_dynamic_splitalpha_innermod {ch['innerSplitAlpha']}",
        f"cl_crosshair_dynamic_splitalpha_outermod {ch['outerSplitAlpha']}",
        f"cl_crosshair_dynamic_maxdist_splitratio {ch['splitSizeRatio']}",
    ]
    return lines
