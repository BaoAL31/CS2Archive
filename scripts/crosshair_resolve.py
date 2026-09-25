"""Single crosshair system for every renderer (POV, Shorts, Highlights, Hook).

Prosettings-first, demo share code fallback — the same rule everywhere, so a
player's crosshair never depends on which product rendered the clip.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from scrapers.prosettings import resolve_crosshair


def demo_crosshair_cvars(steam_id: str, demo_path: Path | str, *, csdm_cmd: str) -> list[str]:
    """Decode one demo's embedded share code for *steam_id* into cvars."""
    cvars: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [csdm_cmd, "json", str(Path(demo_path).resolve()), "--output-folder", tmp],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return cvars
        jf = list(Path(tmp).glob("*.json"))
        if not jf:
            return cvars
        data = json.loads(jf[0].read_text(encoding="utf-8"))
        for pl in data.get("players", []):
            if pl.get("steamId") == steam_id:
                code = pl.get("crosshairShareCode")
                if code:
                    from crosshair_code import decode_crosshair, crosshair_to_convars
                    cvars = crosshair_to_convars(decode_crosshair(code))
                break
    return cvars


def resolve_crosshair_cvars(
    nickname: str | None,
    steam_id: str,
    demo_path: Path | str,
    *,
    csdm_cmd: str,
) -> tuple[list[str], dict]:
    """Prosettings crosshair for *nickname*, demo share code fallback.

    Blank/"unknown" nicknames skip prosettings and go straight to the demo.
    Returns (cvars, info) where info is {"source": "prosettings"|"demo"|"none"}.
    """
    nick = (nickname or "").strip()
    if not nick or nick.lower() == "unknown":
        nick = ""
    return resolve_crosshair(
        nick, lambda: demo_crosshair_cvars(steam_id, demo_path, csdm_cmd=csdm_cmd))
