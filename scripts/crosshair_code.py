from __future__ import annotations

_DICT = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
_DICT_LEN = len(_DICT)

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

def crosshair_to_convars(ch: dict) -> list[str]:
    # CS2 cl_crosshaircolor: 0=green, 1=red, 2=blue, 3=yellow, 4=teal/cyan.
    # cl_crosshaircolor_r/g/b cvars are IGNORED by CS2 (custom RGB not available via cfg).
    # Share code colors 0-3 map directly to CS2 presets. Colors 4+ all clamp to 4 (teal).
    cs2_color = ch["color"] if ch["color"] in {0, 1, 2, 3} else 4
    return [
        f"cl_crosshairstyle {ch['style']}",
        f"cl_crosshairsize {ch['length']}",
        f"cl_crosshairthickness {ch['thickness']}",
        f"cl_crosshairgap {ch['gap']}",
        f"cl_crosshair_drawoutline {1 if ch['outlineEnabled'] else 0}",
        f"cl_crosshair_outlinethickness {ch['outline']}",
        f"cl_crosshairdot {1 if ch['centerDotEnabled'] else 0}",
        f"cl_crosshaircolor {cs2_color}",
        f"cl_crosshairalpha {ch['alpha']}",
        f"cl_crosshairusealpha {1 if ch['alphaEnabled'] else 0}",
        f"cl_crosshair_recoil {1 if ch['followRecoil'] else 0}",
        f"cl_crosshair_t {1 if ch['tStyleEnabled'] else 0}",
        f"cl_crosshairgap_useweaponvalue {1 if ch['deployedWeaponGapEnabled'] else 0}",
        f"cl_fixedcrosshairgap {ch['fixedCrosshairGap']}",
        f"cl_crosshair_sniper_width 1",
        f"cl_crosshair_dynamic_splitdist {ch['splitDistance']}",
        f"cl_crosshair_dynamic_splitalpha_innermod {ch['innerSplitAlpha']}",
        f"cl_crosshair_dynamic_splitalpha_outermod {ch['outerSplitAlpha']}",
        f"cl_crosshair_dynamic_maxdist_splitratio {ch['splitSizeRatio']}",
    ]
