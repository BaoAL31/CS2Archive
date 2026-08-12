"""Generate YouTube title/description/tags for a FACEIT POV demo.

No HLTV ratings/avatars — FACEIT demos carry player names + steam IDs in the
header (via demoparser2). The POV player comes from the backlog (--player /
--steam-id); any other demo player who is a Recognised Pro
(``.data/player_accounts.json``) is treated as a "notable name" and listed in
the description ("Also featuring: ...").

Usage:
    python scripts/faceit/faceit_title.py <demo_path> --player <nick> [--map <map>]
                                  [--steam-id <id>] [--elo <int>] [--opp-elo <int>]
                                  [--kd <float>] [--match-id <id>] [--crosshair-code <code>]
                                  [--viewmodel-fov <n>] [--viewmodel-offset-{x,y,z} <n>]
                                  [--viewmodel-presetpos <n>] [--resolution <res>]
                                  [--aspect-ratio <ar>] [--scaling-mode <mode>]
                                  [--video-settings-source <source>]
Prints JSON: {"title": ..., "description": ..., "tags": [...]}

The description carries the POV player's actual settings as rendered: their
crosshair share code (decoded from the demo via csdm analysis) and their
viewmodel/resolution from prosettings.net, plus a link to the FACEIT room
when a match id is available. No render-credit boilerplate.

The individual FACEIT title deliberately omits team names / tournament name /
stage — a FACEIT pug has none worth showing. When --elo/--opp-elo are present
the title reads "<player> <elo> ELO vs <opp-elo> ELO | <map> | FACEIT CS2 POV".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

MAP_DISPLAY = {
    "de_ancient": "Ancient", "de_mirage": "Mirage", "de_inferno": "Inferno",
    "de_nuke": "Nuke", "de_anubis": "Anubis", "de_overpass": "Overpass",
    "de_vertigo": "Vertigo", "de_dust2": "Dust2", "de_train": "Train",
}

# CS2 cl_crosshairstyle display names (share-code style 0-5).
_CROSSHAIR_STYLES = {
    0: "Default", 1: "Default", 2: "Classic", 3: "Classic Static",
    4: "Classic Dynamic", 5: "Classic Dynamic Legacy",
}
# Share-code preset colors 0-3; 4+ are custom RGB (see crosshair_code.py).
_COLOR_NAMES = {0: "green", 1: "red", 2: "blue", 3: "yellow"}


def _crosshair_summary(code: str) -> str | None:
    """Compact one-line crosshair summary from a share code, or None."""
    try:
        from crosshair_code import decode_crosshair
        ch = decode_crosshair(code)
    except Exception:
        return None
    parts = [
        _CROSSHAIR_STYLES.get(ch["style"], f"style {ch['style']}"),
        f"size {ch['length']:g}",
        f"thickness {ch['thickness']:g}",
        f"gap {ch['gap']:g}",
    ]
    if ch.get("centerDotEnabled"):
        parts.append("dot")
    if ch.get("outlineEnabled"):
        parts.append(f"outline {ch['outline']:g}")
    color = ch.get("color")
    if color in _COLOR_NAMES:
        parts.append(_COLOR_NAMES[color])
    else:
        parts.append(f"rgb({ch.get('red', 0)},{ch.get('green', 0)},{ch.get('blue', 0)})")
    return ", ".join(parts)


def _viewmodel_line(video: dict) -> str | None:
    """'FOV 68, offset 2.5/0/-1.5, presetpos 2' from whatever fields exist."""
    fov = video.get("viewmodel_fov")
    ox, oy, oz = video.get("viewmodel_offset_x"), video.get("viewmodel_offset_y"), video.get("viewmodel_offset_z")
    preset = video.get("viewmodel_presetpos")
    if fov is None and ox is None and oy is None and oz is None and preset is None:
        return None
    parts = []
    if fov is not None:
        parts.append(f"FOV {fov:g}")
    if any(v is not None for v in (ox, oy, oz)):
        if all(v is not None for v in (ox, oy, oz)):
            parts.append(f"offset {ox:g}/{oy:g}/{oz:g}")
        else:
            labels = [f"X {ox:g}" for ox in (ox,) if ox is not None] \
                   + [f"Y {oy:g}" for oy in (oy,) if oy is not None] \
                   + [f"Z {oz:g}" for oz in (oz,) if oz is not None]
            parts.append("offset " + ", ".join(labels))
    if preset is not None:
        parts.append(f"presetpos {preset:g}")
    return ", ".join(parts)


def _demo_players(demo_path: Path) -> list[dict]:
    """Return list of {name, steamid, team_number} from the demo header."""
    import demoparser2 as dp
    parser = dp.DemoParser(str(demo_path))
    info = parser.parse_player_info()
    out = []
    for _, row in info.iterrows():
        out.append({
            "name": str(row.get("name", "")).strip(),
            "steamid": str(row.get("steamid", "")).strip(),
            "team_number": int(row.get("team_number", 0)),
        })
    return out


def _notable_nicks() -> set[str]:
    """Lowercase nick set for all Recognised Pros (player_accounts.json)."""
    try:
        from faceit_names import known_pros
        return known_pros()
    except Exception:
        return set()


def _opponent_pros(players: list[dict], pov_steam_id: str, pov_name: str) -> list[str]:
    """Canonical nicks (uppercased) of Recognised Pros on the opponent team.

    ``players`` are demo {name, steamid, team_number} rows. The opponent team is
    whichever team_number is *not* the POV player's. Returns the canonical pro
    names of opponent-team members (matched by steam ID via player_accounts),
    uppercased for the "vs DONK & MAGIXX" title, or [] when none.
    """
    try:
        from faceit_names import canonical_nick, known_pro_steam_ids
    except Exception:
        return []
    # Find the POV player's team.
    pov_team = None
    for p in players:
        if p["steamid"] == pov_steam_id:
            pov_team = p["team_number"]
            break
    if pov_team is None and pov_name:
        for p in players:
            if p["name"].strip().lower() == pov_name.strip().lower():
                pov_team = p["team_number"]
                break
    if pov_team is None:
        return []

    pros = known_pro_steam_ids()
    out: list[str] = []
    for p in players:
        if p["team_number"] == pov_team:
            continue
        nick = pros.get(p["steamid"])
        if nick:
            out.append(canonical_nick(nick).upper())
    return out


def _map_from_demo(demo_path: Path) -> str:
    """Best-effort map name from the demo filename or header."""
    m = re.search(r"(de_[a-z0-9]+)", demo_path.name, re.I)
    if m:
        key = m.group(1).lower()
        return MAP_DISPLAY.get(key, key.replace("de_", "").capitalize())
    return "Unknown"


def build_title(player: str, map_name: str, notable: list[str],
                elo: int | None = None, opp_elo: int | None = None,
                kd: str | None = None, *, voice_comms: bool = False,
                opponent_pros: list[str] | None = None) -> str:
    """Title: "{player} ({kd}) {elo} ELO vs ~{opp_elo} ELOs | {map} | FACEIT CS2 POV".

    ``kd`` is a "kills/deaths" string (e.g. "34/11"), rendered hyphenated
    "(34-11)" to match the thumbnail style. The opponent ELO is a team
    average, signalled by "~" + plural "ELOs". When ``voice_comms`` is true a
    " + VOICE COMMS" suffix is appended.

    When the opposing team contains recognised pros, ``opponent_pros`` carries
    their canonical nicks (uppercased) and the ELO-vs-ELO portion is replaced
    with "vs DONK & MAGIXX" (more compelling than an ELO average).
    """
    kd_part = f"({kd.replace('/', '-')}) " if kd else ""
    suffix = " + VOICE COMMS" if voice_comms else ""
    if opponent_pros:
        vs = " & ".join(opponent_pros)
        return f"{player} {kd_part}vs {vs} | {map_name} | FACEIT CS2 POV{suffix}"[:100]
    if elo is not None and opp_elo is not None:
        # Opponent ELO is an average of the opposing team, so prefix with "~"
        # and pluralise "ELOs" to signal it's a team average, not one player's
        # exact ELO. E.g. "3631 ELO vs ~3566 ELOs".
        return f"{player} {kd_part}{elo} ELO vs ~{opp_elo} ELOs | {map_name} | FACEIT CS2 POV{suffix}"[:100]
    return f"{player} {kd_part}| {map_name} | FACEIT CS2 POV{suffix}"[:100]


def build_description(player: str, notable: list[str], elo: int | None,
                      opp_elo: int | None, *,
                      match_id: str = "", crosshair_code: str = "",
                      video: dict | None = None) -> str:
    lines: list[str] = []
    if elo is not None and opp_elo is not None:
        lines.append(f"Match ELO: {player} {elo} vs {opp_elo} (opponent team average).")
    if notable:
        lines.append(f"Also featuring: {', '.join(notable)}.")
    if match_id:
        if lines:
            lines.append("")
        lines.append(f"Match: https://www.faceit.com/en/cs2/room/{match_id}")

    settings: list[str] = []
    if crosshair_code:
        summary = _crosshair_summary(crosshair_code)
        settings.append(f"Crosshair: {crosshair_code}"
                        + (f" ({summary})" if summary else ""))
    video = video or {}
    vm = _viewmodel_line(video)
    if vm:
        settings.append(f"Viewmodel: {vm}")
    if video.get("video_settings_source") == "prosettings" and video.get("resolution"):
        extra = [e for e in (video.get("aspect_ratio"), video.get("scaling_mode")) if e]
        settings.append(f"Resolution: {video['resolution']}"
                        + (f" ({', '.join(extra)})" if extra else ""))
    if settings:
        if lines:
            lines.append("")
        lines.append("Settings (as rendered):")
        lines.extend(settings)
    return "\n".join(lines)


def _num(value: str) -> int | float | None:
    """Coerce a CLI string to int/float for display (None when empty)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value) if "." in str(value) else int(value)
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path", help="Path to the FACEIT .dem file")
    ap.add_argument("--player", required=True, help="POV player nickname")
    ap.add_argument("--map", default="", help="Map name (auto-detected if omitted)")
    ap.add_argument("--steam-id", default="", help="POV steam ID (optional)")
    ap.add_argument("--elo", type=int, default=None, help="POV player's FACEIT ELO")
    ap.add_argument("--opp-elo", type=int, default=None, help="Average FACEIT ELO of the opposing team")
    ap.add_argument("--kd", default=None, help="POV player's K/D as kills/deaths, e.g. '34/11' (shown in title)")
    ap.add_argument("--voice-comms", action="store_true", help="Append ' + VOICE COMMS' to the title")
    ap.add_argument("--match-id", default="", help="FACEIT match id → room link in description")
    ap.add_argument("--crosshair-code", default="", help="POV player's crosshair share code (from csdm analysis)")
    ap.add_argument("--viewmodel-fov", default="", help="Viewmodel FOV as rendered")
    ap.add_argument("--viewmodel-offset-x", default="", help="Viewmodel offset X as rendered")
    ap.add_argument("--viewmodel-offset-y", default="", help="Viewmodel offset Y as rendered")
    ap.add_argument("--viewmodel-offset-z", default="", help="Viewmodel offset Z as rendered")
    ap.add_argument("--viewmodel-presetpos", default="", help="Viewmodel presetpos as rendered")
    ap.add_argument("--resolution", default="", help="Capture resolution (e.g. 1280x960)")
    ap.add_argument("--aspect-ratio", default="", help="Aspect ratio (e.g. 4:3)")
    ap.add_argument("--scaling-mode", default="", help="Scaling mode (e.g. Stretched)")
    ap.add_argument("--video-settings-source", default="",
                    help="Source of video settings ('prosettings' gates the resolution line)")
    args = ap.parse_args()

    demo = Path(args.demo_path)
    if not demo.exists():
        print(json.dumps({"title": f"{args.player} | {args.map or 'FACEIT'}",
                          "description": "", "tags": []}))
        return

    players = _demo_players(demo)
    map_name = args.map or _map_from_demo(demo)

    # Canonicalize the POV player name (proper casing: NiKo, TeSeS, ...)
    from faceit_names import canonical_nick
    player = canonical_nick(args.player)

    notable = []
    pro_set = _notable_nicks()
    for p in players:
        nick = p["name"]
        if nick.lower() == args.player.strip().lower():
            continue
        if p["steamid"] == args.steam_id:
            continue
        if nick.lower() in pro_set:
            notable.append(canonical_nick(nick))

    video = {}
    for key in ("viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
                "viewmodel_offset_z", "viewmodel_presetpos"):
        val = _num(getattr(args, key))
        if val is not None:
            video[key] = val
    if args.resolution:
        video["resolution"] = args.resolution
    if args.aspect_ratio:
        video["aspect_ratio"] = args.aspect_ratio
    if args.scaling_mode:
        video["scaling_mode"] = args.scaling_mode
    if args.video_settings_source:
        video["video_settings_source"] = args.video_settings_source

    title = build_title(player, map_name, notable, args.elo, args.opp_elo, args.kd,
                        voice_comms=args.voice_comms,
                        opponent_pros=_opponent_pros(players, args.steam_id, args.player))
    description = build_description(
        player, notable, args.elo, args.opp_elo,
        match_id=args.match_id.strip(), crosshair_code=args.crosshair_code.strip(),
        video=video,
    )
    tags = ["FACEIT", "CS2", "POV", map_name, player] + notable
    tags = list(dict.fromkeys(t for t in tags if t))[:10]

    print(json.dumps({
        "title": title,
        "description": description,
        "tags": tags,
    }, indent=2))


if __name__ == "__main__":
    main()
