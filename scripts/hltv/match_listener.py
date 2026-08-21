"""Poll HLTV results and render high-priority POVs for notable teams.

The listener is intentionally a single process and a single pipeline worker.
It persists state in ``.listener/hltv.json`` so a restart does not repeat
downloads or renders.

Examples:
    python scripts/hltv/match_listener.py --once --dry-run
    python scripts/hltv/match_listener.py --event-url https://www.hltv.org/events/8261/esports-world-cup-2026
    python scripts/hltv/match_listener.py --refresh-teams --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import settings  # noqa: E402
from scrapers.hltv_acquire import fetch_hltv_page_html, match_slug_from_url  # noqa: E402

DEFAULT_EVENT_URL = "https://www.hltv.org/events/8261/esports-world-cup-2026"
DEFAULT_STATE = ROOT / ".listener" / "hltv.json"
DEFAULT_RANKINGS_URL = "https://www.hltv.org/ranking/teams"
MIN_RATING = 1.5


@dataclass
class Match:
    match_id: str
    url: str
    slug: str
    team1: str
    team2: str
    event: str = ""


def _slug_teams(slug: str) -> tuple[str, str]:
    parts = slug.lower().split("-vs-", 1)
    if len(parts) != 2:
        return slug.replace("-", " "), ""
    return tuple(part.replace("-", " ") for part in parts)  # type: ignore[return-value]


def _canonical_match(href: str) -> tuple[str, str] | None:
    match = re.search(r"/matches/(\d+)/([^/?#]+)", href)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_match_links(html: str, base_url: str = settings.hltv_base_url) -> list[Match]:
    """Parse unique completed-match links from a results page fixture."""
    soup = BeautifulSoup(html, "lxml")
    results = soup.select_one(".results-holder")
    links = results.select('a[href*="/matches/"]') if results else soup.select(
        'a[href*="/matches/"]')
    found: list[Match] = []
    seen: set[str] = set()
    for link in links:
        parsed = _canonical_match(link.get("href", ""))
        if not parsed:
            continue
        match_id, slug = parsed
        if match_id in seen:
            continue
        seen.add(match_id)
        team1, team2 = _slug_teams(slug)
        result = link.find_parent(class_="result-con")
        event = ""
        if result:
            event_node = result.select_one(".event-name")
            event = event_node.get_text(" ", strip=True) if event_node else ""
        found.append(Match(
            match_id=match_id,
            url=urljoin(base_url, link["href"]),
            slug=slug,
            team1=team1,
            team2=team2,
            event=event,
        ))
    return found


def parse_event_match_ids(html: str) -> set[str]:
    """Return match ids linked by an HLTV event page."""
    return {m.match_id for m in parse_match_links(html)}


def parse_top_teams(html: str, limit: int = 20) -> list[str]:
    """Extract the ordered team names from the current HLTV rankings page."""
    soup = BeautifulSoup(html, "lxml")
    selectors = (
        ".ranked-team .teamLine .name",
        ".ranking-item .ranking-item-team-name",
        ".ranking-item-team-name",
        ".ranked-team .teamName",
    )
    names: list[str] = []
    for selector in selectors:
        for node in soup.select(selector):
            name = node.get_text(" ", strip=True)
            if name and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
        if names:
            break
    return names[:limit]


def select_matches(matches: list[Match], event_ids: set[str],
                   notable_teams: list[str], event_name: str = "") -> list[Match]:
    """Keep completed event matches involving at least one notable team."""
    teams = {team.casefold() for team in notable_teams}
    selected = []
    for match in matches:
        event_match = match.match_id in event_ids
        if event_name and match.event:
            event_match = match.event.casefold() == event_name.casefold()
        if not event_match:
            continue
        if match.team1.casefold() in teams or match.team2.casefold() in teams:
            selected.append(match)
    return selected


def _default_state() -> dict:
    return {
        "version": 1,
        "created_at": _now(),
        "updated_at": _now(),
        "event_url": DEFAULT_EVENT_URL,
        "teams": [],
        "teams_updated_at": None,
        "matches": {},
        "queue": [],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()
        self.data["queue"] = _sort_queue(_prune_queue(self.data.get("queue", [])))

    def _load(self) -> dict:
        if not self.path.exists():
            return _default_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {**_default_state(), **data}
        except (OSError, json.JSONDecodeError):
            backup = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return _default_state()

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temp.replace(self.path)


class SingleInstance:
    """Best-effort Windows process lock using an exclusive lock file."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="ascii")
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError("another match listener is already running") from exc
        return self

    def __exit__(self, *_):
        import msvcrt
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
        except OSError:
            pass


def _high_priority_cards(match: Match) -> list[str]:
    match_slug = match_slug_from_url(match.url)
    root = ROOT / "backlog" / match_slug / "high"
    if not root.is_dir():
        return []
    cards = []
    for path in sorted(root.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cards.append((str(path.relative_to(ROOT)).replace("\\", "/"), meta))
    return [path for path, _ in select_highest_per_map(cards)]


def select_highest_per_map(
    cards: list[tuple[str, dict]],
) -> list[tuple[str, dict]]:
    """Select one highest-rated card for each map, deterministically."""
    best: dict[str, tuple[str, dict]] = {}
    for path, meta in cards:
        map_name = str(meta.get("map", "")).strip().casefold()
        if not map_name:
            continue
        try:
            rating = float(meta.get("rating", 0))
        except (TypeError, ValueError):
            rating = 0.0
        current = best.get(map_name)
        if current is None:
            best[map_name] = (path, meta)
            continue
        try:
            current_rating = float(current[1].get("rating", 0))
        except (TypeError, ValueError):
            current_rating = 0.0
        if rating > current_rating or (rating == current_rating and path < current[0]):
            best[map_name] = (path, meta)
    return sorted(
        best.values(),
        key=lambda item: (str(item[1].get("map", "")).casefold(), item[0]),
    )


def _prune_queue(cards: list[str]) -> list[str]:
    groups: dict[str, list[tuple[str, dict]]] = {}
    passthrough: list[str] = []
    for path in cards:
        full = ROOT / path
        try:
            meta = json.loads(full.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            passthrough.append(path)
            continue
        group = str(meta.get("hltv_url", "")).strip() or str(full.parent.parent)
        groups.setdefault(group, []).append((path, meta))
    selected = [
        path
        for group in groups.values()
        for path, _ in select_highest_per_map(group)
    ]
    return passthrough + selected


def sort_card_records(cards: list[tuple[str, dict]]) -> list[str]:
    """Order queued cards by rating descending, then path."""
    def rating(record: tuple[str, dict]) -> float:
        try:
            return float(record[1].get("rating", 0))
        except (TypeError, ValueError):
            return 0.0

    return [
        path for path, _ in sorted(cards, key=lambda item: (-rating(item), item[0]))
    ]


def _sort_queue(cards: list[str]) -> list[str]:
    records: list[tuple[str, dict]] = []
    passthrough: list[str] = []
    for path in cards:
        try:
            meta = json.loads((ROOT / path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            passthrough.append(path)
            continue
        records.append((path, meta))
    return sort_card_records(records) + passthrough


def _child_env() -> dict[str, str]:
    """Give direct script launches the same import path as the repo launcher."""
    env = os.environ.copy()
    paths = [str(ROOT / "scripts"), str(ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _enqueue(state: State, cards: list[str]) -> None:
    queued = set(state.data["queue"])
    for card in cards:
        if card not in queued:
            state.data["queue"].append(card)
            queued.add(card)
    state.data["queue"] = _sort_queue(state.data["queue"])


def _run_backlog(match: Match, dry_run: bool, retries: int = 3) -> bool:
    cmd = [sys.executable, str(ROOT / "scripts/pov/create_backlog.py"), match.url]
    if dry_run:
        print(f"[backlog] {' '.join(cmd)}", flush=True)
        return True
    for attempt in range(1, retries + 1):
        print(f"[backlog] attempt {attempt}/{retries}: {' '.join(cmd)}",
              flush=True)
        if subprocess.run(cmd, cwd=ROOT, env=_child_env()).returncode == 0:
            return True
        if attempt < retries:
            delay = 30 * attempt
            print(f"[backlog] failed; retrying in {delay}s", flush=True)
            time.sleep(delay)
    return False


def _run_short_extraction(match: Match, dry_run: bool) -> bool:
    """Extract Shorts timelines for every demo in a newly acquired match."""
    demo_dir = ROOT / "demos" / "hltv" / match_slug_from_url(match.url)
    demos = sorted(demo_dir.glob("*.dem"))
    if dry_run:
        print(f"[shorts] would extract {len(demos)} demo(s) for {match.match_id}",
              flush=True)
        return True
    if not demos:
        print(f"[shorts] no demos available for {match.match_id}", flush=True)
        return False
    script = ROOT / "scripts" / "shorts" / "build_short_timeline.py"
    for demo in demos:
        cmd = [sys.executable, str(script), str(demo)]
        print(f"[shorts] extracting: {' '.join(cmd)}", flush=True)
        if subprocess.run(cmd, cwd=ROOT, env=_child_env()).returncode != 0:
            print(f"[shorts] extraction failed: {demo.name}", flush=True)
            return False
    return True


def _run_pipeline(card: str, dry_run: bool) -> bool:
    cmd = [sys.executable, str(ROOT / "scripts/pov/pipeline.py"),
           "--backlog", str(ROOT / card)]
    print(f"[pipeline] {' '.join(cmd)}", flush=True)
    if dry_run:
        return True
    return subprocess.run(cmd, cwd=ROOT, env=_child_env()).returncode == 0


def _retry_ready(record: dict) -> bool:
    retry_at = record.get("next_retry_at")
    if not retry_at:
        return True
    try:
        return datetime.now(timezone.utc).timestamp() >= datetime.fromisoformat(
            retry_at).timestamp()
    except ValueError:
        return True


def _schedule_retry(record: dict, error: str) -> None:
    attempt = int(record.get("attempts", 1))
    delay = min(3600, 60 * (2 ** min(attempt - 1, 5)))
    record["status"] = "retry"
    record["last_error"] = error
    record["next_retry_at"] = datetime.fromtimestamp(
        time.time() + delay, timezone.utc).isoformat()


async def poll_once(args, state: State) -> None:
    event_url = args.event_url
    state.data["event_url"] = event_url
    if args.refresh_teams or not state.data["teams"]:
        html = await asyncio.to_thread(fetch_hltv_page_html, args.rankings_url,
                                        headless=True, wait_selector=None)
        teams = parse_top_teams(html)
        if not teams:
            raise RuntimeError("HLTV rankings page contained no team names")
        state.data["teams"] = teams
        state.data["teams_updated_at"] = _now()
        print(f"[teams] tracking: {', '.join(teams)}")
        args.refresh_teams = False

    event_html = await asyncio.to_thread(fetch_hltv_page_html, event_url,
                                         headless=True, wait_selector=None)
    event_ids = parse_event_match_ids(event_html)
    result_links: list[Match] = []
    for offset in (0, 1):
        target_date = (datetime.now().date() - timedelta(days=offset)).isoformat()
        results_url = f"{settings.hltv_base_url}/results?date={target_date}"
        results_html = await asyncio.to_thread(fetch_hltv_page_html, results_url,
                                                headless=True, wait_selector=None)
        result_links.extend(parse_match_links(results_html))
    event_slug = event_url.rstrip("/").rsplit("/", 1)[-1]
    event_name = re.sub(r"[-_]+", " ", event_slug).casefold()
    matches = select_matches(result_links, event_ids, state.data["teams"],
                             event_name)
    matches = list({match.match_id: match for match in matches}.values())
    print(f"[poll] {len(matches)} notable completed event match(es)", flush=True)

    for match in matches:
        record = state.data["matches"].setdefault(match.match_id, {
            "match": asdict(match), "status": "discovered",
            "attempts": 0, "last_error": None,
        })
        if record["status"] in {"queued", "running", "completed"}:
            if record.get("shorts_status") != "completed":
                if _run_short_extraction(match, args.dry_run):
                    record["shorts_status"] = "completed"
                    record["shorts_error"] = None
                else:
                    record["shorts_status"] = "retry"
                    record["shorts_error"] = "short extraction failed"
                state.save()
            continue
        if not _retry_ready(record):
            continue
        cards = _high_priority_cards(match)
        if cards:
            print(f"[backlog] adopting {len(cards)} existing high-priority "
                  f"card(s) for {match.match_id}", flush=True)
        else:
            record["attempts"] += 1
            if not _run_backlog(match, args.dry_run, args.backlog_retries):
                _schedule_retry(record, "create_backlog failed")
                state.save()
                continue
            cards = _high_priority_cards(match)
        if args.dry_run:
            cards = [f"backlog/{match_slug_from_url(match.url)}/high/<generated>.json"]
        if not cards:
            _schedule_retry(record, "create_backlog failed")
            state.save()
            continue
        if record.get("shorts_status") != "completed":
            if _run_short_extraction(match, args.dry_run):
                record["shorts_status"] = "completed"
                record["shorts_error"] = None
            else:
                record["shorts_status"] = "retry"
                record["shorts_error"] = "short extraction failed"
        _enqueue(state, cards)
        record["status"] = "queued"
        record["cards"] = cards
        record["last_error"] = None
        state.save()

    while state.data["queue"]:
        card = state.data["queue"][0]
        if not args.dry_run and not (ROOT / card).exists():
            print(f"[queue] missing card, dropping: {card}", flush=True)
            state.data["queue"].pop(0)
            state.save()
            continue
        ok = _run_pipeline(card, args.dry_run)
        if not ok:
            print(f"[queue] pipeline failed; retaining for next poll: {card}",
                  flush=True)
            break
        state.data["queue"].pop(0)
        for record in state.data["matches"].values():
            if card in record.get("cards", []):
                record.setdefault("completed_cards", []).append(card)
                record["status"] = "completed"
        state.save()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-url", default=DEFAULT_EVENT_URL)
    parser.add_argument("--rankings-url", default=DEFAULT_RANKINGS_URL)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--interval", type=int, default=300,
                        help="poll interval in seconds (default: 300)")
    parser.add_argument("--backlog-retries", type=int, default=3,
                        help="attempt each failed backlog acquisition this many times")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-teams", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser


async def main(args) -> None:
    state = State(args.state)
    if args.status:
        print(json.dumps(state.data, indent=2))
        return
    with SingleInstance(args.state.with_suffix(".lock")):
        while True:
            try:
                await poll_once(args, state)
                state.save()
            except Exception as exc:
                print(f"[listener-error] {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                state.data["last_error"] = str(exc)
                state.save()
            if args.once:
                return
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main(build_parser().parse_args()))
