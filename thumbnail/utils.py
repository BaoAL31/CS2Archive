from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ANALYSIS_DIR = Path("demos/analysis")
AVATAR_DIR = Path("demos/avatars")
YOUTUBE_DIR = Path("youtube")

# Avatar subfolder layout: demos/avatars/{nick}/{source}/{nick}.png
# where source is one of "hltv" or "faceit". HLTV takes priority.
AVATAR_SOURCES = ("hltv", "faceit")
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# HLTV human-readable map name → CS2 game file name
MAP_NAME_MAP: dict[str, str] = {
    "Mirage": "de_mirage",
    "Inferno": "de_inferno",
    "Dust2": "de_dust2",
    "Nuke": "de_nuke",
    "Ancient": "de_ancient",
    "Anubis": "de_anubis",
    "Vertigo": "de_vertigo",
    "Overpass": "de_overpass",
    "Cache": "de_cache",
    "Train": "de_train",
    "Office": "cs_office",
    "Italy": "cs_italy",
}

# Reverse map: game file name → HLTV name
GAME_MAP_MAP: dict[str, str] = {v: k for k, v in MAP_NAME_MAP.items()}


def slugify(team1: str, team2: str) -> str:
    t1 = re.sub(r"[^a-z0-9]", "", team1.lower())
    t2 = re.sub(r"[^a-z0-9]", "", team2.lower())
    return f"{t1}-vs-{t2}"


def find_ratings_file(match_slug: str) -> Path | None:
    path = ANALYSIS_DIR / f"{match_slug}_ratings.json"
    if path.exists():
        return path
    for f in ANALYSIS_DIR.glob(f"{match_slug}*.json"):
        return f
    return None


def load_ratings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_player_stats(
    ratings: dict, player_nickname: str, map_name: str
) -> dict | None:
    for table in ratings.get("tables", []):
        if table["map"].lower() != map_name.lower():
            continue
        for p in table["players"]:
            if p["nickname"].strip().lower() == player_nickname.strip().lower():
                return p
    return None


def get_team_names(ratings: dict, map_name: str) -> tuple[str, str]:
    ABBR = {
        "Natus Vincere": "NaVi",
        "GamerLegion": "GL",
        "Team Falcons": "Falcons",
        "Natus Vincere Junior": "NAVI Junior",
    }
    raw_teams: list[str] = []
    for table in ratings.get("tables", []):
        if table["map"].lower() == map_name.lower():
            raw_teams.append(table["team"].strip())
    if len(raw_teams) >= 2:
        t1, t2 = raw_teams[0], raw_teams[1]
        return (ABBR.get(t1, t1), ABBR.get(t2, t2))
    for table in ratings.get("tables", []):
        if table["map"] == "Series Overall":
            raw_teams.append(table["team"].strip())
    if len(raw_teams) >= 2:
        t1, t2 = raw_teams[0], raw_teams[1]
        return (ABBR.get(t1, t1), ABBR.get(t2, t2))
    return ("Team 1", "Team 2")


def get_avatar_path(nickname: str) -> Path | None:
    """Return best avatar path for a nickname.

    Resolution order (per player folder):
      1. demos/avatars/{nick}/hltv/{nick}[_N].<ext>   (HLTV pro bodyshot wins)
      2. demos/avatars/{nick}/faceit/{nick}[_N].<ext>

    Within a source folder, the largest PNG (by pixel area) is chosen so the
    best-centered / highest-res variant wins.
    """
    name = nickname.strip().lower()

    def _best_in(folder: Path) -> Path | None:
        if not folder.is_dir():
            return None
        cands: list[Path] = []
        for ext in AVATAR_EXTS:
            cands.extend(folder.glob(f"{name}*.{ext.lstrip('.')}"))
        if not cands:
            return None
        best = max(
            cands,
            key=lambda p: (Image.open(p).size[0] * Image.open(p).size[1])
            if _safe_size(p)
            else 0,
        )
        return best

    for source in AVATAR_SOURCES:
        p = _best_in(AVATAR_DIR / name / source)
        if p:
            return p
    return None


def _safe_size(p: Path) -> bool:
    try:
        Image.open(p).size
        return True
    except Exception:
        return False


def get_slug_from_url(url: str) -> str | None:
    m = re.search(r"/matches/\d+/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


# POV-involved killfeed rows last 7.5s (5s lifetime × 1.5 local-player mod).
KILLFEED_POV_SECONDS = 7.5
# Capture after the last kill so the new row is actually on the HUD.
KILLFEED_AFTER_SECONDS = 0.25
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _kill_attacker_id(kill: dict) -> str:
    return str(
        kill.get("killerSteamId")
        or kill.get("attacker_steam_id")
        or kill.get("attacker_sid")
        or kill.get("attacker_steamid")
        or ""
    )


def load_kill_timeline(demo_path: str | Path) -> tuple[list[dict], float] | None:
    """Kills already parsed by shorts extraction (or highlights). Never re-parse."""
    stem = Path(demo_path).stem
    for path in (
        _PROJECT_ROOT / "renders" / "shorts" / f"shorts-{stem}" / "action_timeline.json",
        _PROJECT_ROOT / "renders" / f"hl-{stem}" / "action_timeline.json",
    ):
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        kills = data.get("kills") or []
        if kills:
            return kills, float(data.get("tickrate") or 64)
    return None


def rank_killfeed_kills(
    kills: list[dict],
    steam_id: str,
    tickrate: float = 64,
) -> list[tuple[dict, int]]:
    """POV kills ranked by how many of *this player's* rows are on the feed.

    Each POV kill is scored as the number of POV kills in the preceding
    ``KILLFEED_POV_SECONDS`` (inclusive). Capture at the last kill of the
    densest window. Ties prefer the later tick.
    """
    want = str(steam_id)
    pov = [k for k in kills if _kill_attacker_id(k) == want]
    if not pov:
        return []
    indexed = sorted(pov, key=lambda k: int(k["tick"]))
    ticks = [int(k["tick"]) for k in indexed]
    window = max(1, int(round(float(tickrate) * KILLFEED_POV_SECONDS)))
    left = 0
    scores: list[int] = []
    for right, t in enumerate(ticks):
        while t - ticks[left] > window:
            left += 1
        scores.append(right - left + 1)
    order = sorted(
        range(len(indexed)),
        key=lambda i: (scores[i], ticks[i]),
        reverse=True,
    )
    return [(indexed[i], scores[i]) for i in order]


def killfeed_chain_start_tick(
    kills: list[dict],
    steam_id: str,
    last_tick: int,
    tickrate: float = 64,
) -> int:
    """First POV kill still on the feed at ``last_tick``."""
    window = max(1, int(round(float(tickrate) * KILLFEED_POV_SECONDS)))
    want = str(steam_id)
    ticks = sorted(
        int(k["tick"]) for k in kills
        if _kill_attacker_id(k) == want
        and last_tick - window <= int(k["tick"]) <= last_tick
    )
    return ticks[0] if ticks else last_tick


def probe_video_duration(video: Path) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=60,
        )
        if probe.returncode != 0:
            return 0.0
        return float(probe.stdout.strip() or 0)
    except Exception:
        return 0.0


def sidecar_from_round_spans(
    spans: list[tuple[int, int, int]],
    video_duration: float,
    tickrate: float = 64,
) -> dict:
    """Distribute video time across round tick spans (sidecar fallback)."""
    usable = [(int(n), int(a), int(b)) for n, a, b in spans if int(b) > int(a)]
    weights = [(b - a) / max(float(tickrate), 1.0) for _, a, b in usable]
    total_w = sum(weights) or 1.0
    scale = float(video_duration) / total_w
    offsets: dict[str, float] = {}
    ticks: dict[str, list[int]] = {}
    durs: dict[str, float] = {}
    t = 0.0
    for (n, a, b), w in zip(usable, weights):
        d = w * scale
        offsets[str(n)] = round(t, 3)
        ticks[str(n)] = [a, b]
        durs[str(n)] = round(d, 3)
        t += d
    return {
        "round_offsets": offsets,
        "per_round_ticks": ticks,
        "per_round_durations": durs,
        "total_duration_seconds": round(float(video_duration), 3),
        "approximate": True,
    }


def kills_from_demo(demo_path: str | Path) -> tuple[list[dict], float] | None:
    """player_death rows as killfeed kills. Knife-round / suicides kept;
    rank_killfeed_kills filters by steam id."""
    try:
        import demoparser2 as dp
        parser = dp.DemoParser(str(demo_path))
        deaths = parser.parse_event("player_death")
    except Exception as e:
        print(f"  [bg] demo kill parse failed: {e}", flush=True)
        return None
    if deaths is None or len(deaths) == 0:
        return None
    tickrate = 64.0
    try:
        header = parser.parse_header()
        if isinstance(header, dict):
            tickrate = float(header.get("tickrate") or header.get("tick_rate") or 64)
    except Exception:
        pass
    kills: list[dict] = []
    for _, row in deaths.iterrows():
        att = str(row.get("attacker_steamid", "")).strip()
        vic = str(row.get("user_steamid", "")).strip()
        if not att or att.lower() == "nan" or att == vic:
            continue
        kills.append({
            "killerSteamId": att,
            "tick": int(row["tick"]),
            "weaponName": str(row.get("weapon") or row.get("weapon_name") or "?"),
            "victimName": str(row.get("user_name") or row.get("victim_name") or "?"),
        })
    if not kills:
        return None
    return kills, tickrate


def round_spans_from_demo(
    demo_path: str | Path,
    steam_id: str,
) -> list[tuple[int, int, int]]:
    """(round, start_tick, end_tick) matching CSDM's recorded POV window."""
    try:
        from overlay.overlay_pov import _load_pov_play_tick_ranges
        ranges = _load_pov_play_tick_ranges(Path(demo_path), str(steam_id))
    except Exception as e:
        print(f"  [bg] round-span parse failed: {e}", flush=True)
        return []
    out: list[tuple[int, int, int]] = []
    for rn, (a, b) in sorted(ranges.items()):
        if int(b) > int(a):
            out.append((int(rn), int(a), int(b)))
    return out


def find_round_offsets_sidecar(
    video_path: str | Path,
    extra: list[Path] | None = None,
) -> Path | None:
    """Sidecar next to the video, then any extra concat copies."""
    video = Path(video_path)
    candidates = [
        video.parent / f"{video.stem}.round_offsets.json",
        video.parent / "video.round_offsets.json",
        video.parent / "combined.round_offsets.json",
    ]
    if extra:
        candidates.extend(extra)
    for path in candidates:
        if path is not None and Path(path).is_file():
            return Path(path)
    return None


def demo_tick_to_video_seconds(sidecar: dict, tick: int) -> float | None:
    """Map a demo tick onto the concat/overlay timeline.

    Uses ``round_offsets`` + ``per_round_ticks`` + ``per_round_durations``
    from ``combined.round_offsets.json`` (copied next to youtube video as
    ``video.round_offsets.json``). Returns None if the tick sits in a cut.
    """
    offsets = sidecar.get("round_offsets") or {}
    ticks = sidecar.get("per_round_ticks") or {}
    durs = sidecar.get("per_round_durations") or {}
    if not offsets or not ticks:
        return None
    for rn, span in ticks.items():
        if rn not in offsets:
            continue
        a, b = int(span[0]), int(span[1])
        dur = float(durs.get(rn, 0.0))
        if b <= a or dur <= 0:
            continue
        if a <= tick <= b:
            return float(offsets[rn]) + (tick - a) / (b - a) * dur
    return None


def extract_killfeed_frame(
    video_path: str | Path,
    steam_id: str,
    *,
    demo_path: str | Path | None = None,
    sidecar_path: str | Path | None = None,
    extra_sidecars: list[Path] | None = None,
    analysis_path: str | Path | None = None,
    dest: Path | None = None,
) -> Path | None:
    """Grab the densest POV-killfeed frame from an already-rendered video.

    No CS2/HLAE. Tick → seconds via the concat sidecar. ``dest`` is a JPEG;
    a temp file is used when omitted.
    """
    video = Path(video_path)
    if not video.is_file():
        return None

    extras = list(extra_sidecars or [])
    if demo_path:
        stem = Path(demo_path).stem
        renders = _PROJECT_ROOT / "renders"
        if renders.is_dir():
            for d in renders.glob(f"pov-{stem}_*"):
                extras.append(d / "combined.round_offsets.json")
                if analysis_path is None:
                    ap = d / "csdm_analysis.json"
                    if ap.is_file():
                        analysis_path = ap

    side_p = Path(sidecar_path) if sidecar_path else find_round_offsets_sidecar(
        video, extra=extras,
    )
    sidecar: dict | None = None
    if side_p is not None and side_p.is_file():
        sidecar = json.loads(side_p.read_text(encoding="utf-8"))
    elif demo_path and steam_id:
        spans = round_spans_from_demo(demo_path, steam_id)
        dur = probe_video_duration(video)
        if spans and dur > 1:
            sidecar = sidecar_from_round_spans(spans, dur)
            cache = video.parent / "video.round_offsets.json"
            try:
                cache.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
                print(f"  [bg] synthesized sidecar ({len(spans)} rounds) -> {cache.name}",
                      flush=True)
            except OSError:
                pass
    if sidecar is None:
        return None

    loaded = load_kill_timeline(demo_path) if demo_path else None
    if loaded:
        kills, tickrate = loaded
    elif analysis_path and Path(analysis_path).is_file():
        data = json.loads(Path(analysis_path).read_text(encoding="utf-8"))
        tickrate = float(data.get("tickrate", 64) or 64)
        kills = data.get("kills", [])
    elif demo_path:
        parsed = kills_from_demo(demo_path)
        if not parsed:
            return None
        kills, tickrate = parsed
    else:
        return None

    ranked = rank_killfeed_kills(kills, steam_id, tickrate)
    if not ranked:
        return None

    seek_t = None
    picked = None
    feed_n = 0
    missed = 0
    for kill, n in ranked:
        t = demo_tick_to_video_seconds(sidecar, int(kill["tick"]))
        if t is None:
            missed += 1
            continue
        seek_t = t + KILLFEED_AFTER_SECONDS
        picked = kill
        feed_n = n
        break
    if seek_t is None or picked is None:
        print(
            f"  [bg] no tick mapped ({missed}/{len(ranked)} POV kills outside round spans)",
            flush=True,
        )
        return None

    total = float(sidecar.get("total_duration_seconds") or 0)
    if total > 0:
        seek_t = min(total - 0.05, seek_t)
        # Round-win / scoreboard often replaces the feed in the last couple
        # of seconds of a round clip — skip those if an earlier kill maps.
        if seek_t >= total - 2.0:
            alt = None
            for kill, n in ranked:
                t = demo_tick_to_video_seconds(sidecar, int(kill["tick"]))
                if t is None:
                    continue
                cand = min(total - 0.05, max(0.0, t + KILLFEED_AFTER_SECONDS))
                if cand < total - 2.0:
                    alt = (cand, kill, n)
                    break
            if alt is not None:
                seek_t, picked, feed_n = alt
    seek_t = max(0.0, seek_t)

    weapon = picked.get("weaponName") or picked.get("weapon") or "?"
    victim = picked.get("victimName") or picked.get("victim") or "?"
    approx = " approx" if sidecar.get("approximate") else ""
    print(
        f"  [bg] kill frame @ {seek_t:.2f}s (tick {picked['tick']}, "
        f"{feed_n} POV on feed, {weapon} -> {victim}{approx})",
        flush=True,
    )

    if dest is None:
        fd, name = tempfile.mkstemp(prefix="thumb_kill_", suffix=".jpg")
        os.close(fd)
        dest = Path(name)
    else:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{seek_t:.3f}", "-i", str(video),
         "-frames:v", "1",
         "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080:(in_w-1920)/2:0",
         str(dest)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        return None
    return dest
