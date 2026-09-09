"""Per-player within-match features from a .dem (K/D, 4k/ace, map win).

Used to join LIM POV rows to the actual demo instead of player/org intercepts.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from hltv.update_team_demand import canonical_team  # noqa: E402
from shorts.pro_context import _norm  # noqa: E402

DEMOS = ROOT / "demos" / "hltv"
OUT_DEFAULT = ROOT / ".data" / "demo_pov_features.jsonl"
_MAP_FROM_NAME = re.compile(
    r"-(dust2|inferno|mirage|nuke|anubis|ancient|train|cache|overpass|vertigo)"
    r"(?:-|\.dem)",
    re.I,
)
def teams_from_folder(name: str) -> tuple[str, str]:
    rest = re.sub(r"^\d+-", "", name)
    if "-vs-" not in rest:
        return "", ""
    left, right = rest.split("-vs-", 1)
    right = re.split(
        r"-(?:blast|iem|esl|pgl|esports|starladder|katowice|cologne|major|ewc)\b",
        right, maxsplit=1, flags=re.I)[0]
    return left.replace("-", " "), right.replace("-", " ")


def multi_bucket(aces: int, fourks: int) -> str:
    if aces:
        return "ace"
    if fourks:
        return "4k"
    return "none"


def summarize_rounds(by_round: dict[int, dict[str, int]],
                     round_winners: dict[int, str],
                     player_side: dict[tuple[int, str], str]) -> dict[str, dict]:
    """Aggregate per-player kills/round into 4k/ace + map win.

    by_round[round][nick] = kills in that round.
    round_winners[round] = 'CT' or 'T'.
    player_side[(round, nick)] = 'CT' or 'T'.
    """
    out: dict[str, dict] = {}
    nicks = set()
    for kills in by_round.values():
        nicks.update(kills)
    nicks.update(nick for _, nick in player_side)
    for nick in nicks:
        aces = fourks = kills = 0
        won = lost = 0
        for rnd, rkills in by_round.items():
            nkill = int(rkills.get(nick) or 0)
            kills += nkill
            if nkill >= 5:
                aces += 1
            elif nkill >= 4:
                fourks += 1
            winner = round_winners.get(rnd)
            side = player_side.get((rnd, nick))
            if winner and side:
                if side == winner:
                    won += 1
                else:
                    lost += 1
        out[nick] = {
            "kills": kills,
            "aces": aces,
            "fourks": fourks,
            "won_map": "yes" if won > lost else "no" if lost > won else "unknown",
            "multi": multi_bucket(aces, fourks),
        }
    return out


def _side_token(raw) -> str:
    text = str(raw or "").strip().upper()
    if text in {"CT", "COUNTERTERRORIST", "COUNTER-TERRORIST"}:
        return "CT"
    if text in {"T", "TERRORIST"}:
        return "T"
    return ""


def extract_demo_features(demo_path: Path) -> dict[str, dict]:
    """nick.lower() -> kills/deaths/aces/fourks/won_map/multi."""
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))
    deaths = parser.parse_event("player_death")
    round_starts = parser.parse_event("round_start")
    round_ends = parser.parse_event("round_end")
    if deaths is None or len(deaths) == 0:
        return {}

    first_real_tick = 0
    if round_starts is not None and len(round_starts):
        r1 = round_starts[round_starts["round"] == 1]
        if len(r1):
            first_real_tick = int(r1["tick"].max())

    start_ticks: list[tuple[int, int]] = []
    if round_starts is not None and len(round_starts):
        for _, row in round_starts.iterrows():
            tick = int(row["tick"])
            if tick < first_real_tick:
                continue
            start_ticks.append((tick, int(row.get("round") or 0)))
    start_ticks.sort()

    def round_at(tick: int) -> int:
        rnd = 0
        for st, rr in start_ticks:
            if tick >= st:
                rnd = rr
            else:
                break
        return rnd

    by_round: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    deaths_n: dict[str, int] = defaultdict(int)
    att = deaths["attacker_steamid"].astype(str)
    vic = deaths["user_steamid"].astype(str)
    core = deaths[(deaths["tick"] >= first_real_tick) & (att != vic)]
    for _, row in core.iterrows():
        nick = str(row.get("attacker_name") or "").strip().lower()
        victim = str(row.get("user_name") or "").strip().lower()
        rnd = round_at(int(row["tick"]))
        if nick:
            by_round[rnd][nick] += 1
        if victim:
            deaths_n[victim] += 1

    round_winners: dict[int, str] = {}
    player_side: dict[tuple[int, str], str] = {}
    if round_ends is not None and len(round_ends):
        end_ticks = []
        for _, row in round_ends.iterrows():
            tick = int(row["tick"])
            if tick < first_real_tick:
                continue
            rnd = round_at(tick)
            winner = _side_token(row.get("winner"))
            if winner:
                round_winners[rnd] = winner
            end_ticks.append(tick)
        if end_ticks:
            try:
                snap = parser.parse_ticks(["team_name"], ticks=end_ticks)
            except Exception:
                snap = None
            if snap is not None and len(snap):
                for _, row in snap.iterrows():
                    nick = str(row.get("name") or "").strip().lower()
                    side = _side_token(row.get("team_name"))
                    rnd = round_at(int(row["tick"]))
                    if nick and side:
                        player_side[(rnd, nick)] = side

    summary = summarize_rounds(by_round, round_winners, player_side)
    for nick, stats in summary.items():
        stats["deaths"] = int(deaths_n.get(nick) or 0)
        kd = None
        if stats["deaths"]:
            kd = stats["kills"] / stats["deaths"]
        elif stats["kills"]:
            kd = float(stats["kills"])
        stats["kd"] = kd
    return summary


def map_from_demo_name(name: str) -> str:
    hit = _MAP_FROM_NAME.search(name.lower().replace("_", "-"))
    if hit:
        return hit.group(1).lower()
    return ""


def fixture_key(team1: str, team2: str, game_map: str) -> str:
    def canon(name: str) -> str:
        hit = canonical_team(name)
        return _norm(hit or name)
    a, b = sorted([canon(team1), canon(team2)])
    return f"{a}|{b}|{(game_map or '').lower()}"


def index_local_demos(root: Path | None = None) -> dict[str, Path]:
    """fixture_key -> demo path (first match)."""
    base = root or DEMOS
    out: dict[str, Path] = {}
    if not base.exists():
        return out
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        t1, t2 = teams_from_folder(folder.name)
        if not t1 or not t2:
            continue
        for dem in folder.glob("*.dem"):
            game_map = map_from_demo_name(dem.name)
            if not game_map:
                continue
            out[fixture_key(t1, t2, game_map)] = dem
    return out


def lookup_demo(row: dict, index: dict[str, Path]) -> Path | None:
    org = str(row.get("org") or "")
    opp = str(row.get("opp") or "")
    game_map = str(row.get("map") or "")
    if not org or not opp or not game_map:
        return None
    return index.get(fixture_key(org, opp, game_map))


def load_feature_cache(path: Path | None = None) -> dict[str, dict[str, dict]]:
    """demo_stem -> {nick: stats}."""
    target = path or OUT_DEFAULT
    out: dict[str, dict[str, dict]] = {}
    if not target.exists():
        return out
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            out[row["demo"]] = row.get("players") or {}
    return out


def features_for_row(row: dict, cache: dict[str, dict[str, dict]],
                     index: dict[str, Path]) -> dict:
    dem = lookup_demo(row, index)
    if dem is None:
        return {"multi": "unknown", "demo_won": "unknown", "demo_kd": None}
    players = cache.get(dem.stem) or {}
    nick = str(row.get("player") or "").strip().lower()
    stats = players.get(nick) or {}
    if not stats:
        for alias, value in players.items():
            if _norm(alias) == _norm(nick):
                stats = value
                break
    if not stats:
        return {"multi": "unknown", "demo_won": "unknown", "demo_kd": None}
    return {
        "multi": stats.get("multi") or "none",
        "demo_won": stats.get("won_map") or "unknown",
        "demo_kd": stats.get("kd"),
        "demo_aces": stats.get("aces") or 0,
        "demo_fourks": stats.get("fourks") or 0,
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demos", type=Path, default=DEMOS)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()
    existing = load_feature_cache(args.out) if args.out.exists() else {}
    rows = []
    dems = sorted(args.demos.glob("*/*.dem"))
    print(f"demos={len(dems)} cached={len(existing)}", flush=True)
    for dem in dems:
        if dem.stem in existing:
            rows.append({"demo": dem.stem, "path": str(dem),
                         "players": existing[dem.stem]})
            continue
        print(f"  extract {dem.name}", flush=True)
        try:
            players = extract_demo_features(dem)
        except Exception as exc:
            print(f"  FAIL {dem.name}: {exc}", flush=True)
            players = {}
        rows.append({"demo": dem.stem, "path": str(dem), "players": players})
        existing[dem.stem] = players
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
