"""Capture-resolution policy for POV / hook / intro renders.

Policy (CR-17, 2026-10): **ignore the pro's own in-game resolution**. Capture at
the highest resolution this machine can render *while keeping the POV's aspect
ratio*, then let ``concat_rounds`` stretch that native frame to the 2560x1440
upload target. No letterboxing or pillarboxing is ever introduced — the
anamorphic stretch from the native aspect to 16:9 is the intended look.

Rationale: a pro's prosettings resolution (e.g. TeSeS 1024x768 4:3) is a
competitive choice that has nothing to do with capture fidelity. Upstream
rendering at 1024x768 and then upscaling 2.5x made the delivered video soft;
capturing at the machine maximum (1080p-tall, see ``max_height``) keeps the
aspect-ratio framing (and therefore FOV/aim feel) while giving the encoder
~2x the pixels.

The height ceiling exists because CS2/HLAE cannot render a backbuffer taller
than the desktop (``crosshair_code.effective_crosshair_height`` enforces the
same bound for crosshair pixel conversion). Measured 2026-10 on this machine:
requesting ``1920x1440`` on a 1920x1080 desktop produced a **1920x1080** clip —
HLAE clamps the height to the desktop but keeps the requested width, so the
aspect ratio silently changes to 16:9. Always request a height within the
desktop, or the POV's aspect ratio is lost.
"""

from __future__ import annotations

# Highest capture height this machine can render (desktop height, 1080p here).
# Width follows from the aspect ratio and may exceed the desktop width — CS2
# renders the requested backbuffer regardless of how much of it is on screen.
CAPTURE_MAX_HEIGHT = 1080

# Hard ceiling on the derived width. The desktop width is the safe bound: the
# height clamp is proven (see below), a width clamp is not, and a too-wide
# request would risk the mirror-image failure (width clamped, height kept).
CAPTURE_MAX_WIDTH = 1920

# Fallback when the POV has no aspect ratio on record.
DEFAULT_ASPECT = "16:9"

_ASPECTS: dict[str, float] = {
    "4:3": 4 / 3,
    "5:4": 5 / 4,
    "16:9": 16 / 9,
    "16:10": 16 / 10,
    "21:9": 21 / 9,
    "32:9": 32 / 9,
}


def desktop_height() -> int:
    """Desktop height in physical pixels (HLAE's backbuffer ceiling)."""
    try:
        import ctypes

        h = int(ctypes.windll.user32.GetSystemMetrics(1))  # SM_CYSCREEN
        if h >= 600:
            return h
    except Exception:
        pass
    return CAPTURE_MAX_HEIGHT


def desktop_width() -> int:
    """Desktop width in physical pixels (bound on the derived capture width)."""
    try:
        import ctypes

        w = int(ctypes.windll.user32.GetSystemMetrics(0))  # SM_CXSCREEN
        if w >= 800:
            return w
    except Exception:
        pass
    return CAPTURE_MAX_WIDTH


def parse_aspect(text: str) -> float | None:
    """Parse "4:3" / "16:9" / "16:10" / "1024x768" into a width/height ratio."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if ":" in s:
        a, _, b = s.partition(":")
        if a in _ASPECTS and b in _ASPECTS:
            return _ASPECTS[a] / _ASPECTS[b]
        try:
            aw, ah = float(a), float(b)
        except ValueError:
            return None
        return aw / ah if aw > 0 and ah > 0 else None
    if "x" in s:
        a, _, b = s.partition("x")
        try:
            aw, ah = int(a), int(b)
        except ValueError:
            return None
        return aw / ah if aw > 0 and ah > 0 else None
    return _ASPECTS.get(s)


def capture_size_for_aspect(
    aspect: str | float | None,
    *,
    max_height: int | None = None,
    max_width: int | None = None,
) -> tuple[int, int]:
    """Highest even WxH for ``aspect`` that this machine can render.

    Height is the smaller of ``max_height`` (default: desktop height) and
    ``CAPTURE_MAX_HEIGHT``; width follows. If the derived width busts
    ``max_width`` (default: desktop width) the pair is scaled down together so
    the ratio is preserved.
    """
    if isinstance(aspect, (int, float)):
        ar = float(aspect)
    else:
        ar = parse_aspect(aspect or "") or parse_aspect(DEFAULT_ASPECT)  # type: ignore[assignment]
    if not ar or ar <= 0:
        ar = parse_aspect(DEFAULT_ASPECT)  # type: ignore[assignment]

    # Height is the binding constraint: above the desktop HLAE clamps it and
    # keeps the requested width, silently changing the aspect ratio.
    limit_h = min(int(max_height or desktop_height()), CAPTURE_MAX_HEIGHT)
    h = limit_h if limit_h >= 600 else CAPTURE_MAX_HEIGHT
    w = int(round(h * ar))

    limit_w = int(max_width or min(desktop_width(), CAPTURE_MAX_WIDTH))
    if w > limit_w:
        w = limit_w
        h = int(round(w / ar))

    # HUD/crosshair layouts want even dimensions.
    w -= w % 2
    h -= h % 2
    return w, h


def capture_size_for_steam_id(steam_id: str) -> tuple[int, int] | None:
    """Capture WxH for a Recognised-Pro steam_id, or None when unknown."""
    try:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / ".data" / "player_accounts.json"
        accounts = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    acct = next(
        (a for a in accounts if str(a.get("steam_id")) == str(steam_id)), None
    )
    return capture_size_for_player(acct)


def capture_size_for_player(account: dict | None) -> tuple[int, int] | None:
    """Capture WxH for a player_accounts.json record, or None if unknown.

    Uses only the record's *aspect ratio* — its ``capture_width``/``capture_height``
    (the pro's own resolution) is deliberately ignored.
    """
    if not account:
        return None
    aspect = str(account.get("aspect_ratio") or "")
    if not parse_aspect(aspect):
        aspect = str(account.get("resolution") or "")
    if not parse_aspect(aspect):
        return None
    return capture_size_for_aspect(aspect)
