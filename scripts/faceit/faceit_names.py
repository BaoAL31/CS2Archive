"""FACEIT name canonicalization + avatar resolution.

FACEIT demo headers store player nicks in arbitrary case (often lowercase,
e.g. "niko", "teses"). We map them to the canonical nickname from
.data/player_accounts.json (e.g. "NiKo", "TeSeS") so titles/thumbnails show
proper casing and can locate the player's avatar in demos/avatars/.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_PATH = PROJECT_ROOT / ".data" / "player_accounts.json"
AVATAR_DIR = PROJECT_ROOT / "demos" / "avatars"

# Cache: lowercase demo nick -> canonical nickname
_CANON: dict[str, str] = {}
# FACEIT player_id -> canonical nickname (stable, unique match key)
_FACEIT_IDS: dict[str, str] = {}
# steam_id_64 -> canonical nickname (second stable match key)
_STEAM_IDS: dict[str, str] = {}
# canonical nickname -> FACEIT nick to query the API with
_FACEIT_NICK: dict[str, str] = {}
_LOADED = False


def _load() -> None:
    global _CANON, _FACEIT_IDS, _STEAM_IDS, _FACEIT_NICK, _LOADED
    if _LOADED:
        return
    _LOADED = True
    if not ACCOUNTS_PATH.exists():
        return
    try:
        import json
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        players = data if isinstance(data, list) else data.get("players", [])
        for p in players:
            nick = (p.get("nickname") or "").strip()
            if nick:
                _CANON[nick.lower()] = nick
                fn = p.get("faceit_nickname") or ""
                if fn:
                    _FACEIT_NICK[nick] = fn
                    _FACEIT_NICK[nick.lower()] = fn
                    # Live FACEIT nick drifts on rename (s1mpleWRLD ->
                    # s1mplecsgod). Alias it so demo headers / stats keys
                    # still resolve to canonical nick.
                    _CANON.setdefault(fn.lower(), nick)
            fid = p.get("faceit_id")
            if isinstance(fid, str) and fid and fid != "-1":
                _FACEIT_IDS[fid] = nick
            sid = p.get("steam_id")
            if sid:
                _STEAM_IDS[str(sid)] = nick
    except Exception:
        pass


def canonical_nick(demo_nick: str) -> str:
    """Return canonical nickname for a demo nick, or the original if unknown."""
    _load()
    if not demo_nick:
        return demo_nick
    return _CANON.get(demo_nick.lower(), demo_nick)


def known_pro_faceit_ids() -> dict[str, str]:
    """FACEIT player_id -> canonical nickname for all known pros."""
    _load()
    return dict(_FACEIT_IDS)


def known_pro_steam_ids() -> dict[str, str]:
    """steam_id_64 -> canonical nickname for all known pros."""
    _load()
    return dict(_STEAM_IDS)


def faceit_nick(nickname: str) -> str:
    """Return the FACEIT query nick for a canonical nickname.

    player_accounts may store a FACEIT nick that differs from the canonical
    display nickname (e.g. display "s1mple" vs FACEIT "holaaaa").
    """
    _load()
    return _FACEIT_NICK.get(nickname) or _FACEIT_NICK.get(nickname.lower()) or nickname


def avatar_path(demo_nick: str) -> Path | None:
    """Path to the player's avatar PNG, or None if not cached.

    Avatars live under demos/avatars/{nick}/{source}/{nick}.png where source is
    one of faceit/hltv.
    """
    nick = canonical_nick(demo_nick)
    base = AVATAR_DIR / nick
    if not base.is_dir():
        return None
    for src in ("hltv", "faceit"):
        for ext in (".png", ".jpg"):
            p = base / src / f"{nick}{ext}"
            if p.exists():
                return p
    return None


def known_pros() -> set[str]:
    """Lowercase set of all known pro nicks (from accounts)."""
    _load()
    return set(_CANON.keys())
