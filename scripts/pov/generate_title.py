"""Generate YouTube video title and description from ratings data."""

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
from faceit_title import _crosshair_summary, _num, _viewmodel_line


def _strip_html(text: str) -> str:
    """Remove markup/entities and collapse whitespace from a scraped field.

    HLTV scrapes can carry raw markup (e.g. the veto-box) into fields like
    match_stage; stripping here keeps descriptions and tags clean.
    """
    if not text:
        return ""
    text = re.sub(r"<[^>]+", " ", text)
    text = re.sub(r"[<>]+", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_team_names(ratings_data: dict) -> tuple[str, str]:
    tables = ratings_data.get("tables", [])
    teams: list[str] = []
    for t in tables:
        name = t.get("team", "").strip()
        if name and name not in teams:
            teams.append(name)
    if len(teams) >= 2:
        return (teams[0], teams[1])
    return ("Team A", "Team B")


def find_player_map_stats(ratings_data: dict, player: str, map_name: str) -> dict | None:
    tables = ratings_data.get("tables", [])
    player_lower = player.strip().lower()

    for t in tables:
        t_map = t.get("map", "").strip().lower()
        if "series overall" in t_map:
            continue
        if map_name.lower() not in t_map:
            continue
        for p in t.get("players", []):
            nick = p.get("nickname", "").strip().lower()
            if nick == player_lower:
                return p
    return None


def shorten_team(name: str) -> str:
    name = name.strip()
    overrides = {
        "natus vincere": "NAVI",
        "ninjas in pyjamas": "NiP",
        "team vitality": "Vitality",
        "team spirit": "Spirit",
        "team liquid": "Liquid",
        "faze clan": "FaZe",
        "gamerlegion": "GL",
        "betboom": "BB",
        "the mongolz": "MongolZ",
        "bc.game": "BC.Game",
        "passion ua": "Passion UA",
        "fnatic": "Fnatic",
        "paiN": "paiN",
        "complexity": "COL",
    }
    key = name.lower()
    if key in overrides:
        return overrides[key]
    return name


def shorten_tournament(name: str) -> str:
    """Shorten verbose tournament names for titles."""
    name = name.strip()
    # Explicit abbreviations for verbose tournament names. Keep these tight
    # so the title stays under YouTube's 100-char limit (stage is trimmed
    # first, then map when over budget).
    # Keyed case-insensitively because shorten_tournament receives the raw
    # tournament string (e.g. "XSE Pro League Guangzhou 2026").
    ABBREV = {
        "xse pro league guangzhou 2026": "XSE PL 2026",
    }
    if name.lower() in ABBREV:
        return ABBREV[name.lower()]
    result = name.title()
    for short, full in {
        "Iem": "IEM",
        "Blast": "BLAST",
        "Pgl": "PGL",
        "Esl": "ESL",
        "Cac": "CAC",
        "Cs Asia Championships": "CAC",
    }.items():
        result = result.replace(short, full)
    result = result.replace("Season ", "S")
    return result


def normalize_stage(stage: str) -> str:
    """Extract core stage name from verbose HLTV stage strings."""
    if not stage:
        return ""
    stage_lower = stage.lower()
    if "grand final" in stage_lower:
        return "Grand Final"
    if "semi" in stage_lower:
        return "Semi-Final"
    if "quarter" in stage_lower:
        return "Quarter-Final"
    if "final" in stage_lower:
        return "Final"
    if "playoff" in stage_lower:
        return "Playoff"
    if "group" in stage_lower:
        return "Group Stage"
    return stage.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate YouTube video title and description")
    parser.add_argument("ratings_json", help="Path to ratings JSON file")
    parser.add_argument("--player", required=True, help="Player nickname")
    parser.add_argument("--map", required=True, help="Map name")
    parser.add_argument("--tournament", default="", help="Tournament name")
    parser.add_argument("--team-a", help="Override team A name")
    parser.add_argument("--team-b", help="Override team B name")
    parser.add_argument(
        "--variant",
        choices=["raw", "overlay"],
        default="raw",
        help="Variant: 'raw' (default) or 'overlay' (suffix title/desc/tags)",
    )
    parser.add_argument("--crosshair-code", default="",
                        help="POV player's crosshair share code (from csdm analysis)")
    parser.add_argument("--viewmodel-fov", default="", help="Viewmodel FOV as rendered")
    parser.add_argument("--viewmodel-offset-x", default="", help="Viewmodel offset X as rendered")
    parser.add_argument("--viewmodel-offset-y", default="", help="Viewmodel offset Y as rendered")
    parser.add_argument("--viewmodel-offset-z", default="", help="Viewmodel offset Z as rendered")
    parser.add_argument("--viewmodel-presetpos", default="", help="Viewmodel presetpos as rendered")
    parser.add_argument("--resolution", default="", help="Capture resolution (e.g. 1280x960)")
    parser.add_argument("--aspect-ratio", default="", help="Aspect ratio (e.g. 4:3)")
    parser.add_argument("--scaling-mode", default="", help="Scaling mode (e.g. Stretched)")
    parser.add_argument("--video-settings-source", default="",
                        help="Source of video settings ('prosettings' gates the resolution line)")
    args = parser.parse_args()

    path = Path(args.ratings_json)
    if not path.exists():
        print(json.dumps({"error": f"Ratings file not found: {path}"}))
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))

    team_a, team_b = extract_team_names(data)
    if args.team_a:
        team_a = args.team_a
    if args.team_b:
        team_b = args.team_b

    team_a_short = shorten_team(team_a)
    team_b_short = shorten_team(team_b)

    stats = find_player_map_stats(data, args.player, args.map)
    rating = stats.get("rating", "").strip() if stats else "?.??"
    kd = stats.get("kd", "").strip() if stats else ""
    adr = stats.get("adr", "").strip() if stats else ""
    kast = stats.get("kast", "").strip() if stats else ""

    tournament = args.tournament or ""
    tournament = _strip_html(tournament)
    if not tournament:
        m = re.search(r"([A-Za-z\s]+20\d{2})", path.stem.replace("_", " "))
        if m:
            tournament = m.group(1)

    tournament_short = shorten_tournament(tournament)

    stage_raw = data.get("match_stage", "") if isinstance(data, dict) else ""
    stage_raw = _strip_html(stage_raw)
    # Drop the map-veto block (numbered "1. X removed …" list) HLTV appends
    # after the stage name, so it doesn't pollute description/stage tag.
    stage_raw = re.split(r"\s*\d+\.\s", stage_raw, maxsplit=1)[0].strip()
    stage = normalize_stage(stage_raw)

    # Title sections with removable priority (None = never drop).
    # Trim priority when over YouTube's 100-char limit:
    # 1=map (the only removable section left; stage + overlay suffix
    # are no longer in the title).
    # Player / teams / tournament are always kept.
    player_parts = [args.player]
    if kd:
        player_parts.append(f"({kd})")
    if rating != "?.??":
        player_parts.append(f"{rating} Rating POV")
    sections = [(" ".join(player_parts), None)]
    sections.append((f"{team_a_short} vs {team_b_short}", None))
    sections.append((args.map, 3))
    if tournament_short:
        sections.append((tournament_short, None))

    title = " | ".join(t for t, _ in sections)

    desc_lines = [
        f"{args.player}'s POV on {args.map}",
    ]
    if stage_raw:
        desc_lines.append(f"{tournament} - {stage_raw}" if tournament else stage_raw)
    elif tournament:
        desc_lines.append(tournament)
    if team_a and team_b:
        desc_lines.append(f"{team_a} vs {team_b}")
    desc_lines.append("")
    desc_lines.append(f"HLTV Rating 3.0: {rating}")
    if kd:
        desc_lines.append(f"K-D: {kd}")
    if adr:
        desc_lines.append(f"ADR: {adr}")
    if kast:
        desc_lines.append(f"KAST: {kast}")
    desc_lines.append("")
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(path))
    if date_match:
        desc_lines.append(date_match.group(1))

    # Settings (as rendered) — crosshair + viewmodel + resolution, mirrors the
    # FACEIT path so HLTV POVs advertise the player's actual settings too.
    settings: list[str] = []
    if args.crosshair_code:
        summary = _crosshair_summary(args.crosshair_code)
        settings.append(f"Crosshair: {args.crosshair_code}"
                        + (f" ({summary})" if summary else ""))
    video = {
        k: _num(getattr(args, k))
        for k in ("viewmodel_fov", "viewmodel_offset_x", "viewmodel_offset_y",
                  "viewmodel_offset_z", "viewmodel_presetpos")
    }
    video = {k: v for k, v in video.items() if v is not None}
    vm = _viewmodel_line(video)
    if vm:
        settings.append(f"Viewmodel: {vm}")
    if args.video_settings_source == "prosettings" and args.resolution:
        extra = [e for e in (args.aspect_ratio, args.scaling_mode) if e]
        settings.append(f"Resolution: {args.resolution}"
                        + (f" ({', '.join(extra)})" if extra else ""))
    if settings:
        desc_lines.append("")
        desc_lines.append("Settings (as rendered):")
        desc_lines.extend(settings)

    description = "\n".join(desc_lines)

    # Tags from normalized stage
    stage_tag = stage if stage else None

    tags = list(filter(None, [
        args.player,
        args.map,
        team_a_short,
        team_b_short,
        tournament or None,
        stage_tag,
        f"{args.player} cs2",
        f"{team_a_short} CS2",
        f"{team_b_short} CS2",
        "CS2",
        "Counter-Strike 2",
        "Counter Strike 2",
        "CS2 POV",
        "CS2 gameplay",
        "CS2 full match",
        "POV",
        "Full Match",
        "HLTV",
        "Counter-Strike",
        "eSports",
        "Competitive",
        "Gaming",
    ]))

    if args.variant == "overlay":
        extra_overlay_tags = [
            "input overlay",
            "utility cam",
            "CS2 overlay",
            "keyboard overlay",
            "mouse input",
            "CS2 utility cam",
            "smoke lineup",
        ]
        description = (
            f"{description}\n\n"
            "Real-time keyboard & mouse input overlay plus utility trajectory "
            "clips for smokes, flashes, molotovs, and other grenades."
        )
        # Overlay label stays in description + tags only; not in the title
        # (it's already shown in the thumbnail).
        tags = tags + extra_overlay_tags

    # Enforce YouTube's 100-char title limit for ALL variants: drop the
    # map section (the only removable one), then hard-truncate as a last
    # resort. Player/teams/tournament kept.
    removable = sorted([s for s in sections if s[1] is not None],
                       key=lambda s: s[1])
    while len(title) > 100 and removable:
        victim = removable.pop(0)
        sections = [s for s in sections if s != victim]
        title = " | ".join(t for t, _ in sections)
    if len(title) > 100:
        title = title[:100]

    print(json.dumps({"title": title, "description": description, "tags": tags}))


if __name__ == "__main__":
    main()
