"""
Prosettings.net CS2 video settings (resolution / aspect / scaling).

No public API — scrapes https://prosettings.net/lists/cs2/ into a local cache.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / ".data" / "pro_video_settings.json"
LIST_URL = "https://prosettings.net/lists/cs2/"

DEFAULT_SETTINGS = {
    "resolution": "1920x1080",
    "width": 1920,
    "height": 1080,
    "aspect_ratio": "16:9",
    "scaling_mode": "Native",
    "viewmodel_fov": None,
    "viewmodel_offset_x": None,
    "viewmodel_offset_y": None,
    "viewmodel_offset_z": None,
    "viewmodel_presetpos": None,
    "hud_scaling": None,
}

_RES_RE = re.compile(r"^(\d+)\s*[x×]\s*(\d+)$", re.I)
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_PLAYER_URL = "https://prosettings.net/players/{slug}/"


def _parse_resolution(text: str) -> tuple[int, int] | None:
    m = _RES_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _normalize_scaling(text: str) -> str:
    t = (text or "").strip().lower()
    if "black" in t:
        return "Black Bars"
    if "stretch" in t:
        return "Stretched"
    if "native" in t:
        return "Native"
    return text.strip() or "Native"


def _parse_num(text: str) -> float | int | None:
    t = (text or "").strip().replace(",", "")
    if not _NUM_RE.match(t):
        return None
    return float(t) if "." in t else int(t)


def _label_value_map(soup: BeautifulSoup) -> dict[str, str]:
    """Flatten common prosettings label/value pairs (table rows or dl)."""
    out: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0] and cells[1]:
            out[cells[0].lower()] = cells[1]
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            k, v = dt.get_text(strip=True), dd.get_text(strip=True)
            if k and v:
                out[k.lower()] = v
    return out


def scrape_player_viewmodel(
    nickname: str,
    session: requests.Session | None = None,
) -> dict:
    """Fetch viewmodel fields from a player's prosettings page.

    Returns keys: viewmodel_fov, viewmodel_offset_x/y/z, viewmodel_presetpos
    (missing keys omitted). Empty dict on failure.
    """
    nick = (nickname or "").strip()
    if not nick:
        return {}
    slug = nick.lower().replace(" ", "-")
    sess = session or requests.Session()
    try:
        resp = sess.get(_PLAYER_URL.format(slug=slug), headers=_UA, timeout=60)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return {}

    # Prefer the Viewmodel section's following table; fall back to page-wide map.
    section_map: dict[str, str] = {}
    for h in soup.find_all(["h2", "h3", "h4"]):
        if h.get_text(strip=True).lower() == "viewmodel":
            sib = h.find_next(["table", "dl"])
            if sib is not None:
                section_map = _label_value_map(BeautifulSoup(str(sib), "lxml"))
            break
    labels = section_map or _label_value_map(soup)

    key_map = {
        "fov": "viewmodel_fov",
        "offset x": "viewmodel_offset_x",
        "offset y": "viewmodel_offset_y",
        "offset z": "viewmodel_offset_z",
        "presetpos": "viewmodel_presetpos",
        "preset pos": "viewmodel_presetpos",
    }
    out: dict = {}
    for lab, field in key_map.items():
        raw = labels.get(lab)
        if raw is None:
            continue
        num = _parse_num(raw)
        if num is None:
            continue
        out[field] = num
    return out


def viewmodel_convars(settings: dict) -> list[str]:
    """CS2 convars for viewmodel and HUD fields present in *settings*."""
    lines: list[str] = []
    mapping = (
        ("viewmodel_fov", "viewmodel_fov"),
        ("viewmodel_offset_x", "viewmodel_offset_x"),
        ("viewmodel_offset_y", "viewmodel_offset_y"),
        ("viewmodel_offset_z", "viewmodel_offset_z"),
        ("viewmodel_presetpos", "viewmodel_presetpos"),
        ("hud_scaling", "hud_scaling"),
    )
    for key, cvar in mapping:
        val = settings.get(key)
        if val is None or val == "":
            continue
        lines.append(f"{cvar} {val}")
    return lines


def scrape_list(session: requests.Session | None = None) -> dict[str, list[dict]]:
    """Fetch the CS2 list page and return nickname_lower -> list of settings."""
    sess = session or requests.Session()
    resp = sess.get(LIST_URL, headers=_UA, timeout=90)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("prosettings CS2 list: no <table> found")

    headers = [th.get_text(strip=True) for th in table.find_all("th")]

    def col(*names: str) -> int:
        for n in names:
            if n in headers:
                return headers.index(n)
        raise KeyError(f"column not found among {headers}: {names}")

    i_team = col("Team")
    i_player = col("Player")
    i_res = col("Resolution")
    i_aspect = col("Aspect Ratio")
    i_scale = col("Scaling Mode")

    out: dict[str, list[dict]] = {}
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) <= max(i_player, i_res, i_aspect, i_scale, i_team):
            continue
        nick = cells[i_player].strip()
        if not nick:
            continue
        parsed = _parse_resolution(cells[i_res])
        if not parsed:
            continue
        w, h = parsed
        key = nick.lower()
        entry = {
            "nickname": nick,
            "team": cells[i_team].strip(),
            "resolution": f"{w}x{h}",
            "width": w,
            "height": h,
            "aspect_ratio": cells[i_aspect].strip() or "",
            "scaling_mode": _normalize_scaling(cells[i_scale]),
        }
        out.setdefault(key, []).append(entry)
    if not out:
        raise RuntimeError("prosettings CS2 list: parsed 0 players")
    return out


def save_cache(players: dict[str, list[dict]], path: Path = CACHE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = sum(len(v) for v in players.values())
    payload = {
        "source": LIST_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "count": count,
        "players": players,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_cache(path: Path = CACHE_PATH) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("players") or {}
    # Migrate legacy flat dict values -> list
    out: dict[str, list[dict]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = [v]
    return out


def scrape_and_cache(*, force: bool = False, path: Path = CACHE_PATH) -> dict[str, list[dict]]:
    if path.exists() and not force:
        players = load_cache(path)
        if players:
            return players
    players = scrape_list()
    save_cache(players, path)
    return players


def _pick_entry(nickname: str, entries: list[dict]) -> dict:
    if len(entries) == 1:
        return entries[0]
    # Prefer exact case match (NiKo vs niko).
    exact = [e for e in entries if e.get("nickname") == nickname]
    if len(exact) == 1:
        # If the exact-case entry is legacy 4:3 stretched but a same-nick
        # 16:9 Native entry exists, prefer the modern native settings.
        if exact[0].get("aspect_ratio") != "16:9":
            native = [e for e in entries
                      if e.get("aspect_ratio") == "16:9"
                      and e.get("scaling_mode", "").lower() == "native"]
            if native:
                return native[0]
        return exact[0]
    if exact:
        entries = exact
    # Prefer 16:9 Native (modern default) over legacy 4:3 stretched when ambiguous.
    native = [e for e in entries if e.get("aspect_ratio") == "16:9"
              and e.get("scaling_mode", "").lower() == "native"]
    if len(native) == 1:
        return native[0]
    if not entries:
        return exact[0] if exact else entries[0]
    print(f"  [WARN] ambiguous prosettings nick {nickname!r}: "
          f"{[(e.get('nickname'), e.get('team')) for e in entries]} — using first")
    return entries[0]


def _ensure_viewmodel(entry: dict, nickname: str, *, force: bool = False) -> dict:
    """Attach viewmodel fields to a cache entry (scrapes player page if needed)."""
    vm_keys = (
        "viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
        "viewmodel_offset_z", "viewmodel_presetpos",
    )
    if not force and any(entry.get(k) is not None for k in vm_keys):
        return entry
    vm = scrape_player_viewmodel(entry.get("nickname") or nickname)
    if vm:
        entry.update(vm)
    return entry


def resolve_video_settings(
    nickname: str,
    *,
    refresh_if_missing: bool = True,
    fetch_viewmodel: bool = True,
) -> dict:
    """Return video settings for a player. Falls back to 1920x1080 Native."""
    nick = (nickname or "").strip()
    key = nick.lower()
    players = load_cache()
    if key not in players and refresh_if_missing:
        try:
            players = scrape_and_cache(force=True)
        except Exception as e:
            print(f"  [WARN] prosettings scrape failed: {e}")
            players = load_cache()

    entries = players.get(key) or []
    if not entries:
        out = dict(DEFAULT_SETTINGS)
        out["nickname"] = nick
        out["team"] = ""
        out["source"] = "default"
        if fetch_viewmodel:
            out.update(scrape_player_viewmodel(nick))
        return out

    hit = dict(_pick_entry(nick, entries))
    if fetch_viewmodel:
        before = {k: hit.get(k) for k in (
            "viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
            "viewmodel_offset_z", "viewmodel_presetpos",
        )}
        hit = _ensure_viewmodel(hit, nick)
        if any(hit.get(k) != before[k] for k in before):
            # Replace matching cache row and persist.
            for i, e in enumerate(entries):
                if (
                    e.get("nickname") == hit.get("nickname")
                    and e.get("team") == hit.get("team")
                    and e.get("resolution") == hit.get("resolution")
                ):
                    entries[i] = hit
                    players[key] = entries
                    try:
                        save_cache(players)
                    except Exception:
                        pass
                    break

    return {
        "nickname": hit.get("nickname", nick),
        "team": hit.get("team", ""),
        "resolution": hit["resolution"],
        "width": int(hit["width"]),
        "height": int(hit["height"]),
        "aspect_ratio": hit.get("aspect_ratio", ""),
        "scaling_mode": hit.get("scaling_mode", "Native"),
        "viewmodel_fov": hit.get("viewmodel_fov"),
        "viewmodel_offset_x": hit.get("viewmodel_offset_x"),
        "viewmodel_offset_y": hit.get("viewmodel_offset_y"),
        "viewmodel_offset_z": hit.get("viewmodel_offset_z"),
        "viewmodel_presetpos": hit.get("viewmodel_presetpos"),
        "hud_scaling": hit.get("hud_scaling"),
        "source": "prosettings",
    }


def backlog_video_fields(nickname: str) -> dict:
    """Fields to merge into a backlog entry."""
    s = resolve_video_settings(nickname)
    fields = {
        "resolution": s["resolution"],
        "aspect_ratio": s["aspect_ratio"],
        "scaling_mode": s["scaling_mode"],
        "capture_width": s["width"],
        "capture_height": s["height"],
        "video_settings_source": s.get("source", "prosettings"),
    }
    for k in (
        "viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
        "viewmodel_offset_z", "viewmodel_presetpos", "hud_scaling",
    ):
        if s.get(k) is not None:
            fields[k] = s[k]
    return fields


def sync_player_accounts(*, force_scrape: bool = True) -> dict:
    """Scrape prosettings (optional) and write video fields onto every player_accounts row.

    Returns a summary dict with matched / defaulted / accounts lists.
    """
    from player_accounts import _load_accounts, _save_accounts

    if force_scrape:
        scrape_and_cache(force=True)
    else:
        scrape_and_cache(force=False)

    records = _load_accounts()
    matched: list[dict] = []
    defaulted: list[dict] = []

    for rec in records:
        nick = (rec.get("nickname") or "").strip()
        if not nick:
            continue
        s = resolve_video_settings(nick, refresh_if_missing=False)
        rec["resolution"] = s["resolution"]
        rec["aspect_ratio"] = s["aspect_ratio"]
        rec["scaling_mode"] = s["scaling_mode"]
        rec["capture_width"] = int(s["width"])
        rec["capture_height"] = int(s["height"])
        rec["video_settings_source"] = s.get("source", "default")
        for k in (
            "viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
            "viewmodel_offset_z", "viewmodel_presetpos", "hud_scaling",
        ):
            if s.get(k) is not None:
                rec[k] = s[k]
        row = {
            "nickname": nick,
            "steam_id": rec.get("steam_id") or "",
            "resolution": rec["resolution"],
            "aspect_ratio": rec["aspect_ratio"],
            "scaling_mode": rec["scaling_mode"],
            "source": rec["video_settings_source"],
            "team": s.get("team", ""),
            "viewmodel_fov": s.get("viewmodel_fov"),
        }
        if s.get("source") == "prosettings":
            matched.append(row)
        else:
            defaulted.append(row)

    _save_accounts(records)
    return {
        "total": len(records),
        "matched": matched,
        "defaulted": defaulted,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Scrape prosettings.net CS2 video settings")
    parser.add_argument("--force", action="store_true", help="Re-scrape even if cache exists")
    parser.add_argument("--lookup", metavar="NICK", help="Print settings for one player")
    parser.add_argument(
        "--sync-accounts",
        action="store_true",
        help="Write video settings onto every .data/player_accounts.json entry",
    )
    args = parser.parse_args()

    if args.lookup:
        s = resolve_video_settings(args.lookup, refresh_if_missing=True)
        print(json.dumps(s, indent=2))
        return

    if args.sync_accounts:
        summary = sync_player_accounts(force_scrape=True)
        print(f"Synced {summary['total']} accounts "
              f"({len(summary['matched'])} prosettings, "
              f"{len(summary['defaulted'])} default 1920x1080)")
        print("\nMatched:")
        for r in sorted(summary["matched"], key=lambda x: x["nickname"].lower()):
            print(f"  {r['nickname']:20} {r['resolution']:12} {r['aspect_ratio']:6} "
                  f"{r['scaling_mode']:12} steam={r['steam_id'] or '-'}  {r.get('team','')}")
        if summary["defaulted"]:
            print("\nDefaulted (not on prosettings list):")
            for r in sorted(summary["defaulted"], key=lambda x: x["nickname"].lower()):
                print(f"  {r['nickname']:20} steam={r['steam_id'] or '-'}")
        return

    players = scrape_and_cache(force=args.force)
    n = sum(len(v) for v in players.values())
    print(f"Cached {n} player entries ({len(players)} unique nicks) -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
