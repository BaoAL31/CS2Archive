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


def canonical_nick(
    steam_id: str,
    fallback: str = "",
    accounts_path: Path | str | None = None,
) -> str:
    """Canonical (prosettings) nickname for a steam64 from player_accounts.json.

    Hook timelines carry the demo's raw player name, which is often wrong or a
    bare steam64 — feeding that to the prosettings lookup misses and drops the
    render onto the demo share-code fallback (a different crosshair from the
    POV). Resolve by steam_id first so every renderer uses the same input.
    Returns ``fallback`` when unlisted.
    """
    root = Path(__file__).resolve().parents[1]
    path = Path(accounts_path) if accounts_path else root / ".data" / "player_accounts.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        players = data if isinstance(data, list) else data.get("players", [])
        for p in players:
            if str(p.get("steam_id") or "") == str(steam_id):
                nick = (p.get("nickname") or "").strip()
                if nick:
                    return nick
                break
    except Exception:
        pass
    return fallback


def demo_crosshair_cvars(
    steam_id: str,
    demo_path: Path | str,
    *,
    csdm_cmd: str,
    screen_height: int = 1440,
) -> list[str]:
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
                    from cs2archive.crosshair_code import decode_crosshair, crosshair_to_convars
                    cvars = crosshair_to_convars(
                        decode_crosshair(code), screen_height=screen_height)
                break
    return cvars


def resolve_crosshair_cvars(
    nickname: str | None,
    steam_id: str,
    demo_path: Path | str,
    *,
    csdm_cmd: str,
    screen_height: int = 1440,
    demo_paths: list[Path | str] | None = None,
) -> tuple[list[str], dict]:
    """Prosettings crosshair for *nickname*, demo share code fallback.

    Blank/"unknown" nicknames skip prosettings and go straight to the demo.
    ``demo_paths`` covers split demos (p1/p2); the first part with a share
    code wins. Returns (cvars, info).
    """
    nick = (nickname or "").strip()
    if not nick or nick.lower() == "unknown":
        nick = ""
    parts = list(demo_paths) if demo_paths else [demo_path]

    def fallback() -> list[str]:
        for part in parts:
            cvars = demo_crosshair_cvars(
                steam_id, part, csdm_cmd=csdm_cmd, screen_height=screen_height)
            if cvars:
                return cvars
        return []

    return resolve_crosshair(nick, fallback, screen_height=screen_height)
