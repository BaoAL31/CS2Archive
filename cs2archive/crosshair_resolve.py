"""Single crosshair system for every renderer (POV, Shorts, Highlights, Hook).

Prosettings-first, demo share code fallback for unlisted players only — the
same rule everywhere, so a player's crosshair never depends on which product
rendered the clip.

Hard-fail contract: this module NEVER returns an empty crosshair into a
render. A total miss raises RuntimeError (non-zero exit, loud log) instead
of shipping the game-default crosshair. Listed players (player_accounts.json
by steam_id) must hit prosettings; the demo share code is never consulted
for them (stale FACEIT/HLTV codes decode fine and would silently ship the
wrong crosshair past every guard).

Successful resolutions are saved per steam_id (`.data/crosshair_cache.json`)
so repeat renders never re-scrape: source settings for prosettings hits, raw
share code for demo hits. Cvars are always re-derived at the requested
screen height — the cache never stores derived cvars, so height changes
can't stale it. Entries refresh after 30 days; a refresh failure raises
rather than serving stale data.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scrapers.prosettings import crosshair_convars, resolve_crosshair

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / ".data" / "crosshair_cache.json"
CACHE_TTL_DAYS = 30


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


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_PATH.parent,
                                         prefix=CACHE_PATH.name, suffix=".tmp",
                                         delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CACHE_PATH)
    finally:
        if tmp is not None and Path(tmp).exists():
            Path(tmp).unlink()


def _cache_entry_fresh(entry: dict) -> bool:
    try:
        updated = datetime.fromisoformat(str(entry.get("updated_at") or ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - updated).total_seconds() / 86400
        return 0 <= age_days < CACHE_TTL_DAYS
    except (TypeError, ValueError):
        return False


def _cvars_from_entry(entry: dict, screen_height: int) -> list[str]:
    """Re-derive cvars from cached source data at the requested height."""
    try:
        if entry.get("source") == "prosettings" and entry.get("settings"):
            return crosshair_convars(entry["settings"], screen_height=screen_height)
        if entry.get("source") == "demo" and entry.get("code"):
            from cs2archive.crosshair_code import decode_crosshair, crosshair_to_convars
            return crosshair_to_convars(
                decode_crosshair(str(entry["code"])), screen_height=screen_height)
    except (ValueError, TypeError, KeyError):
        pass
    return []


def _demo_json_players(
    demo_path: Path | str,
    csdm_cmd: str,
) -> list[dict] | None:
    """Player rows from ``csdm json`` for one demo, or None on any failure."""
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [csdm_cmd, "json", str(Path(demo_path).resolve()), "--output-folder", tmp],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return None
        jf = list(Path(tmp).glob("*.json"))
        if not jf:
            return None
        try:
            data = json.loads(jf[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        players = data.get("players", [])
        return list(players) if isinstance(players, list) else None


def _demo_cvars_and_code(
    players: list[dict] | None,
    steam_id: str,
    screen_height: int,
) -> tuple[list[str], str]:
    """(cvars, raw code) for *steam_id* from one part's player rows."""
    if not players:
        return [], ""
    for pl in players:
        if not isinstance(pl, dict):
            continue
        if str(pl.get("steamId") or "") == str(steam_id):
            code = pl.get("crosshairShareCode")
            if not code:
                return [], ""
            from cs2archive.crosshair_code import decode_crosshair, crosshair_to_convars
            try:
                return (crosshair_to_convars(
                    decode_crosshair(str(code)), screen_height=screen_height), str(code))
            except ValueError as exc:
                # FACEIT demos often carry stale/garbage share codes.
                # Callers treat [] as failure (resolve raises); never
                # invent cvars here.
                print(f"  [WARN] demo share code undecodable "
                      f"({exc}); no demo crosshair")
                return [], ""
    return [], ""


def demo_crosshair_cvars(
    steam_id: str,
    demo_path: Path | str,
    *,
    csdm_cmd: str,
    screen_height: int = 1440,
) -> list[str]:
    """Decode one demo's embedded share code for *steam_id* into cvars."""
    cvars, _code = _demo_cvars_and_code(
        _demo_json_players(demo_path, csdm_cmd), steam_id, screen_height)
    return cvars


def _has_crosshair(cvars: list[str]) -> bool:
    return any(c.startswith("cl_crosshair") for c in (cvars or []))


def resolve_crosshair_cvars(
    nickname: str | None,
    steam_id: str,
    demo_path: Path | str,
    *,
    csdm_cmd: str,
    screen_height: int = 1440,
    demo_paths: list[Path | str] | None = None,
) -> tuple[list[str], dict]:
    """Crosshair cvars for a render. Never returns empty — raises instead.

    Listed players (player_accounts.json by steam_id) must resolve via
    prosettings; unlisted players fall back to the demo share code. Blank
    nicknames resolve canonical-first by steam_id. Any total miss raises
    RuntimeError: renders must fail, never ship the game-default crosshair.
    """
    sid = str(steam_id)
    nick = (nickname or "").strip()
    if not nick or nick.lower() == "unknown":
        nick = canonical_nick(sid, fallback="")
    listed = bool(canonical_nick(sid, fallback=""))

    cache = _load_cache()
    entry = cache.get(sid)
    if isinstance(entry, dict) and _cache_entry_fresh(entry):
        cvars = _cvars_from_entry(entry, screen_height)
        if _has_crosshair(cvars):
            print(f"  Crosshair: cached {entry.get('source')} "
                  f"({entry.get('nickname') or nick or sid})")
            return cvars, {"source": entry.get("source"), "cached": True}
        cache.pop(sid, None)

    parts = list(demo_paths) if demo_paths else [demo_path]

    def fail(reason: str) -> RuntimeError:
        return RuntimeError(
            f"crosshair: unresolvable for '{nick or sid}' ({sid}): {reason}; "
            f"refusing game-default crosshair")

    def save(source: str, payload: dict) -> None:
        cache[sid] = {"nickname": nick or sid, "source": source,
                      "updated_at": datetime.now(timezone.utc).isoformat(),
                      **payload}
        try:
            _save_cache(cache)
        except OSError as exc:
            print(f"  [WARN] crosshair cache save failed: {exc}")

    if nick:
        cvars, info = resolve_crosshair(nick, None, screen_height=screen_height)
        if _has_crosshair(cvars):
            print(f"  Crosshair: prosettings ({nick})")
            save("prosettings", {"settings": info.get("settings")})
            return cvars, {"source": "prosettings", "settings": info.get("settings")}
        if listed:
            raise fail("prosettings miss; demo fallback refused for listed players")
    else:
        print(f"  Crosshair: no nickname for unlisted {sid}; trying demo share code")

    for part in parts:
        cvars, code = _demo_cvars_and_code(
            _demo_json_players(part, csdm_cmd), sid, screen_height)
        if _has_crosshair(cvars):
            print("  Crosshair: demo share code")
            save("demo", {"code": code})
            return cvars, {"source": "demo"}
    raise fail("prosettings miss + no decodable demo share code")
