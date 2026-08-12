"""Create a FACEIT match intro card: match + team details on one image.

Fetches the match from the FACEIT Data API (teams, roster, score, map, date,
region) plus per-player match stats (K-D, ADR, HS%) and current ELO, then
composites a 2560x1440 intro card ready to pop at the start of a POV video.

Standalone for now — wiring it into the pipeline (ffmpeg insert at video
start) comes later.

Usage:
    python scripts/faceit/create_match_intro.py --match-id <id> [--output <dir>]
    python scripts/faceit/create_match_intro.py --backlog backlog/faceit/high/<slug>.json
    python scripts/faceit/create_match_intro.py --match-id <id> --skip-elo

Outputs:
    <output>/intro.png            the 2560x1440 card
    <output>/intro_details.json   raw fetched data (reuse in pipeline later)
    <output>/avatars/             cached player avatar downloads
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()
sys.path.insert(0, str(PROJECT_ROOT / "thumbnail"))

from config import settings  # noqa: E402
from scrapers.faceit import FACEITClient  # noqa: E402

ACCOUNTS_FILE = PROJECT_ROOT / ".data" / "player_accounts.json"

FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"

W, H = 2560, 1440

# Layout constants
TOP_PILL_CX, TOP_PILL_Y = 1280, 76
SCORE_Y = 260
NAME_Y = 390
ELO_Y = 455
ROWS_Y0 = 630
ROW_PITCH = 145
AVATAR_D = 120
C_LEFT, C_RIGHT = 640, 1920

# Palette
ORANGE = (255, 85, 0)
ORANGE_SOFT = (255, 122, 51)
WHITE = (245, 245, 245)
GREY = (150, 158, 172)
MUTED = (178, 186, 199)


def _font(size: int) -> ImageFont.FreeTypeFont:
    if FONT_PATH.exists():
        return ImageFont.truetype(str(FONT_PATH), size)
    return ImageFont.load_default()


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    anchor: str = "mm",
    shadow: bool = True,
) -> None:
    if shadow:
        draw.text(
            (xy[0] + 3, xy[1] + 3), text, font=font, fill=(0, 0, 0, 210),
            anchor=anchor, stroke_width=2, stroke_fill=(0, 0, 0, 210),
        )
    draw.text(
        xy, text, font=font, fill=fill, anchor=anchor,
        stroke_width=2, stroke_fill=(0, 0, 0, 150),
    )


def _pill(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    fill: tuple = (0, 0, 0, 140),
    text_fill: tuple = WHITE,
    pad_x: int = 26,
    pad_y: int = 13,
    radius: int = 999,
) -> tuple[int, int, int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = (bbox[2] - bbox[0]) + pad_x * 2
    h = (bbox[3] - bbox[1]) + pad_y * 2
    x0, y0 = cx - w // 2, cy - h // 2
    draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=radius, fill=fill)
    draw.text((cx, cy), text, font=font, fill=text_fill, anchor="mm")
    return x0, y0, x0 + w, y0 + h


def _gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
        ))
    return col.resize((w, h))


def _circle_avatar(img_path: Path, d: int) -> Optional[Image.Image]:
    try:
        im = Image.open(img_path).convert("RGBA").resize((d, d), Image.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d, d), fill=255)
    out = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _official_name_map() -> dict[str, str]:
    """Map FACEIT player_id -> official/canonical nickname.

    Source: .data/player_accounts.json (the Recognised Pros store). The intro
    card shows each player's FACEIT IGN; when that differs from their official
    name, we render the official name as an alias.
    """
    try:
        records = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(records, dict):
        records = records.get("players", [])
    out: dict[str, str] = {}
    for r in records:
        pid = r.get("faceit_id")
        nick = r.get("nickname")
        if pid and nick:
            out[str(pid)] = str(nick)
    return out


def _clean_team_name(name: str) -> str:
    """Strip FACEIT's auto 'team_' prefix / trailing '-' for display."""
    n = name
    if n.lower().startswith("team_"):
        n = n[5:]
    n = n.rstrip("-")
    return n or name


def _row_y(i: int) -> int:
    return ROWS_Y0 + i * ROW_PITCH


def _draw_roster(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    players: list[dict],
    cx: int,
    *,
    ring_fill: tuple,
    pov_nick: Optional[str],
) -> None:
    """Left team is left-aligned; right team is mirrored (right-aligned)."""
    right_side = cx > W // 2
    card_left = 1380 if right_side else 80
    card_right = card_left + 1100
    if right_side:
        avatar_x = card_right - 175
        text_x = card_right - 290
    else:
        avatar_x = card_left + 175
        text_x = card_left + 290
    for i, p in enumerate(players):
        ry = _row_y(i)
        av = p.get("avatar_img")
        if av is not None:
            draw.ellipse(
                [avatar_x - AVATAR_D // 2 - 4, ry - AVATAR_D // 2 - 4,
                 avatar_x + AVATAR_D // 2 + 4, ry + AVATAR_D // 2 + 4],
                fill=None, outline=ring_fill, width=4,
            )
            img.paste(
                av,
                (avatar_x - AVATAR_D // 2, ry - AVATAR_D // 2),
                av,
            )
        else:
            # placeholder: dark circle with initial
            r = AVATAR_D // 2
            draw.ellipse(
                [avatar_x - r, ry - r, avatar_x + r, ry + r],
                fill=(38, 46, 60), outline=ring_fill, width=4,
            )
            nick = p.get("nickname", "?")
            _text(
                draw, (avatar_x, ry), nick[:1].upper(), _font(46),
                MUTED, shadow=False,
            )

        is_pov = pov_nick is not None and (
            p["nickname"].lower() == pov_nick.lower()
            or (p.get("official") or "").lower() == pov_nick.lower()
        )
        name_font = _font(44)
        stat_font = _font(30)

        name_fill = ORANGE_SOFT if is_pov else WHITE
        name_y, stat_y = ry - 18, ry + 36
        nickname = p.get("nickname")
        alias = p.get("alias")
        gap = 14
        nickname_w = draw.textlength(nickname, font=name_font)
        sep_w = draw.textlength(" / ", font=name_font)
        alias_w = draw.textlength(alias, font=name_font) if alias else 0
        name_block_w = nickname_w + (sep_w + alias_w if alias else 0)

        if right_side:
            # Right-aligned: alias, then " / ", then nickname up to text_x.
            x = text_x
            if alias:
                _text(
                    draw, (x, name_y), alias, name_font,
                    GREY, anchor="rm", shadow=False,
                )
                x -= alias_w + gap
                _text(
                    draw, (x, name_y), " / ", name_font,
                    MUTED, anchor="rm", shadow=False,
                )
                x -= sep_w + gap
            _text(
                draw, (x, name_y), nickname, name_font,
                name_fill, anchor="rm", shadow=False,
            )
            if is_pov:
                _pill(
                    draw, max(text_x - name_block_w - 62, card_left + 78),
                    name_y, "POV", _font(22),
                    fill=(255, 85, 0, 255), pad_x=10, pad_y=5, radius=10,
                )
            stat = p.get("stat_line", "")
            _text(
                draw, (text_x, stat_y), stat, stat_font,
                MUTED, anchor="rm", shadow=False,
            )
        else:
            _text(
                draw, (text_x, name_y), nickname, name_font,
                name_fill, anchor="lm", shadow=False,
            )
            if alias:
                _text(
                    draw, (text_x + nickname_w + gap, name_y),
                    " / ", name_font, MUTED, anchor="lm", shadow=False,
                )
                _text(
                    draw, (text_x + nickname_w + gap + sep_w + gap, name_y),
                    alias, name_font, GREY, anchor="lm", shadow=False,
                )
            if is_pov:
                _pill(
                    draw, min(text_x + name_block_w + 62, card_right - 78),
                    name_y, "POV", _font(22),
                    fill=(255, 85, 0, 255), pad_x=10, pad_y=5, radius=10,
                )
            stat = p.get("stat_line", "")
            _text(
                draw, (text_x, stat_y), stat, stat_font,
                MUTED, anchor="lm", shadow=False,
            )


async def _fetch_match(match_id: str, skip_elo: bool) -> dict:
    client = FACEITClient()
    details = await client._request("GET", f"/matches/{match_id}")
    stats = await client.get_match_stats(match_id) or {}
    raw_stats = await client._request("GET", f"/matches/{match_id}/stats")

    region = ""
    rounds_n = ""
    for rnd in raw_stats.get("rounds", []):
        rs = rnd.get("round_stats", {}) or {}
        region = rs.get("Region", region)
        rounds_n = rs.get("Rounds", rounds_n)

    teams = details.get("teams", {})
    factions = {
        "faction1": teams.get("faction1", {}),
        "faction2": teams.get("faction2", {}),
    }
    results = details.get("results", {}) or {}
    score = results.get("score", {}) or {}
    winner = results.get("winner")

    started = details.get("started_at")
    date_str = ""
    if started:
        try:
            date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            pass

    elo_by_pid: dict[str, int] = {}
    if not skip_elo:
        pids = [
            p.get("player_id")
            for f in factions.values()
            for p in f.get("roster", [])
            if p.get("player_id")
        ]
        for i, pid in enumerate(pids):
            elo = await client.get_player_elo(pid)
            if elo:
                elo_by_pid[pid] = elo
            await asyncio.sleep(settings.faceit_request_delay)

    players_by_nick = stats.get("players", {})
    official_names = _official_name_map()
    team_list: list[dict] = []
    for fkey, f in factions.items():
        roster = []
        for p in f.get("roster", []):
            nick = p.get("nickname", "?")
            ps = players_by_nick.get(nick, {})
            pid = p.get("player_id", "")
            official = official_names.get(str(pid))
            roster.append({
                "nickname": nick,
                "official": official or nick,
                "player_id": pid,
                "avatar": p.get("avatar", ""),
                "kills": ps.get("kills", "?"),
                "deaths": ps.get("deaths", "?"),
                "kd": ps.get("kd", "?"),
                "adr": ps.get("adr", "?"),
                "hs": ps.get("hs", "?"),
                "elo": elo_by_pid.get(pid),
                "team": fkey,
                "is_winner": fkey == winner,
            })
        elo_vals = [r["elo"] for r in roster if r.get("elo")]
        team_list.append({
            "key": fkey,
            "name": f.get("name", "Unknown"),
            "score": score.get(fkey, "?"),
            "is_winner": fkey == winner,
            "avg_elo": round(sum(elo_vals) / len(elo_vals)) if elo_vals else None,
            "roster": roster,
        })

    return {
        "match_id": match_id,
        "status": details.get("status"),
        "map": stats.get("map", "Unknown"),
        "date": date_str,
        "region": region,
        "rounds": rounds_n,
        "best_of": details.get("best_of"),
        "winner": winner,
        "teams": team_list,
        "url": f"https://www.faceit.com/en/cs2/room/{match_id}",
    }


async def _download_avatars(data: dict, av_dir: Path) -> None:
    av_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        for team in data["teams"]:
            for p in team["roster"]:
                if not p.get("avatar"):
                    continue
                fname = re.sub(r"[^A-Za-z0-9_.-]", "_", p["nickname"]) + ".jpg"
                dest = av_dir / fname
                if dest.exists() and dest.stat().st_size > 0:
                    p["avatar_path"] = str(dest)
                    continue
                try:
                    r = await c.get(p["avatar"])
                    if r.status_code == 200 and r.content:
                        dest.write_bytes(r.content)
                        p["avatar_path"] = str(dest)
                    else:
                        print(f"  [WARN] avatar {p['nickname']}: HTTP {r.status_code}")
                except Exception as e:
                    print(f"  [WARN] avatar {p['nickname']}: {e}")


def render_card(data: dict, out_path: Path) -> None:
    """Render two balanced team cards on a transparent canvas."""
    score_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    score_draw = ImageDraw.Draw(score_layer)

    # Compact metadata chip in the transparent space above both cards.
    score_draw.rounded_rectangle(
        [900, 35, 1660, 117], radius=20,
        fill=(7, 8, 10, 245), outline=(255, 255, 255, 38), width=2,
    )

    map_disp = data["map"].replace("de_", "").upper() or "?"
    pill_parts = [("FACEIT CS2", ORANGE_SOFT), ("  •  ", MUTED),
                  (f"{map_disp}  •  {data['date']}  •  {data['region']}", WHITE)]
    f_pill = _font(30)
    total_w = sum(score_draw.textlength(t, font=f_pill) for t, _ in pill_parts)
    x = TOP_PILL_CX - total_w // 2
    for t, col in pill_parts:
        tw = score_draw.textlength(t, font=f_pill)
        _text(score_draw, (x + tw / 2, TOP_PILL_Y), t, f_pill, col, shadow=False)
        x += tw

    t1, t2 = data["teams"][0], data["teams"][1]

    left_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    right_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_layer)
    right_draw = ImageDraw.Draw(right_layer)

    # Each team is one coherent card: score, identity, ELO, and roster.
    left_draw.rounded_rectangle(
        [80, 145, 1180, 1370], radius=28,
        fill=(7, 8, 10, 242), outline=(255, 255, 255, 45), width=2,
    )
    right_draw.rounded_rectangle(
        [1380, 145, 2480, 1370], radius=28,
        fill=(7, 8, 10, 242), outline=(255, 255, 255, 45), width=2,
    )
    # A short top accent identifies the winner without outlining the whole card.
    winner_draw = left_draw if t1["is_winner"] else right_draw
    winner_x0 = 80 if t1["is_winner"] else 1380
    winner_draw.rounded_rectangle(
        [winner_x0 + 390, 145, winner_x0 + 710, 151],
        radius=3, fill=ORANGE,
    )

    score_font = _font(158)
    s1_col = ORANGE_SOFT if t1["is_winner"] else GREY
    s2_col = ORANGE_SOFT if t2["is_winner"] else GREY
    _text(left_draw, (C_LEFT, SCORE_Y), str(t1["score"]),
          score_font, s1_col, shadow=False)
    _text(right_draw, (C_RIGHT, SCORE_Y), str(t2["score"]),
          score_font, s2_col, shadow=False)

    # VS belongs to neither card and remains legible over gameplay.
    _pill(
        score_draw, W // 2, 340, "VS", _font(28),
        fill=(7, 8, 10, 235), text_fill=MUTED,
        pad_x=18, pad_y=9,
    )

    url_font = _font(22)
    url_w = score_draw.textlength(data["url"], font=url_font)
    score_draw.rounded_rectangle(
        [W // 2 - url_w / 2 - 24, 1384,
         W // 2 + url_w / 2 + 24, 1432],
        radius=14, fill=(7, 8, 10, 235),
        outline=(255, 255, 255, 32), width=1,
    )
    _text(
        score_draw, (W // 2, 1408), data["url"],
        url_font, MUTED, shadow=False,
    )

    name_font = _font(54)
    n1_col = WHITE if t1["is_winner"] else GREY
    n2_col = WHITE if t2["is_winner"] else GREY
    _text(left_draw, (C_LEFT, NAME_Y), _clean_team_name(t1["name"]), name_font, n1_col, shadow=False)
    _text(right_draw, (C_RIGHT, NAME_Y), _clean_team_name(t2["name"]), name_font, n2_col, shadow=False)
    elo_font = _font(30)
    e1 = f"AVG {t1['avg_elo']} ELO" if t1.get("avg_elo") else ""
    e2 = f"AVG {t2['avg_elo']} ELO" if t2.get("avg_elo") else ""
    if e1:
        _text(left_draw, (C_LEFT, ELO_Y), e1, elo_font, ORANGE_SOFT, shadow=False)
    if e2:
        _text(right_draw, (C_RIGHT, ELO_Y), e2, elo_font, ORANGE_SOFT, shadow=False)
    left_draw.line([(170, 535), (1090, 535)], fill=(255, 255, 255, 42), width=2)
    right_draw.line([(1470, 535), (2390, 535)], fill=(255, 255, 255, 42), width=2)

    # Rosters
    pov_nick = data.get("pov_player")
    for t, layer, layer_draw, cx, ring in (
        (t1, left_layer, left_draw, C_LEFT, ORANGE if t1["is_winner"] else WHITE),
        (t2, right_layer, right_draw, C_RIGHT, ORANGE if t2["is_winner"] else WHITE),
    ):
        for p in t["roster"]:
            av_path = p.get("avatar_path")
            p["avatar_img"] = _circle_avatar(Path(av_path), AVATAR_D) if av_path else None
            ps = f"{p['kills']}-{p['deaths']}"
            extra = []
            if p.get("adr") and str(p["adr"]) != "?":
                extra.append(f"{p['adr']} ADR")
            if p.get("hs") and str(p["hs"]) != "?":
                extra.append(f"{p['hs']}% HS")
            p["stat_line"] = f"{ps}   •   " + "  •  ".join(extra) if extra else ps
            official = p.get("official") or p["nickname"]
            nick_l = p["nickname"].lower()
            off_l = official.lower()
            p["alias"] = official if off_l != nick_l and not (
                off_l in nick_l or nick_l in off_l
            ) else None
        _draw_roster(layer, layer_draw, t["roster"], cx, ring_fill=ring, pov_nick=pov_nick)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The primary result stays transparent outside its three cards. The
    # individual RGBA images can also be independently composited in ffmpeg.
    final = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for layer in (score_layer, left_layer, right_layer):
        final.alpha_composite(layer)
    final.save(out_path, "PNG")
    score_layer.save(out_path.with_name("intro_score.png"), "PNG")
    left_layer.save(out_path.with_name("intro_team_left.png"), "PNG")
    right_layer.save(out_path.with_name("intro_team_right.png"), "PNG")
    print(f"[OK] intro card: {out_path}")
    print("[OK] transparent layers: intro_score.png, intro_team_left.png, intro_team_right.png")


def _load_backlog(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def run(match_id: str, output: Path, skip_elo: bool,
              backlog: Optional[dict], pov_player: Optional[str]) -> None:
    print(f"[1/4] fetching match {match_id} from FACEIT API ...")
    data = await _fetch_match(match_id, skip_elo)
    data["pov_player"] = pov_player or (backlog or {}).get("player")
    if not data["teams"] or data["status"] != "FINISHED":
        print(f"  [WARN] match status: {data.get('status')}")

    for t in data["teams"]:
        print(f"  {t['name']}  [{t['score']}]  avg_elo={t.get('avg_elo')}")
        for p in t["roster"]:
            print(f"    {p['nickname']:<14} {p['kills']}-{p['deaths']}  "
                  f"ADR {p['adr']}  HS {p['hs']}%  ELO {p.get('elo')}")

    print(f"[2/4] downloading {sum(len(t['roster']) for t in data['teams'])} avatars ...")
    av_dir = output / "avatars"
    await _download_avatars(data, av_dir)

    print("[3/4] rendering card ...")
    render_card(data, output / "intro.png")

    print("[4/4] saving details json ...")
    for t in data["teams"]:
        for p in t["roster"]:
            p.pop("avatar_img", None)
    (output / "intro_details.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    print("[OK] done.")


def main() -> None:
    ap = argparse.ArgumentParser(description="FACEIT match intro card")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--match-id", help="FACEIT match id (or room URL)")
    g.add_argument("--backlog", help="Path to a backlog card json (reads faceit_match_id + player)")
    g.add_argument("--details", help="Existing intro_details.json; re-render without FACEIT API calls")
    ap.add_argument("--output", default=None,
                    help="Output dir (default: renders/intro-<match_id>)")
    ap.add_argument("--skip-elo", action="store_true",
                    help="Skip per-player ELO fetches")
    ap.add_argument("--player", default=None,
                    help="POV player nickname to highlight (default: from --backlog)")
    args = ap.parse_args()

    backlog = None
    if args.details:
        details_path = Path(args.details)
        data = json.loads(details_path.read_text(encoding="utf-8"))
        output = Path(args.output) if args.output else details_path.parent
        render_card(data, output / "intro.png")
        return
    if args.backlog:
        backlog = _load_backlog(Path(args.backlog))
        match_id = backlog.get("faceit_match_id") or backlog.get("match_id")
        if not match_id:
            print("[ERR] backlog card has no faceit_match_id")
            sys.exit(1)
    else:
        match_id = args.match_id
        if "/room/" in match_id:
            match_id = match_id.rstrip("/").split("/room/")[-1]

    output = Path(args.output) if args.output else PROJECT_ROOT / "renders" / f"intro-{match_id}"
    asyncio.run(run(match_id, output, args.skip_elo, backlog, args.player))


if __name__ == "__main__":
    main()
