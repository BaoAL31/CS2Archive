"""Poll HLTV results and render the highest-weighted POV per match.

Cards are scored with highlight-channel team demand, POV-channel player
demand, org rank, and HLTV rating. One card per match is queued from
``backlog/<match>/{high,medium}/``.

The listener is intentionally a single process and a single pipeline worker.
It persists state in ``.listener/hltv.json`` so a restart does not repeat
downloads or renders.

Examples:
    python scripts/hltv/match_listener.py --once --dry-run
    python scripts/hltv/match_listener.py --event-url https://www.hltv.org/events/8249/blast-open-porto-2026
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
_SCRIPTS = ROOT / "scripts"
for _p in (str(ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

from config import settings  # noqa: E402
from hltv.score_cards import (  # noqa: E402
    attach_scores,
    format_score,
    load_indexes,
    maybe_refresh_indexes,
)
from scrapers.hltv_acquire import fetch_hltv_page_html, match_slug_from_url  # noqa: E402

DEFAULT_EVENT_URL = "https://www.hltv.org/events/8249/blast-open-porto-2026"
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


def _candidate_cards(match: Match, indexes: dict | None = None) -> list[str]:
    match_slug = match_slug_from_url(match.url)
    root = ROOT / "backlog" / match_slug
    cards: list[tuple[str, dict]] = []
    for bucket in ("high", "medium"):
        folder = root / bucket
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cards.append((str(path.relative_to(ROOT)).replace("\\", "/"), meta))
    scored = attach_scores(
        cards, indexes=indexes, fixture_teams=(match.team1, match.team2)
    )
    selected = select_best_card(scored)
    for path, meta in selected:
        print(
            f"[score] {meta.get('player')} {meta.get('map')} "
            f"{format_score(meta)} ({path})",
            flush=True,
        )
    return [path for path, _ in selected]


def _card_rank(meta: dict) -> tuple[float, float]:
    """Higher weight, then higher rating."""
    try:
        weight = float(meta.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0.0
    try:
        rating = float(meta.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    return (weight, rating)


def select_best_card(
    cards: list[tuple[str, dict]],
) -> list[tuple[str, dict]]:
    """Select the single highest-weighted card (one POV per match)."""
    best: tuple[str, dict] | None = None
    for path, meta in cards:
        if not str(meta.get("map", "")).strip():
            continue
        if best is None:
            best = (path, meta)
            continue
        cand = _card_rank(meta)
        cur = _card_rank(best[1])
        if cand > cur or (cand == cur and path < best[0]):
            best = (path, meta)
    return [best] if best else []


def _prune_queue(cards: list[str], indexes: dict | None = None) -> list[str]:
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
        for path, _ in select_best_card(attach_scores(group, indexes=indexes))
    ]
    return passthrough + selected


def sort_card_records(cards: list[tuple[str, dict]]) -> list[str]:
    """Order queued cards by weight, then rating, then path."""
    def key(record: tuple[str, dict]) -> tuple:
        path, meta = record
        weight, rating = _card_rank(meta)
        return (-weight, -rating, path)

    return [path for path, _ in sorted(cards, key=key)]


def _sort_queue(cards: list[str], indexes: dict | None = None) -> list[str]:
    records: list[tuple[str, dict]] = []
    passthrough: list[str] = []
    for path in cards:
        try:
            meta = json.loads((ROOT / path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            passthrough.append(path)
            continue
        records.append((path, meta))
    if records:
        records = attach_scores(records, indexes=indexes)
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


def _enqueue(state: State, cards: list[str], indexes: dict | None = None) -> None:
    queued = set(state.data["queue"])
    for card in cards:
        if card not in queued:
            state.data["queue"].append(card)
            queued.add(card)
    state.data["queue"] = _sort_queue(state.data["queue"], indexes=indexes)


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


def initialize_result_baseline(state: State, matches: list[Match]) -> None:
    """Mark everything currently visible as historical and non-actionable."""
    current_ids = (
        {match.match_id for match in matches}
        | set(state.data["matches"].keys())
    )
    for record in state.data["matches"].values():
        record["status"] = "completed"
        record["last_error"] = None
    for match in matches:
        record = state.data["matches"].setdefault(match.match_id, {
            "match": asdict(match), "status": "completed",
            "attempts": 0, "last_error": None,
        })
        record["match"] = asdict(match)
        record["status"] = "completed"
    state.data["result_baseline_initialized"] = True
    state.data["result_baseline_ids"] = sorted(current_ids)
    state.data["queue"] = []
    state.data["baseline_at"] = _now()


def _actionable_matches(state: State, matches: list[Match]) -> list[Match]:
    """Return only newly appeared results or records that need retrying."""
    known = set(state.data.get("result_baseline_ids", []))
    return [
        match for match in matches
        if match.match_id not in known
        or state.data["matches"].get(match.match_id, {}).get("status")
        in {"retry", "discovered"}
    ]


async def poll_once(args, state: State) -> None:
    event_url = args.event_url
    state.data["event_url"] = event_url
    if not args.dry_run:
        maybe_refresh_indexes(scrape=True)
    indexes = load_indexes()
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
    if not state.data.get("result_baseline_initialized"):
        initialize_result_baseline(state, matches)
        state.save()
        print(f"[baseline] skipped {len(matches)} existing completed match(es)",
              flush=True)
        return
    actionable = _actionable_matches(state, matches)
    state.data["result_baseline_ids"] = sorted(
        set(state.data.get("result_baseline_ids", []))
        | {match.match_id for match in matches}
    )

    for match in actionable:
        record = state.data["matches"].setdefault(match.match_id, {
            "match": asdict(match), "status": "discovered",
            "attempts": 0, "last_error": None,
        })
        if record["status"] in {"queued", "running", "completed"}:
            continue
        if not _retry_ready(record):
            continue
        cards = _candidate_cards(match, indexes)
        if cards:
            print(f"[backlog] adopting {len(cards)} weighted "
                  f"card(s) for {match.match_id}", flush=True)
        else:
            record["attempts"] += 1
            if not _run_backlog(match, args.dry_run, args.backlog_retries):
                _schedule_retry(record, "create_backlog failed")
                state.save()
                continue
            cards = _candidate_cards(match, indexes)
        if args.dry_run:
            cards = [f"backlog/{match_slug_from_url(match.url)}/high/<generated>.json"]
        if not cards:
            # A successful backlog run can legitimately produce no high/medium
            # performer (all ratings below 1.0). This is a completed no-op.
            record["status"] = "completed"
            record["cards"] = []
            record["no_candidate_cards"] = True
            record["last_error"] = None
            state.save()
            continue
        _enqueue(state, cards, indexes)
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
            # HARD FAIL: never silently skip or retain for retry.
            # Surface the pipeline's [PIPELINE_ERROR] immediately so the
            # underlying bug (e.g. zero-length victim seq 186388->186388)
            # gets fixed instead of looping every poll.
            for record in state.data["matches"].values():
                if card in record.get("cards", []):
                    record["status"] = "failed"
                    record["last_error"] = f"pipeline hard-failed for {card}"
            state.save()
            # SystemExit is BaseException, not caught by main's `except Exception`,
            # so the listener dies and the failure is visible (no silent skip/loop).
            print(f"[queue] HARD FAIL pipeline for {card} — fix underlying bug, not skipping", flush=True)
            raise SystemExit(f"hard fail: pipeline failed for {card}")
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
    parser.add_argument("--pipeline-retries", type=int, default=3,
                        help="retry a failed pipeline this many times before skipping it")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-teams", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--no-rebaseline",
        action="store_true",
        help="keep existing result_baseline_ids across launch (for event switches / catch-up)",
    )
    return parser


async def main(args) -> None:
    state = State(args.state)
    if args.status:
        print(json.dumps(state.data, indent=2))
        return
    with SingleInstance(args.state.with_suffix(".lock")):
        # Default: every launch establishes a fresh edge — results already
        # visible now are historical, while anything appearing after this
        # launch is new. --no-rebaseline keeps the on-disk baseline (used
        # when switching events so already-completed matches can still be
        # actioned if removed from baseline_ids).
        if not args.no_rebaseline:
            state.data["result_baseline_initialized"] = False
            state.data["queue"] = []
            state.save()
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
