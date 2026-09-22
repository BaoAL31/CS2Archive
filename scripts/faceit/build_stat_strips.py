"""Build Repeek-style stat strips for a FACEIT lobby: per-player ELO, rank
badge and last-30 aggregates, composited as left/right PNG strips.

Reference: the freeze-time side panes in stacked-pro FACEIT videos
(avatar, nick, level, ELO, #rank, Last-30 row:
 Matches Wins Rating Swing K/D/A K/D K/R ADR).

Data is FACEIT Data API only — player entity (ELO/level/avatar/country),
regional leaderboard position, match history (wins) + per-match stat lines.
"Swing" is our K/D trend (recent-10 vs older-10 of the window) and "Rating"
our Rating-2.0-style approximation from KPR/survival/multikills — same
columns as the reference, our formulas (Repeek's are proprietary).

Usage:
    python scripts/faceit/build_stat_strips.py --match-id 1-75510475-266d-4a7a-a384-72ed968f005d
    python scripts/faceit/build_stat_strips.py --from-json renders/stat-strips/<id>/strips.json
    python scripts/faceit/build_stat_strips.py --match-id <id> --out renders/stat-strips/<stem> --no-cache

Outputs (default renders/stat-strips/<match_id>/):
    left.png / right.png   720x1440 RGBA strips (one 5-card column each)
    strips.json            all numbers behind the render (reuse in pipeline)
    avatars/               cached player headshots

Cost: ~35 API calls per player (~350/lobby), cached 24h under
.data/faceit_last30/<faceit_id>.json. Standalone for now — ffmpeg overlay
wiring into the highlight path comes later.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from scrapers.faceit import FACEITClient  # noqa: E402
from faceit_names import (  # noqa: E402
    canonical_nick,
    known_pro_faceit_ids,
    nick_alias_parts,
)

WINDOW_DEFAULT = 30
CACHE_TTL_S = 24 * 3600
CACHE_DIR = PROJECT_ROOT / ".data" / "faceit_last30"
FLAG_DIR = PROJECT_ROOT / ".data" / "flags"
STATS_CONCURRENCY = 6

STRIP_W, STRIP_H = 720, 1440
CARD_H, CARD_GAP, CARDS_TOP = 248, 16, 28
PAD = 16
AVATAR = 90
PLATE = 102
PLATE_X, PLATE_Y = 2, 14
MATCH_COL_R = 148  # vertical rule after the Matches stack

BG = (16, 16, 16)
BORDER = (36, 36, 36)
WHITE = (245, 245, 245)
ELO_GREY = (176, 176, 176)
GREY = (154, 154, 154)
GREEN = (61, 220, 132)
RED = (235, 87, 87)
PILL_RED = (216, 37, 44)
PILL_GOLD = (232, 183, 19)
RANK_INK = (10, 10, 10)
LEVELS_DIR = PROJECT_ROOT / "assets" / "faceit" / "levels"
_LEVEL_ICON_CACHE: dict = {}
_FLAG_CACHE: dict = {}

MONTSERRAT = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"
SEGOE = Path("C:/Windows/Fonts/segoeui.ttf")
SEGOE_BOLD = Path("C:/Windows/Fonts/segoeuib.ttf")


def _font(size: int, bold: bool = True):
    cands = [SEGOE_BOLD, MONTSERRAT] if bold else [SEGOE]
    for p in cands:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    return ImageFont.load_default()


def _num(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_rounds(score: str) -> int:
    """Total rounds from a '13 / 11' style scoreline."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", score or "")
    if not m:
        return 0
    return int(m.group(1)) + int(m.group(2))


def match_rating(kills: float, deaths: float, rounds: int,
                 double: float, triple: float, quadro: float, penta: float) -> float:
    """Rating-2.0-style approximation (KPR / survival / multikills).

    Exact-N multikill rounds are derived assuming FACEIT counts are
    cumulative (>=N) with clamps, so exact-bucket data degrades gracefully.
    """
    if rounds <= 0:
        return 0.0
    kpr = kills / rounds
    surv = max(rounds - deaths, 0) / rounds
    n5 = max(penta, 0)
    n4 = max(quadro - penta, 0)
    n3 = max(triple - quadro, 0)
    n2 = max(double - triple, 0)
    n1 = max(kills - (2 * n2 + 3 * n3 + 4 * n4 + 5 * n5), 0)
    mkr = (n1 + 4 * n2 + 9 * n3 + 16 * n4 + 25 * n5) / rounds / 1.277
    return (kpr / 0.679 + 0.7 * (surv / 0.317) + mkr) / 2.7


def aggregate_window(games: list[dict]) -> dict:
    """Means over a newest-first window of per-match dicts.

    Each game: {k, d, a, adr, kr, rounds, rating, won}.
    """
    n = len(games)
    if not n:
        return {"n": 0}
    sk = sum(g["k"] for g in games)
    sd = sum(g["d"] for g in games)
    sa = sum(g["a"] for g in games)
    sr = sum(g["rounds"] for g in games)
    wins = sum(1 for g in games if g["won"])
    if n >= 20:
        recent, older = games[:10], games[-10:]
    else:
        recent, older = games[:(n + 1) // 2], games[n // 2:]

    def _kd(rows: list[dict]) -> float:
        dk = sum(g["k"] for g in rows)
        dd = sum(g["d"] for g in rows)
        return dk / dd if dd > 0 else 0.0

    kd_old = _kd(older)
    kd_new = _kd(recent)
    swing = ((kd_new - kd_old) / kd_old * 100.0) if kd_old >= 0.2 else 0.0
    return {
        "n": n,
        "wins_pct": wins / n * 100.0,
        "rating": sum(g["rating"] for g in games) / n,
        "swing": swing,
        "k_avg": sk / n,
        "d_avg": sd / n,
        "a_avg": sa / n,
        "kd": sk / sd if sd > 0 else 0.0,
        "kr": sk / sr if sr > 0 else 0.0,
        "adr": sum(g["adr"] for g in games) / n,
    }


def _truncate(draw: ImageDraw.ImageDraw, text: str,
              font: ImageFont.FreeTypeFont, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "...", font=font) > max_w:
        text = text[:-1]
    return text + "..."


def _pill(draw: ImageDraw.ImageDraw, x0: float, y0: float, x1: float, y1: float,
          fill: tuple, radius: int = 999) -> None:
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def _opaque_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    return im.getbbox()


def _paste_cy(dst: Image.Image, src: Image.Image, x: int, cy: float) -> int:
    """Paste ``src`` so its opaque pixels are vertically centered on ``cy``.

    Returns the visual width (for advancing the header cursor).
    """
    box = _opaque_box(src)
    if not box:
        y = int(round(cy - src.size[1] / 2))
        dst.paste(src, (int(x), y), src)
        return src.size[0]
    l, t, r, b = box
    y = int(round(cy - (b - t) / 2 - t))
    dst.paste(src, (int(x), y), src)
    return r - l


def _text_cy(draw: ImageDraw.ImageDraw, x: float, cy: float, text: str,
             font: ImageFont.FreeTypeFont, fill: tuple) -> None:
    """Draw left-aligned text whose ink is vertically centered on ``cy``."""
    probe = draw.textbbox((x, cy), text, font=font, anchor="lm")
    ink_cy = (probe[1] + probe[3]) / 2
    draw.text((x, cy + (cy - ink_cy)), text, font=font, fill=fill, anchor="lm")


def level_icon_path(level: int) -> Path | None:
    """Real FACEIT skill icon for 1-10 (assets/faceit/levels/), else None."""
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return None
    if not 1 <= lvl <= 10:
        return None
    p = LEVELS_DIR / f"level-{lvl}.png"
    return p if p.exists() else None


def _level_icon(level: int, size: int):
    key = (int(level or 0), size)
    if key not in _LEVEL_ICON_CACHE:
        path = level_icon_path(level)
        icon = None
        if path is not None:
            try:
                raw = Image.open(path).convert("RGBA")
                box = raw.getbbox()
                if box:
                    raw = raw.crop(box)
                raw.thumbnail((size, size), Image.LANCZOS)
                icon = raw
            except Exception:
                icon = None
        _LEVEL_ICON_CACHE[key] = icon
    return _LEVEL_ICON_CACHE[key]


async def _fetch_avatar(url: str, dest: Path) -> Path | None:
    if dest.exists() and dest.stat().st_size > 1500:
        return dest
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            if len(r.content) < 1500:
                return None
            dest.write_bytes(r.content)
            return dest
    except Exception:
        return None


def fmt_matches(n: int) -> str:
    """Full match count with thousands separators (never compact k)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return f"{n:,}"


def fmt_wins(pct: float) -> str:
    return f"%{pct:.0f}"


def fmt_swing(swing: float) -> str:
    """Repeek-style signed percent: +%4.96 / -%0.75."""
    return f"{'+' if swing >= 0 else '-'}%{abs(swing):.2f}"


def fmt_rating(r: float) -> str:
    return f"{r:.2f}"


def rating_is_hot(r: float) -> bool:
    """Green + underline at ~1.30, matching the freeze-time reference."""
    return r >= 1.30


def _circle_avatar(path: Path | None, nick: str, size: int) -> Image.Image:
    tile = Image.new("RGBA", (size, size), (32, 38, 50, 255))
    if path is not None:
        try:
            im = Image.open(path).convert("RGBA")
            im.thumbnail((size * 2, size * 2), Image.LANCZOS)
            side = max(im.size)
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(im, ((side - im.size[0]) // 2, (side - im.size[1]) // 2), im)
            tile = sq.resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    else:
        d = ImageDraw.Draw(tile)
        d.text((size / 2, size / 2), (nick or "?")[:1].upper(),
               font=_font(size // 2), fill=WHITE, anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    return out


def _flag_img(path: Path | None, size: tuple[int, int] = (34, 24)) -> Image.Image | None:
    if path is None or not Path(path).exists():
        return None
    key = (str(path), size)
    if key not in _FLAG_CACHE:
        try:
            im = Image.open(path).convert("RGBA").resize(size, Image.LANCZOS)
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, size[0] - 1, size[1] - 1], radius=4, fill=255)
            out = Image.new("RGBA", size, (0, 0, 0, 0))
            out.paste(im, (0, 0), mask)
            _FLAG_CACHE[key] = out
        except Exception:
            _FLAG_CACHE[key] = None
    return _FLAG_CACHE[key]


async def _fetch_flag(cc: str) -> Path | None:
    cc = (cc or "").lower()
    if len(cc) != 2 or not cc.isalpha():
        return None
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    dest = FLAG_DIR / f"{cc}.png"
    if dest.exists() and dest.stat().st_size > 80:
        return dest
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(f"https://flagcdn.com/w80/{cc}.png")
            r.raise_for_status()
            if len(r.content) < 80:
                return None
            dest.write_bytes(r.content)
            return dest
    except Exception:
        return None


def display_nicks(form: dict) -> tuple[str, str | None]:
    live = str(form.get("nick") or "?")
    fid = form.get("fid")
    canon = known_pro_faceit_ids().get(fid) if fid else None
    if not canon:
        canon = canonical_nick(live)
    return nick_alias_parts(live, canon)


def render_card(form: dict, avatar_path: Path | None, *,
                flag_path: Path | None = None,
                hot_rating: bool = False) -> Image.Image:
    """One player card: hanging circular avatar + Repeek-like last-30 row."""
    w = STRIP_W - PAD
    card = Image.new("RGBA", (w, CARD_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([20, 8, w - 2, CARD_H - 4],
                        radius=16, fill=BG + (255,), outline=BORDER, width=2)

    d.rounded_rectangle(
        [PLATE_X, PLATE_Y, PLATE_X + PLATE, PLATE_Y + PLATE],
        radius=18, fill=BG + (255,), outline=BORDER, width=2)
    av = _circle_avatar(avatar_path, form.get("nick", "?"), AVATAR)
    inset = (PLATE - AVATAR) // 2
    card.paste(av, (PLATE_X + inset, PLATE_Y + inset), av)

    level = int(form.get("level") or 0)
    icon = _level_icon(level, 28)
    flag = _flag_img(flag_path, (36, 24))
    elo = form.get("elo")
    rank = form.get("rank")
    elo_txt = f"{int(elo)}" if elo else "--"
    elo_font = _font(28)
    nick_font = _font(26)

    pill_w = 0
    if rank:
        digits = len(str(int(rank)))
        pill_w = 36 + digits * 14
    elif icon is not None:
        pill_w = 44
    elo_w = d.textlength(elo_txt, font=elo_font)
    right_w = elo_w + (10 + pill_w if pill_w else 0)
    right_x = w - 16 - right_w

    head_x = PLATE_X + PLATE + 8
    hx = head_x
    cy = PLATE_Y + PLATE // 2
    if flag is not None:
        hx += _paste_cy(card, flag, hx, cy) + 6
    if icon is not None:
        hx += _paste_cy(card, icon, hx, cy) + 8

    nick, real = display_nicks(form)
    nick_size = 26
    while nick_size > 16:
        nick_font = _font(nick_size)
        if d.textlength(nick, font=nick_font) <= right_x - hx - 8:
            break
        nick_size -= 1
    nick = _truncate(d, nick, nick_font, right_x - hx - 8)
    _text_cy(d, hx, cy, nick, nick_font, WHITE)
    if real:
        real_font = _font(15, bold=False)
        real = _truncate(d, real, real_font, right_x - hx - 8)
        _text_cy(d, hx, cy + 20, real, real_font, GREY)

    ax = right_x
    _text_cy(d, ax, cy, elo_txt, elo_font, ELO_GREY)
    ax += elo_w + 8
    if rank:
        gold = int(rank) == 1
        elo_ink = d.textbbox((0, cy), elo_txt, font=elo_font, anchor="lm")
        half = max(14, (elo_ink[3] - elo_ink[1]) / 2 + 3)
        _pill(d, ax, cy - half, ax + pill_w, cy + half,
              PILL_GOLD if gold else PILL_RED, radius=14)
        d.text((ax + pill_w / 2, cy), f"#{rank}", font=_font(20),
               fill=RANK_INK, anchor="mm")
    elif icon is not None:
        _paste_cy(card, icon, int(ax), cy)

    agg = form.get("agg", {}) if isinstance(form.get("agg"), dict) else {}
    n = agg.get("n", 0)
    last_lbl = "Last 30 matches" if n == 30 else f"Last {n} matches"
    stats_y = 168
    d.text((MATCH_COL_R + 12, stats_y - 36), last_lbl,
           font=_font(15, bold=False), fill=GREY, anchor="lt")

    swing = agg.get("swing", 0.0)
    rating = agg.get("rating", 0.0)
    lifetime = form.get("lifetime_matches") or n
    matches_txt = fmt_matches(lifetime)
    mx = 86
    d.text((mx, 168), matches_txt, font=_font(22), fill=ELO_GREY, anchor="mm")
    d.text((mx, 196), "Matches", font=_font(14, bold=False),
           fill=GREY, anchor="mm")
    d.line([(MATCH_COL_R, 148), (MATCH_COL_R, 214)], fill=BORDER, width=1)

    cols = [
        (fmt_wins(agg.get("wins_pct", 0.0)), "Wins", ELO_GREY, False),
        (fmt_rating(rating), "Rating",
         GREEN if hot_rating or rating_is_hot(rating) else ELO_GREY,
         hot_rating or rating_is_hot(rating)),
        (fmt_swing(swing), "Swing",
         GREEN if swing >= 0 and n else (RED if n else ELO_GREY), False),
        (f"{agg.get('k_avg', 0.0):.0f}/{agg.get('d_avg', 0.0):.0f}/{agg.get('a_avg', 0.0):.0f}",
         "K/D/A", ELO_GREY, False),
        (f"{agg.get('kd', 0.0):.2f}", "K/D", ELO_GREY, False),
        (f"{agg.get('kr', 0.0):.2f}", "K/R", ELO_GREY, False),
        (f"{agg.get('adr', 0.0):.1f}", "ADR", ELO_GREY, False),
    ]
    weights = [0.85, 0.9, 1.25, 1.55, 0.85, 0.85, 1.05]
    unit = (w - MATCH_COL_R - 24) / sum(weights)
    x = MATCH_COL_R + 8
    lab_font = _font(13, bold=False)
    for (val, lab, fill, underline), wt in zip(cols, weights):
        cw = unit * wt
        vf = _font(18)
        d.text((x + cw / 2, 168), val, font=vf, fill=fill, anchor="mm")
        if underline:
            tw = d.textlength(val, font=vf)
            d.line([(x + cw / 2 - tw / 2, 182), (x + cw / 2 + tw / 2, 182)],
                   fill=fill, width=3)
        d.text((x + cw / 2, 198), lab, font=lab_font, fill=GREY, anchor="mm")
        x += cw
    return card


def render_strip(forms: list[dict], avatar_paths: dict[str, Path | None],
                 flag_paths: dict[str, Path | None] | None = None) -> Image.Image:
    strip = Image.new("RGBA", (STRIP_W, STRIP_H), (0, 0, 0, 0))
    ratings = [f.get("agg", {}).get("rating", 0.0)
               for f in forms if isinstance(f.get("agg"), dict)]
    best = max(ratings) if ratings else 0.0
    y = CARDS_TOP
    flags = flag_paths or {}
    for form in forms:
        r = (form.get("agg") or {}).get("rating", 0.0)
        card = render_card(
            form, avatar_paths.get(form["fid"]),
            flag_path=flags.get(form["fid"]),
            hot_rating=best > 0 and abs(r - best) < 1e-6)
        strip.paste(card, (0, y), card)
        y += CARD_H + CARD_GAP
    return strip


async def player_form(client: FACEITClient, fid: str, sem: asyncio.Semaphore,
                      window: int = WINDOW_DEFAULT,
                      use_cache: bool = True) -> dict:
    """ELO + rank + last-`window` aggregates for one FACEIT id (cached 24h)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{fid}.json"
    if use_cache and cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - payload.get("fetched_at", 0) < CACHE_TTL_S:
                return payload["form"]
        except (OSError, ValueError, KeyError):
            pass

    entity = await client.get_player(fid)
    if not entity:
        return {"fid": fid, "nick": "?", "error": "player lookup failed"}
    cs2 = entity.get("games", {}).get("cs2", {}) or {}
    elo = cs2.get("faceit_elo")
    level = cs2.get("skill_level", 0)
    nick = entity.get("nickname", "?")
    form: dict = {
        "fid": fid,
        "nick": nick,
        "elo": int(elo) if elo is not None else None,
        "level": int(level or 0),
        "country": (entity.get("country") or "").lower(),
        "avatar_url": entity.get("avatar") or "",
        "rank": None,
    }
    history = await client.get_history_items(fid, limit=window + 5)
    region = (history[0].get("region") if history else None) or cs2.get("region") or "EU"
    rank_task = client.get_player_ranking(fid, region)
    lifetime_task = client.get_lifetime_stats(fid)

    finished = [it for it in history if it.get("status") == "finished"][:window]

    async def _one(item: dict) -> dict | None:
        mid = item.get("match_id", "")
        async with sem:
            stats = await client.get_match_stats(mid)
        if not stats:
            return None
        line = next(
            (ln for ln in stats["players"].values()
             if ln.get("player_id") == fid), None)
        if not line:
            return None
        rounds = parse_rounds(stats.get("score", ""))
        k, d = _num(line.get("kills")), _num(line.get("deaths"))
        return {
            "k": k, "d": d, "a": _num(line.get("assists")),
            "adr": _num(line.get("adr")), "kr": _num(line.get("kr")),
            "rounds": rounds,
            "rating": match_rating(k, d, rounds, _num(line.get("double")),
                                   _num(line.get("triple")), _num(line.get("quadro")),
                                   _num(line.get("penta"))),
            "won": str(line.get("result", "")).strip().lower() in ("1", "1.0", "true"),
        }

    games = [g for g in await asyncio.gather(*(_one(it) for it in finished)) if g]
    form["agg"] = aggregate_window(games)
    form["rank"], lifetime = await asyncio.gather(rank_task, lifetime_task)
    try:
        form["lifetime_matches"] = int(float(str((lifetime or {}).get("Matches", 0))))
    except (TypeError, ValueError):
        form["lifetime_matches"] = 0
    if not form["lifetime_matches"]:
        form["lifetime_matches"] = form["agg"].get("n", 0)

    cache_path.write_text(json.dumps(
        {"fetched_at": time.time(), "form": form}, indent=2), encoding="utf-8")
    return form


async def build_strips(match_id: str, out_dir: Path,
                       window: int = WINDOW_DEFAULT,
                       use_cache: bool = True) -> dict:
    client = FACEITClient()
    try:
        match = await client.get_match(match_id)
        if not match:
            raise RuntimeError(f"match {match_id} not found")
        stats = await client.get_match_stats(match_id) or {}
        nick_to_fid = {k: v.get("player_id")
                       for k, v in stats.get("players", {}).items()
                       if v.get("player_id")}
        factions = []
        for key in ("faction1", "faction2"):
            fac = (match.get("teams", {}) or {}).get(key, {})
            roster = fac.get("roster") or fac.get("players") or []
            fids: list[str] = []
            for entry in roster:
                fid = entry.get("player_id") or nick_to_fid.get(
                    entry.get("nickname") or entry.get("game_player_name") or "")
                if fid and fid not in fids:
                    fids.append(fid)
            factions.append({"name": fac.get("nickname") or fac.get("name") or key,
                             "fids": fids})
        sem = asyncio.Semaphore(STATS_CONCURRENCY)
        forms = await asyncio.gather(*(
            player_form(client, fid, sem, window, use_cache)
            for fac in factions for fid in fac["fids"]))
        by_fid = {f["fid"]: f for f in forms}

        out_dir.mkdir(parents=True, exist_ok=True)
        av_dir = out_dir / "avatars"
        av_dir.mkdir(exist_ok=True)
        avatar_paths: dict[str, Path | None] = {}
        for f in forms:
            fid = f["fid"]
            url = f.get("avatar_url", "")
            suffix = (urlsplit(url).path.rsplit(".", 1)[-1] or "jpg").lower()[:4]
            if suffix not in ("jpg", "jpeg", "png", "webp"):
                suffix = "jpg"
            dest = av_dir / f"{fid}.{suffix}"
            avatar_paths[fid] = await _fetch_avatar(url, dest)
        flag_paths = {f["fid"]: await _fetch_flag(f.get("country", ""))
                      for f in forms}

        sides = []
        for i, fac in enumerate(factions):
            fac_forms = [by_fid[fid] for fid in fac["fids"] if fid in by_fid]
            strip = render_strip(fac_forms, avatar_paths, flag_paths)
            name = "left.png" if i == 0 else "right.png"
            strip.save(out_dir / name, "PNG")
            elos = [f["elo"] for f in fac_forms if f.get("elo")]
            sides.append({"name": fac["name"], "avg_elo": round(sum(elos) / len(elos)) if elos else None,
                          "players": [f["nick"] for f in fac_forms]})
        payload = {
            "match_id": match_id,
            "map": stats.get("map", "?"),
            "score": stats.get("score", ""),
            "generated_at": datetime.now().isoformat(),
            "window": window,
            "left": sides[0] if len(sides) > 0 else {},
            "right": sides[1] if len(sides) > 1 else {},
            "players": by_fid,
        }
        (out_dir / "strips.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload
    finally:
        await client.close()


def _avatar_paths_from_dir(forms: list[dict], av_dir: Path) -> dict[str, Path | None]:
    paths: dict[str, Path | None] = {}
    for f in forms:
        fid = f["fid"]
        hit = next(iter(av_dir.glob(f"{fid}.*")), None) if av_dir.exists() else None
        paths[fid] = hit
    return paths


async def render_from_json(json_path: Path, out_dir: Path) -> dict:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    by_fid = payload.get("players") or {}
    av_dir = out_dir / "avatars"
    forms = list(by_fid.values())
    avatar_paths = _avatar_paths_from_dir(forms, av_dir)
    flag_paths = {}
    for f in forms:
        flag_paths[f["fid"]] = await _fetch_flag(f.get("country", ""))
    nick_to_fid = {f["nick"]: f["fid"] for f in forms}
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, side in enumerate(("left", "right")):
        nicks = (payload.get(side) or {}).get("players") or []
        fac_forms = [by_fid[nick_to_fid[n]] for n in nicks if n in nick_to_fid]
        if not fac_forms:
            continue
        strip = render_strip(fac_forms, avatar_paths, flag_paths)
        strip.save(out_dir / ("left.png" if i == 0 else "right.png"), "PNG")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--match-id", default=None)
    ap.add_argument("--from-json", default=None,
                    help="re-render PNGs from an existing strips.json (no API)")
    ap.add_argument("--out", default=None,
                    help="output dir (default renders/stat-strips/<match_id>)")
    ap.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                    help="last-N matches per player (default 30)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore .data/faceit_last30 cache")
    args = ap.parse_args()

    if args.from_json:
        src = Path(args.from_json)
        out = Path(args.out) if args.out else src.parent
        payload = asyncio.run(render_from_json(src, out))
    else:
        if not args.match_id:
            ap.error("--match-id is required unless --from-json is set")
        out = Path(args.out) if args.out else (
            PROJECT_ROOT / "renders" / "stat-strips" / args.match_id)
        payload = asyncio.run(build_strips(
            args.match_id, out, window=args.window, use_cache=not args.no_cache))
    for side in ("left", "right"):
        info = payload.get(side, {})
        print(f"[{side}] {info.get('name')} avg_elo={info.get('avg_elo')} "
              f"players={','.join(info.get('players', []))}")
    print(out)


if __name__ == "__main__":
    main()
