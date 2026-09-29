"""Shared helpers for backlog creators (HLTV + FACEIT).

Single source of truth for:
  - rating -> priority bucket thresholds
  - avatar cache lookup
  - Recognised-Pro account lookup by steam64
  - backlog card writing

Import via `from cs2archive._backlog_common import ...`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ACCOUNTS_PATH = PROJECT_ROOT / ".data" / "player_accounts.json"
BACKLOG_DIR = PROJECT_ROOT / "backlog"

# Rating >= HIGH_RATING -> high, >= MID_RATING -> mid, else low.
HIGH_RATING = 1.5
MID_RATING = 1.0


def rating_bucket(rating: float | None, *, mid_name: str = "mid",
                  unknown: str | None = "mid") -> str:
    """Priority bucket from an in-match rating.

    mid_name: HLTV layout uses "medium", FACEIT uses "mid" (keep each
    source's historical folder names). unknown=None means a missing rating
    is an error condition for the caller; otherwise it maps to `unknown`.
    """
    if rating is None or rating != rating:  # None or NaN
        if unknown is None:
            raise ValueError("rating required")
        return unknown
    if rating >= HIGH_RATING:
        return "high"
    if rating >= MID_RATING:
        return mid_name
    return "low"


def load_accounts_by_steam() -> dict[str, dict]:
    """steam_id_64 -> account record for every Recognised Pro."""
    if not ACCOUNTS_PATH.exists():
        return {}
    data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    players = data if isinstance(data, list) else data.get("players", [])
    out: dict[str, dict] = {}
    for p in players:
        sid = str(p.get("steam_id") or "").strip()
        if sid:
            out[sid] = p
    return out


def find_account(*, player: str = "", steam_id: str = "") -> dict:
    """Recognised-Pro account by stable steam_id first, then nick.

    Nick matches ``nickname`` or ``faceit_nickname`` (case-insensitive) —
    FACEIT cards carry the FACEIT nick (donk666) while the account +
    prosettings live under the HLTV nick (donk). Returns {} on miss.
    """
    by_steam = load_accounts_by_steam()
    sid = str(steam_id or "").strip()
    if sid and sid in by_steam:
        return by_steam[sid]
    want = str(player or "").strip().lower()
    if want:
        for acct in by_steam.values():
            for key in ("nickname", "faceit_nickname"):
                if str(acct.get(key) or "").strip().lower() == want:
                    return acct
    return {}


def find_avatar(nickname: str, steam_id: str = "") -> str:
    """Project-relative path to the player's cached avatar, or "".

    Layout: demos/avatars/{nick}/{source}/{nick}.{png,jpg,jpeg} where source
    is hltv|faceit. Matches stable steam_id first; nick matching (raw +
    canonical) is fallback only.
    """
    base = PROJECT_ROOT / "demos" / "avatars"
    candidates = [nickname.strip().lower()]
    sid = str(steam_id or "").strip()
    if sid:
        try:
            acct = load_accounts_by_steam().get(sid) or {}
            canon = str(acct.get("nickname") or "").strip().lower()
            if canon and canon not in candidates:
                candidates.insert(0, canon)
        except Exception:
            pass
    try:
        from cs2archive.faceit.faceit_names import canonical_nick
        canon = canonical_nick(nickname)
        if canon and canon.lower() not in candidates:
            candidates.append(canon.lower())
    except Exception:
        pass
    for nick in candidates:
        folder = base / nick
        if not folder.is_dir():
            continue
        for source in ("hltv", "faceit"):
            for ext in (".png", ".jpg", ".jpeg"):
                p = folder / source / f"{nick}{ext}"
                if p.exists():
                    return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return ""


def write_card(meta: dict, backlog_file: Path) -> Path:
    """Persist one backlog card as pretty JSON."""
    meta = dict(meta)
    meta.setdefault("pipeline_cmd", pipeline_cmd(backlog_file))
    backlog_file.parent.mkdir(parents=True, exist_ok=True)
    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return backlog_file


def pipeline_cmd(backlog_file: Path) -> str:
    """Printable pipeline invocation using this interpreter, not a baked conda path.

    Module form: cs2archive is an installed package, so no PYTHONPATH prefix and no path to
    the script are needed (CR-01). This value is documentation for a human copying it out of
    a card -- nothing executes it.
    """
    rel = rel_to_project(Path(backlog_file))
    return f"& {sys.executable} -m cs2archive.pov.pipeline --backlog {rel}"


def rel_to_project(path: Path) -> str:
    """Project-relative POSIX path string (absolute fallback outside root)."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def detect_demo_source(arg: str) -> str:
    """Classify a CLI argument: 'faceit' | 'hltv' | 'url'.

    A path to an existing .dem under demos/faceit (or any .dem when the
    caller opts in) routes to the FACEIT flow; anything URL-shaped routes
    to the HLTV match flow.
    """
    a = arg.strip()
    if a.lower().startswith(("http://", "https://")):
        return "url"
    p = Path(a)
    if p.suffix.lower() == ".dem" and p.exists():
        norm = str(p.resolve()).replace("\\", "/").lower()
        return "faceit" if "/demos/faceit/" in norm else "hltv"
    return "unknown"


def card_nicks_for_demo(demo: Path) -> dict[str, str]:
    """steam_id -> backlog card ``player`` nick for one demo.

    The card nick is the EXACT string the pipeline builds its render dir
    from, so shorts grouped by it land in the POV folder the render will
    use — never the demo's raw name. Cards are written minutes earlier by
    the same backlog run.
    """
    stem = Path(demo).stem
    out: dict[str, str] = {}
    if not BACKLOG_DIR.is_dir():
        return out
    for card in BACKLOG_DIR.rglob("*.json"):
        try:
            meta = json.loads(card.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        if Path(str(meta.get("demo_path") or "")).stem != stem:
            continue
        sid = str(meta.get("steam_id") or "").strip()
        nick = str(meta.get("player") or "").strip()
        if sid and nick:
            out.setdefault(sid, nick)
    return out


def persist_shorts_grouped(demo: Path, timeline: dict,
                           shorts_list: list[dict],
                           nick_by_sid: dict[str, str] | None = None) -> int:
    """Persist shorts under each short's own POV folder.

    ``nick_by_sid`` maps steam_id -> backlog card nick (see
    :func:`card_nicks_for_demo`); unmapped POVs fall back to the accounts
    canonical nick, then to the legacy ``renders/shorts/`` tree with a loud
    warning — a short is never dropped for want of a folder. The shared
    kill-list cache is persisted once per involved POV folder. Returns the
    number of short_timeline.json files written.
    """
    from cs2archive.paths import pov_dir, shorts_base
    from cs2archive.shorts import discard_empty_shorts_dir
    from cs2archive.shorts.build_short_timeline import (
        _build_short_slug, persist_action_timeline, short_json_payload,
    )

    demo = Path(demo)
    nick_by_sid = dict(nick_by_sid or {})
    if any(s.get("pov_steam_id") not in nick_by_sid for s in shorts_list):
        try:
            accounts = load_accounts_by_steam()
        except Exception:
            accounts = {}
        for s in shorts_list:
            sid = str(s.get("pov_steam_id") or "")
            if sid and sid not in nick_by_sid:
                nick = str((accounts.get(sid) or {}).get("nickname") or "").strip()
                if nick:
                    nick_by_sid[sid] = nick

    by_base: dict[Path, list[dict]] = {}
    for short in shorts_list:
        sid = str(short.get("pov_steam_id") or "")
        nick = nick_by_sid.get(sid, "")
        if nick:
            base = shorts_base(pov_dir(demo.stem, nick))
        else:
            from cs2archive.shorts import resolve_output_dir as _legacy_base
            base = _legacy_base(demo)
            try:
                shown = base.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                shown = str(base)
            print(f"  [WARN] no backlog card for short POV {sid or '?'} — "
                  f"using legacy {shown}")
        by_base.setdefault(base, []).append(short)

    written = 0
    for base, shorts in sorted(by_base.items(), key=lambda kv: str(kv[0])):
        try:
            persist_action_timeline(demo, timeline, output_dir=base)
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] shorts cache failed for {base}: {e}")
            continue
        for short in shorts:
            slug = _build_short_slug(short)
            short_dir = base / f"shorts-{slug}"
            short_dir.mkdir(parents=True, exist_ok=True)
            (short_dir / "short_timeline.json").write_text(
                json.dumps(short_json_payload(timeline, short), indent=2),
                encoding="utf-8")
            written += 1
        discard_empty_shorts_dir(base)
    return written
